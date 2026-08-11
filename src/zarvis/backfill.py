"""One-time backfill of contact history from Gmail into `zarvis.touch`.

WHY THIS EXISTS
---------------
Decay pressure — "this relationship is going cold and nothing will ever tell you"
— is the mechanism the whole system was built around. It is computed from the
last touch with a person.

`zarvis.touch` held three rows for seventy-seven people. With no touch to read,
`last_touch` fell back to `person.created_at`, which seed.py sets to now(). So
every person looked like they had been contacted on the day Zarvis was installed,
decay came out near zero for the entire book, and the first real ranking put
twenty people at an identical score with the tier boundary slicing through the
middle of the tie.

Nothing errored. The number was just quietly wrong, which is the failure mode
this file exists to remove.

The data was there the whole time: 43,850 messages of it. This reads the mailbox
once and writes down what actually happened.

SCOPE
-----
Bounded to known senders, same as ingest. A year back by default: far enough that
the plateau end of the decay curve is real rather than assumed, and cheap because
`messages.list` with a server-side query does the filtering.

Idempotent — safe to re-run, widen the window, and re-run again.

    PYTHONPATH=src python -m zarvis.backfill --months 12
    PYTHONPATH=src python -m zarvis.backfill --months 12 --dry-run
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime

import psycopg

from .config import get_config
from .db import connect
from .google_auth import execute, gmail_service
from .ingest.gmail import SENDER_CHUNK, _header, _known_senders, _sender_email
from .signals import prime_identity_cache, resolve_person_by_email

log = logging.getLogger(__name__)

CHANNEL = "email"
BATCH_SIZE = 50  # Gmail allows 100; 50 keeps error handling legible


def _addresses(raw: str | None) -> list[str]:
    """Every address in a To/Cc header.

    Naive comma split. Display names containing commas ("Miller, Ryan"
    <r@x.invalid>) produce a junk fragment, which resolves to nobody and is
    discarded — a wrong address here costs a missing touch, never a wrong one.
    """
    if not raw:
        return []
    out = []
    for piece in raw.split(","):
        addr = _sender_email(piece)
        if addr and "@" in addr:
            out.append(addr)
    return out


def _query(senders: list[str], months: int, *, outbound: bool) -> str:
    field = "to" if outbound else "from"
    scope = "in:sent" if outbound else "-in:sent -in:chats"
    return (
        f"{scope} newer_than:{months}m {field}:({' OR '.join(senders)})"
    )


def _list_ids(svc, query: str) -> list[str]:
    ids: list[str] = []
    page = None
    while True:
        resp = execute(
            svc.users().messages().list(
                userId="me", q=query, maxResults=500, pageToken=page
            )
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


# Gmail allows 250 quota units per user per second, and `messages.get` costs 5.
# A 50-request batch therefore spends the whole second's budget the instant it
# lands, so batches have to be paced rather than fired back to back.
BATCH_PAUSE_SECONDS = 1.0
FETCH_ROUNDS = 6


def _fetch_metadata(svc, message_ids: list[str]) -> list[dict]:
    """Batched metadata fetch. Headers only — bodies are not needed for a touch.

    Retries at the level that actually fails. A batch is one HTTP request
    carrying many independent sub-requests, and Gmail rate-limits the
    sub-requests: the outer call returns 200 while individual members come back
    403 rateLimitExceeded. Wrapping `batch.execute()` in a retry therefore
    catches nothing, and the first version of this quietly discarded 3,289 of
    ~4,900 messages while reporting success.

    Rate limiting is a speed limit, not a refusal, so anything that failed for
    that reason is owed another attempt. Failures are collected per message and
    replayed in successive rounds with a widening pause.
    """
    import time

    fetched: dict[str, dict] = {}
    pending = list(dict.fromkeys(message_ids))

    for round_number in range(FETCH_ROUNDS):
        failed: list[str] = []

        def collect(request_id, response, exception, _failed=failed):
            if exception is not None:
                _failed.append(request_id)
                return
            fetched[response["id"]] = response

        for i in range(0, len(pending), BATCH_SIZE):
            chunk = pending[i : i + BATCH_SIZE]
            batch = svc.new_batch_http_request(callback=collect)
            for message_id in chunk:
                # request_id is what comes back in the callback, so the message
                # id has to be carried explicitly to know WHICH one failed.
                batch.add(
                    svc.users().messages().get(
                        userId="me",
                        id=message_id,
                        format="metadata",
                        metadataHeaders=["From", "To", "Cc", "Date"],
                    ),
                    request_id=message_id,
                )
            execute(batch)
            time.sleep(BATCH_PAUSE_SECONDS)

        if not failed:
            break

        pause = BATCH_PAUSE_SECONDS * (2 ** (round_number + 1))
        log.warning(
            "backfill: %d messages rate limited, retrying in %.0fs [round %d/%d]",
            len(failed),
            pause,
            round_number + 1,
            FETCH_ROUNDS,
        )
        time.sleep(pause)
        pending = failed
    else:
        # Loop ran every round and `pending` still holds the stragglers.
        log.error(
            "backfill: %d messages still failing after %d rounds — touch history "
            "for these is MISSING, not absent. Re-run to fill the gap.",
            len(pending),
            FETCH_ROUNDS,
        )

    log.info("backfill: fetched %d of %d messages", len(fetched), len(message_ids))
    return list(fetched.values())


def _touches_from(
    conn: psycopg.Connection, messages: list[dict], *, outbound: bool
) -> list[tuple]:
    """-> rows of (person_id, channel, direction, external_ref, at)."""
    rows: list[tuple] = []
    direction = "outbound" if outbound else "inbound"

    for message in messages:
        internal_ms = message.get("internalDate")
        if not internal_ms:
            continue
        at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)

        if outbound:
            # One sent message can touch several people. Each is a real touch.
            candidates = _addresses(_header(message, "To")) + _addresses(
                _header(message, "Cc")
            )
        else:
            sender = _sender_email(_header(message, "From"))
            candidates = [sender] if sender else []

        seen: set[str] = set()
        for address in candidates:
            person_id = resolve_person_by_email(conn, address)
            if not person_id or person_id in seen:
                continue
            seen.add(person_id)
            rows.append((person_id, CHANNEL, direction, message["id"], at))

    return rows


def _write(conn: psycopg.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    workspace_id = get_config().workspace_id
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into zarvis.touch
                (workspace_id, person_id, channel, direction, external_ref, at)
            values (%s, %s, %s, %s, %s, %s)
            on conflict do nothing
            """,
            [(workspace_id, *row) for row in rows],
        )
    return len(rows)


