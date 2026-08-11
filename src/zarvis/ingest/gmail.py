"""Gmail ingest: detect replies and inbound mail from known people.

Polling, not Pub/Sub. Push requires a public HTTPS endpoint plus re-issuing
`users.watch` every <=7 days, which is real operational weight for latency a
once-daily agent does not need. `history.list` costs 2 quota units against a
6,000/min/user budget, so this is free in practice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg
# imported lazily inside ingest() — see google_auth.py for why

from ..config import get_config
from ..google_auth import execute, gmail_service
from ..signals import Signal, resolve_person_by_email

log = logging.getLogger(__name__)

SOURCE = "gmail"
SEED_WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def _get_cursor(conn: psycopg.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "select value from zarvis.cursor where workspace_id=%s and source=%s and key='default'",
            (get_config().workspace_id, SOURCE),
        )
        row = cur.fetchone()
    return row["value"] if row else None


def _set_cursor(conn: psycopg.Connection, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.cursor (workspace_id, source, key, value)
            values (%s, %s, 'default', %s)
            on conflict (workspace_id, source, key)
            do update set value = excluded.value, updated_at = now()
            """,
            (get_config().workspace_id, SOURCE, value),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------


def _header(message: dict, name: str) -> str | None:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _sender_email(raw: str | None) -> str | None:
    """Extract a bare address from `Name <addr@host>` or a plain address."""
    if not raw:
        return None
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return raw.strip().lower()


def _plaintext_body(message: dict) -> str | None:
    """Walk the MIME tree for text/plain. Raw text, never a summary."""
    import base64

    def walk(part: dict) -> str | None:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for child in part.get("parts", []) or []:
            found = walk(child)
            if found:
                return found
        return None

    return walk(message.get("payload", {}))


def _is_calendar_message(message: dict) -> bool:
    """Is this a machine-generated invite or RSVP rather than a person writing?

    Google sends `Accepted: Priya and Ryan Miller` from the counterparty's own
    address, so it passes every sender check and looks exactly like a reply. On
    the first real scan, ten of thirty-one threads were these — they would have
    entered the queue at `reply_received` urgency 8.0 ("they wrote, nobody
    answered") when an RSVP needs no answer at all, and calendar ingest had
    already recorded the same meeting at 3.0.

    Detected on the MIME type rather than the subject prefix, which is localised
    and reworded.
    """

    def walk(part: dict) -> bool:
        if (part.get("mimeType") or "").startswith("text/calendar"):
            return True
        return any(walk(child) for child in part.get("parts", []) or [])

    return walk(message.get("payload", {}))


