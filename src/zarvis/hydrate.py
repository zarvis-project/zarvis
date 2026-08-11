"""Fetch the actual emails behind a person's touches.

    PYTHONPATH=src python -m zarvis.hydrate --person "dana whitfield"
    PYTHONPATH=src python -m zarvis.hydrate --person okafor --months 24
    PYTHONPATH=src python -m zarvis.hydrate --all --limit 10

The backfill recorded that 5,775 emails happened. This reads them.

WHY IT IS SEPARATE FROM BACKFILL
---------------------------------
Backfill runs over the whole book and stores metadata only, because decay
pressure needs a date and nothing else, and pulling 5,775 message bodies to
compute "weeks since last contact" would be absurd.

Hydration is on-demand and per person, because message bodies are only worth
fetching when something is about to read them: an escalation packet, a
deliberation, a draft into a live thread. Sam Okafor has fourteen touches
and 323 tokens of context; those are the same fourteen emails, unread.

It also repairs a gap backfill cannot. Backfill only sees people who already
exist in `zarvis.person`, so an active relationship that was never seeded is
invisible no matter how much mail there is. Dana Whitfield had a thread running
through two days ago and zero touches, because he is a marketing collaborator
rather than a product user and nothing ever created him. Hydration searches by
address, so it finds and records those threads the moment the person exists.
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from .config import get_config
from .db import connect
from .google_auth import execute, gmail_service
from .ingest.gmail import (
    SYNTHETIC_EMAIL_DOMAINS,
    _header,
    _plaintext_body,
    _sender_email,
)

log = logging.getLogger("zarvis.hydrate")

DEFAULT_MONTHS = 24
MAX_BODY_CHARS = 20000


def _addresses(conn: psycopg.Connection, person_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select lower(value) v from zarvis.person_identity
            where workspace_id = %s and person_id = %s and kind = 'email'
            """,
            (get_config().workspace_id, person_id),
        )
        return [
            r["v"] for r in cur.fetchall()
            if r["v"] and not r["v"].endswith(SYNTHETIC_EMAIL_DOMAINS)
        ]


def _queue_people(conn: psycopg.Connection, tiers: tuple[str, ...]) -> list[dict]:
    """Everyone the queue currently cares about.

    This is what makes nightly hydration affordable: the review room only reads
    the top of the board, so only the top of the board needs its mail fetched.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct p.id, p.full_name
            from zarvis.queue_item q
            join zarvis.person p on p.id = q.person_id
            where q.workspace_id = %s and q.status = 'open' and q.tier = any(%s)
            order by p.full_name
            """,
            (get_config().workspace_id, list(tiers)),
        )
        return cur.fetchall()


def _since(conn: psycopg.Connection, person_id: str) -> str | None:
    """Gmail `after:` date from the newest message already hydrated.

    Without this, every nightly run refetches a person's entire history. Dana
    alone is 54 messages; across a 15-person priority and standard tier that is
    hundreds of pointless round trips a day, and it grows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select max(at) mx from zarvis.touch
            where workspace_id = %s and person_id = %s and body is not null
            """,
            (get_config().workspace_id, person_id),
        )
        row = cur.fetchone()
    if not row or not row["mx"]:
        return None
    # A day of overlap, deliberately. Gmail's `after:` is date granular and a
    # message that arrives later the same day would otherwise be missed forever.
    import datetime as _dt

    return (row["mx"] - _dt.timedelta(days=1)).strftime("%Y/%m/%d")


def _people(conn: psycopg.Connection, needle: str | None, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        if needle:
            cur.execute(
                """
                select id, full_name from zarvis.person
                where workspace_id = %s and full_name ilike %s
                order by impact desc nulls last limit %s
                """,
                (get_config().workspace_id, f"%{needle}%", limit),
            )
        else:
            # Highest impact first: if this is capped, the people worth reading
            # should be the ones that got read.
            cur.execute(
                """
                select id, full_name from zarvis.person
                where workspace_id = %s
                order by impact desc nulls last, full_name limit %s
                """,
                (get_config().workspace_id, limit),
            )
        return cur.fetchall()


def _hydrate_person(
    conn: psycopg.Connection, svc, person: dict, *, months: int
) -> tuple[int, int]:
    """-> (touches created, bodies written)."""
    ws = get_config().workspace_id
    addresses = _addresses(conn, str(person["id"]))
    if not addresses:
        log.warning("%s has no real email address, skipping", person["full_name"])
        return 0, 0

    window = f"after:{since}" if (since := _since(conn, str(person["id"]))) else f"newer_than:{months}m"
    query = (
        f"{window} -in:chats "
        f"({' OR '.join(f'from:{a} OR to:{a}' for a in addresses)})"
    )
    ids: list[str] = []
    page = None
    while True:
        resp = execute(
            svc.users().messages().list(
                userId="me", q=query, maxResults=200, pageToken=page
            )
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    created = written = 0
    for message_id in dict.fromkeys(ids):
        message = execute(
            svc.users().messages().get(userId="me", id=message_id, format="full")
        )
        labels = set(message.get("labelIds") or [])
        if "DRAFT" in labels:
            continue

        direction = "outbound" if "SENT" in labels else "inbound"
        internal = message.get("internalDate")
        if not internal:
            continue
        import datetime as _dt

        at = _dt.datetime.fromtimestamp(int(internal) / 1000, tz=_dt.UTC)
        subject = _header(message, "Subject")
        body = (_plaintext_body(message) or "")[:MAX_BODY_CHARS]

        with conn.cursor() as cur:
            # Insert if the backfill never saw it, otherwise fill in the text.
            # `on conflict do update` here touches only subject and body, which
            # is the whole reason the grant is column-scoped: the ledger fields
            # stay append-only even on this path.
            cur.execute(
                """
                insert into zarvis.touch
                    (workspace_id, person_id, channel, direction, external_ref,
                     at, subject, body)
                values (%s, %s, 'email', %s, %s, %s, %s, %s)
                -- `touch_dedupe_idx` is a PARTIAL unique index, so the conflict
                -- target has to repeat its predicate or Postgres cannot match it.
                on conflict (workspace_id, channel, external_ref, person_id, direction)
                    where external_ref is not null
                do update set subject = excluded.subject, body = excluded.body
                returning (xmax = 0) as inserted
                """,
                (ws, person["id"], direction, message_id, at, subject, body),
            )
            row = cur.fetchone()
            if row and row["inserted"]:
                created += 1
            written += 1
    conn.commit()
    return created, written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--person", help="substring of their name")
    group.add_argument("--all", action="store_true")
    group.add_argument(
        "--queue", action="store_true",
        help="everyone in the priority and standard tiers. This is the nightly mode.",
    )
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS)
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args(argv)

    svc = gmail_service()
    total_c = total_w = 0
    with connect() as conn:
        if args.queue:
            people = _queue_people(conn, ("priority", "standard"))
        else:
            people = _people(conn, args.person, args.limit if args.all else 5)
        if not people:
            log.error("nobody matched")
            return 1
        for person in people:
            c, w = _hydrate_person(conn, svc, person, months=args.months)
            total_c += c
            total_w += w
            log.info(
                "%s: %d new touches, %d messages hydrated", person["full_name"], c, w
            )

    log.info("done: %d touches created, %d bodies written", total_c, total_w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