def backfill(conn: psycopg.Connection, *, months: int, dry_run: bool) -> dict:
    svc = gmail_service()
    prime_identity_cache(conn)
    senders = _known_senders(conn)
    if not senders:
        log.warning("backfill: no known senders, nothing to do")
        return {}

    all_rows: list[tuple] = []
    counts: dict[str, int] = {}

    for outbound in (True, False):
        label = "outbound" if outbound else "inbound"
        message_ids: list[str] = []
        for i in range(0, len(senders), SENDER_CHUNK):
            chunk = senders[i : i + SENDER_CHUNK]
            message_ids.extend(_list_ids(svc, _query(chunk, months, outbound=outbound)))

        message_ids = list(dict.fromkeys(message_ids))
        log.info("backfill/%s: %d messages in %dm window", label, len(message_ids), months)

        messages = _fetch_metadata(svc, message_ids)
        rows = _touches_from(conn, messages, outbound=outbound)
        log.info("backfill/%s: %d touches resolved to known people", label, len(rows))
        counts[label] = len(rows)
        all_rows.extend(rows)

    if dry_run:
        people = len({r[0] for r in all_rows})
        log.info(
            "DRY RUN: %d touches across %d people, nothing written",
            len(all_rows),
            people,
        )
        conn.rollback()
        counts["written"] = 0
        return counts

    written = _write(conn, all_rows)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) c from zarvis.touch")
        total = cur.fetchone()["c"]
        cur.execute(
            "select count(distinct person_id) c from zarvis.touch"
        )
        covered = cur.fetchone()["c"]
    log.info("backfill: %d offered, table now holds %d touches across %d people",
             written, total, covered)
    counts["written"] = written
    counts["people_covered"] = covered
    return counts


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with connect() as conn:
        backfill(conn, months=args.months, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
