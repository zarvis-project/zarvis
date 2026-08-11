"""Meeting summaries from Fireflies, and transcripts on demand.

    PYTHONPATH=src python -m zarvis.fireflies --queue
    PYTHONPATH=src python -m zarvis.fireflies --person "priya raman"
    PYTHONPATH=src python -m zarvis.fireflies --transcript <id>

WHY THIS MATTERS MORE THAN IT LOOKS
------------------------------------
The first board review declined to act on nine of fifteen people, and its stated
reason for most of them was "no correspondence on file". That was accurate about
the email record and wrong about the relationship. Ryan's selling happens on
calls, and every one of them is recorded.

So Zarvis was reasoning about a book of relationships while systematically blind
to the channel where the actual conversations occur. No prompt tuning fixes
that.

WHAT IS STORED, AND WHAT IS NOT
-------------------------------
The summary lands in `touch.body`: overview, action items, and the bullet gist.
That is a few hundred tokens per meeting and is what a decision actually needs.

The full transcript is NOT stored. A one-hour call is tens of thousands of
tokens of mostly filler, and putting that in every review's context would blow
past the 200k long-context threshold where xAI doubles the rate on every token
in the request. `touch.source_ref` keeps the Fireflies id so any transcript can
be pulled on demand when something decides it needs the detail.

Meetings are `direction = 'mutual'`, which is why migration 20260809000009
widened that constraint. Recording a call as outbound would make it read as
another unanswered message from Ryan in exactly the two places that count them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

import psycopg

from .config import get_config
from .db import connect
from .hydrate import _addresses, _people, _queue_people

log = logging.getLogger("zarvis.fireflies")

ENDPOINT = "https://api.fireflies.ai/graphql"
SOURCE = "fireflies"

# `transcripts` accepts `participants` (email array) plus a date range, which is
# the same shape Gmail hydration already uses. `summary` is the payload; the
# transcript itself is fetched separately and only when asked for.
LIST_QUERY = """
query Meetings($participants: [String!], $fromDate: DateTime, $limit: Int) {
  transcripts(participants: $participants, fromDate: $fromDate, limit: $limit) {
    id
    title
    date
    dateString
    duration
    meeting_link
    participants
    summary {
      overview
      action_items
      bullet_gist
      keywords
      topics_discussed
    }
  }
}
"""

# Verified against the live schema by introspection on 2026-08-09, after the
# documented `[String]` turned out to be `[String!]` and cost a round trip.
# `date` is a Float of epoch milliseconds. `action_items` and `bullet_gist` are
# Strings, not lists, despite reading like lists.

TRANSCRIPT_QUERY = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    sentences { speaker_name text }
  }
}
"""


class FirefliesError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("ZARVIS_FIREFLIES_API_KEY")
    if not key:
        raise FirefliesError(
            "ZARVIS_FIREFLIES_API_KEY is not set. Get one from Fireflies "
            "Settings > Developer Settings."
        )
    return key


def _post(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {_api_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise FirefliesError(
            f"{exc.code}: {exc.read().decode('utf-8', errors='replace')[:400]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FirefliesError(f"could not reach Fireflies: {exc.reason}") from exc

    if payload.get("errors"):
        raise FirefliesError(json.dumps(payload["errors"])[:500])
    return payload.get("data") or {}


def _summary_text(meeting: dict) -> str:
    """Overview, action items and gist as one readable block.

    Deliberately plain prose rather than JSON: this ends up inside a prompt
    alongside quoted emails, and matching that shape costs fewer tokens and
    reads better than a nested object.
    """
    s = meeting.get("summary") or {}
    parts: list[str] = []
    if s.get("overview"):
        parts.append(s["overview"].strip())
    if s.get("action_items"):
        items = s["action_items"]
        if isinstance(items, list):
            items = "\n".join(f"- {i}" for i in items)
        parts.append("Action items:\n" + str(items).strip())
    if s.get("bullet_gist"):
        gist = s["bullet_gist"]
        if isinstance(gist, list):
            gist = "\n".join(f"- {g}" for g in gist)
        parts.append("Notes:\n" + str(gist).strip())
    return "\n\n".join(parts)


def _since(conn: psycopg.Connection, person_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select max(at) mx from zarvis.touch
            where workspace_id = %s and person_id = %s and channel = 'meeting'
            """,
            (get_config().workspace_id, person_id),
        )
        row = cur.fetchone()
    if not row or not row["mx"]:
        return None
    import datetime as _dt

    return (row["mx"] - _dt.timedelta(days=1)).isoformat()


def sync_person(conn: psycopg.Connection, person: dict, *, months: int) -> int:
    addresses = _addresses(conn, str(person["id"]))
    if not addresses:
        return 0

    import datetime as _dt

    from_date = _since(conn, str(person["id"])) or (
        _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=30 * months)
    ).isoformat()

    data = _post(
        LIST_QUERY,
        {"participants": addresses, "fromDate": from_date, "limit": 50},
    )
    meetings = data.get("transcripts") or []

    written = 0
    for m in meetings:
        summary = _summary_text(m)
        if not summary:
            # A meeting with no summary yet is one Fireflies is still
            # processing. Skip rather than storing an empty touch that would
            # look like a call with nothing said in it.
            continue
        at = m.get("date")
        if isinstance(at, (int, float)):
            at = _dt.datetime.fromtimestamp(at / 1000, tz=_dt.UTC)
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into zarvis.touch
                    (workspace_id, person_id, channel, direction, external_ref,
                     at, subject, body, source, source_ref)
                values (%s, %s, 'meeting', 'mutual', %s, %s, %s, %s, %s, %s)
                on conflict (workspace_id, channel, external_ref, person_id, direction)
                    where external_ref is not null
                do update set subject = excluded.subject, body = excluded.body,
                              source = excluded.source, source_ref = excluded.source_ref
                """,
                (
                    get_config().workspace_id, person["id"],
                    f"fireflies:{m['id']}", at, m.get("title"), summary,
                    SOURCE, m["id"],
                ),
            )
        written += 1
    conn.commit()
    return written


def fetch_transcript(transcript_id: str) -> str:
    """The full transcript, for when a summary is not enough."""
    data = _post(TRANSCRIPT_QUERY, {"id": transcript_id})
    t = data.get("transcript") or {}
    lines = [
        f"{s.get('speaker_name') or '?'}: {s.get('text') or ''}"
        for s in (t.get("sentences") or [])
    ]
    return f"# {t.get('title')} ({str(t.get('date'))[:10]})\n\n" + "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--person")
    group.add_argument("--queue", action="store_true")
    group.add_argument("--transcript", metavar="ID", help="print one full transcript")
    parser.add_argument("--months", type=int, default=12)
    args = parser.parse_args(argv)

    try:
        if args.transcript:
            print(fetch_transcript(args.transcript))
            return 0

        total = 0
        with connect() as conn:
            people = (
                _queue_people(conn, ("priority", "standard"))
                if args.queue
                else _people(conn, args.person, 5)
            )
            for person in people:
                try:
                    n = sync_person(conn, person, months=args.months)
                except FirefliesError as exc:
                    log.error("%s: %s", person["full_name"], exc)
                    continue
                total += n
                if n:
                    log.info("%s: %d meetings", person["full_name"], n)
        log.info("done: %d meeting summaries stored", total)
    except FirefliesError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