def _to_signal(conn: psycopg.Connection, message: dict) -> Signal | None:
    labels = set(message.get("labelIds") or [])

    # Outbound mail is a *touch*, recorded when we send it. Ingest is only
    # interested in what came back.
    if "SENT" in labels or "DRAFT" in labels:
        return None

    # Meeting state belongs to calendar.py, which reads it from the calendar
    # rather than inferring it from mail about the calendar.
    if _is_calendar_message(message):
        return None

    sender = _sender_email(_header(message, "From"))
    if not sender:
        return None

    person_id = resolve_person_by_email(conn, sender)
    if person_id is None:
        # Not a known contact. Do NOT invent a person here — ingest observes,
        # it does not create entities. Counted by the caller so the gap stays
        # visible instead of silently dropped.
        return None

    internal_ms = message.get("internalDate")
    observed_at = (
        datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)
        if internal_ms
        else datetime.now(UTC)
    )

    return Signal(
        source=SOURCE,
        kind="reply_received",
        observed_at=observed_at,
        person_id=person_id,
        source_ref=message.get("threadId"),
        value={
            "message_id": message.get("id"),
            "thread_id": message.get("threadId"),
            "subject": _header(message, "Subject"),
            "from": sender,
            "rfc_message_id": _header(message, "Message-ID"),
        },
        body=_plaintext_body(message),
        # It arrived from them, so it is genuinely theirs.
        authored_by="counterparty",
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


# Gmail caps the `q` parameter, so a big from:() clause has to be chunked.
# Conservative: ~30 addresses is comfortably inside the limit.
SENDER_CHUNK = 30


# The product manufactures addresses for accounts that have no real one:
# `acct-<id>@placeholder.example.com` and
# `managed-<uuid>@managed.example.internal`. They are 27 of the 78 identities.
#
# They cannot match a message, so including them buys nothing — and they are not
# free. Gmail bills the `q` parameter by the query, so a third of every search
# was spent asking about addresses that do not exist, which is a real cost
# against a per-minute unit budget the backfill can exhaust in seconds.
SYNTHETIC_EMAIL_DOMAINS = (
    "placeholder.example.com",
    "managed.example.internal",
)


def _known_senders(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct lower(value) as email
            from zarvis.person_identity
            where workspace_id = %s and kind = 'email'
            """,
            (get_config().workspace_id,),
        )
        rows = [r["email"] for r in cur.fetchall() if r["email"]]

    real = [e for e in rows if not e.endswith(SYNTHETIC_EMAIL_DOMAINS)]
    if len(real) < len(rows):
        log.info(
            "gmail: %d of %d identities are synthetic product addresses, excluded",
            len(rows) - len(real),
            len(rows),
        )
    return real


def _seed_message_ids(svc, senders: list[str]) -> tuple[list[str], str | None]:
    """First run, or after a history gap: scan a recent window.

    Scoped to people we actually know. The naive version — every inbound message
    in the window — pulls hundreds of newsletters and notifications and discards
    ~95% of them AFTER paying for a full `messages.get` on each. Asking Gmail to
    filter server-side turns a few hundred round trips into a handful.

    With no known senders, this returns nothing rather than falling back to
    scanning everything: an empty address book means there is nobody to resolve
    a message to anyway.
    """
    ids: list[str] = []
    if not senders:
        log.warning("gmail: no known senders, skipping seed scan")
        return ids, svc.users().getProfile(userId="me").execute().get("historyId")

    for i in range(0, len(senders), SENDER_CHUNK):
        chunk = senders[i : i + SENDER_CHUNK]
        query = (
            f"-in:sent -in:draft -in:chats newer_than:{SEED_WINDOW_DAYS}d "
            f"from:({' OR '.join(chunk)})"
        )
        page = None
        while True:
            resp = execute(
                svc.users().messages().list(
                    userId="me", q=query, maxResults=100, pageToken=page
                )
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page = resp.get("nextPageToken")
            if not page:
                break

    log.info(
        "gmail: seed scan over %d known senders found %d messages", len(senders), len(ids)
    )
    profile = svc.users().getProfile(userId="me").execute()
    return ids, profile.get("historyId")


def _incremental_message_ids(svc, start_history_id: str) -> tuple[list[str], str | None]:
    """Cheap path: only what changed since the last run."""
    ids: list[str] = []
    page = None
    latest = start_history_id
    while True:
        resp = (
            svc.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page,
            )
            .execute()
        )
        for entry in resp.get("history", []):
            for added in entry.get("messagesAdded", []):
                ids.append(added["message"]["id"])
        latest = resp.get("historyId", latest)
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids, latest


def ingest(conn: psycopg.Connection) -> list[Signal]:
    """Collect reply signals. Returns them; the caller writes and counts."""
    # Lazy, so an uninstalled google client or a missing service-account key
    # degrades to "gmail source unavailable" instead of taking down product
    # ingest at import time. See google_auth.py.
    from googleapiclient.errors import HttpError

    svc = gmail_service()
    cursor = _get_cursor(conn)

    if cursor:
        try:
            message_ids, new_cursor = _incremental_message_ids(svc, cursor)
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            # A 404 means the stored historyId aged out of Gmail's window.
            # Documented behaviour, not an error: drop it and full-resync.
            log.warning("gmail history id expired; reseeding from %dd window", SEED_WINDOW_DAYS)
            message_ids, new_cursor = _seed_message_ids(svc, _known_senders(conn))
    else:
        log.info("gmail: no cursor, seeding from %dd window", SEED_WINDOW_DAYS)
        message_ids, new_cursor = _seed_message_ids(svc, _known_senders(conn))

    # Reply detection is a property of a THREAD, not of a message.
    #
    # The per-message version asked "did they write?" and every inbound message
    # answered yes — including messages in conversations already replied to. On
    # the first real scan that was seven of thirty-one threads entering the queue
    # at urgency 8.0 under the claim "they wrote, nobody answered", when the last
    # word in each was Ryan's. A queue that surfaces finished conversations
    # teaches you to stop trusting the queue.
    #
    # Collapsing to threads first also costs less: one `threads.get` replaces
    # every `messages.get` in that thread, and 71 messages were 31 threads.
    thread_ids = list(dict.fromkeys(m_id for m_id in message_ids))
    seen_threads: set[str] = set()

    signals: list[Signal] = []
    skipped_answered = 0
    skipped_calendar = 0
    unknown_senders = 0

    vanished = 0

    for message_id in thread_ids:
        # A message id is not a thread id, so the thread has to be resolved. The
        # metadata format is enough to learn which thread it belongs to.
        #
        # 404 is normal here and must not be fatal. `history.list` reports what
        # happened, not what still exists, so anything deleted between the
        # listing and this fetch comes back "Requested entity was not found" —
        # and an unhandled one killed the entire Gmail source. On a job that
        # retries every 30 minutes that is not a blip: it fails identically on
        # every attempt until the message ages out of the history window, so the
        # morning stays broken all day.
        try:
            head = execute(
                svc.users().messages().get(
                    userId="me", id=message_id, format="metadata", metadataHeaders=[]
                )
            )
            thread_id = head.get("threadId")
            if not thread_id or thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)

            thread = execute(
                svc.users().threads().get(userId="me", id=thread_id, format="full")
            )
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            vanished += 1
            continue
        messages = sorted(
            thread.get("messages", []), key=lambda m: int(m.get("internalDate") or 0)
        )
        if not messages:
            continue

        # Only the last word in the conversation matters. If it is ours, the ball
        # is in their court and there is nothing to queue.
        last = messages[-1]
        if "SENT" in set(last.get("labelIds") or []):
            skipped_answered += 1
            continue
        if _is_calendar_message(last):
            skipped_calendar += 1
            continue

        signal = _to_signal(conn, last)
        if signal:
            signals.append(signal)
        else:
            unknown_senders += 1

    # A dry run must not advance the cursor.
    #
    # This bit, badly. The first dry run fetched 71 messages, wrote no signals
    # because dry runs write no signals, and then committed the new historyId
    # anyway — so the next run asked Gmail for everything since, got nothing, and
    # those 71 messages were gone from the agent's view without ever having been
    # recorded. Silent, and invisible except as an inexplicable zero.
    #
    # The rule the cursor has to obey: advance only when the data it covers has
    # actually been persisted.
    if new_cursor and not get_config().dry_run:
        _set_cursor(conn, new_cursor)
    elif new_cursor:
        log.info("gmail: dry run, cursor left at %s", cursor or "unset")

    log.info(
        "gmail: %d messages -> %d threads; %d awaiting a reply, "
        "%d already answered, %d calendar RSVPs, %d unresolved senders, %d deleted",
        len(message_ids),
        len(seen_threads),
        len(signals),
        skipped_answered,
        skipped_calendar,
        unknown_senders,
        vanished,
    )
    return signals


def stale_threads(conn: psycopg.Connection, days: int = 3) -> list[dict]:
    """Threads where we spoke last and nothing came back.

    This is the raw material for decay pressure: a conversation that stopped
    mid-exchange decays differently from one that reached a natural close, and
    the difference is invisible from a summary but obvious from the thread.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute(
            """
            with outbound as (
                select person_id, max(at) as last_outbound
                from zarvis.touch
                where workspace_id = %(workspace_id)s
                  and direction = 'outbound'
                  and channel = 'email'
                group by person_id
            ),
            inbound as (
                select person_id, max(at) as last_inbound
                from zarvis.touch
                where workspace_id = %(workspace_id)s
                  and direction = 'inbound'
                group by person_id
            )
            select o.person_id, o.last_outbound, i.last_inbound
            from outbound o
            left join inbound i on i.person_id = o.person_id
            where o.last_outbound < %(cutoff)s
              and (i.last_inbound is null or i.last_inbound < o.last_outbound)
            order by o.last_outbound asc
            """,
            {"workspace_id": get_config().workspace_id, "cutoff": cutoff},
        )
        return cur.fetchall()
