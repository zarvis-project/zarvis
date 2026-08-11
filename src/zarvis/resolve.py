"""Identity resolution across mail, calendar and recordings.

    PYTHONPATH=src python -m zarvis.resolve --harvest
    PYTHONPATH=src python -m zarvis.resolve --harvest --days 365 --apply
    PYTHONPATH=src python -m zarvis.resolve --unknown

Calendar is the only source that carries a NAME, an EMAIL and an EXACT TIME in
the same record. Mail has addresses without reliable names. Fireflies has
recordings with participant emails that frequently do not match the address
someone emails from. That makes the calendar the join table between the other
two, and until now Zarvis threw it away: `ingest/calendar.py` resolves attendees
to people and silently discards every attendee it cannot match.

Three things fall out of using it properly.

**Alias discovery.** Someone accepts an invite as `s.okafor@company.example.com` and emails
from `sam@personal.example.com`. The calendar record contains the display name, so
the second address can be attached to the person we already know rather than
being invisible.

**Unknown attendees surface.** A person Ryan has met but who exists nowhere in
Zarvis appears in an invite with a name attached. Dana Whitfield was exactly this,
and he was only found because Ryan happened to mention him: the most active
thread in the mailbox belonged to someone the system had never heard of. This
makes that case automatic.

**Orphan recordings get attributed.** A Fireflies meeting whose participants
resolve to nobody can still be matched by START TIME against a calendar event,
and that event's attendees say who was in the room.

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
It does not create people. Ingest observes; it does not invent entities, and an
auto-created person from a calendar invite would put conference-room accounts,
recruiters and one-off vendors into the ranking. Unknown attendees are reported
for Ryan to promote into `seed/03-judgment.toml` if they matter.

Alias attachment requires an EXACT normalised full-name match, and `--apply` to
write. A wrong merge silently fuses two people's histories into one, which is
far worse than a missing alias and much harder to notice.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import psycopg

from .config import get_config
from .db import connect
from .google_auth import calendar_service, execute
from .ingest.calendar import _is_real_attendee

log = logging.getLogger("zarvis.resolve")

# Addresses that are never a person worth knowing.
NOISE = (
    "calendar-notification@", "noreply@", "no-reply@", "notifications@",
    "@resource.calendar.google.com", "@group.calendar.google.com",
)


def _normalise(name: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Deliberately crude. Anything cleverer starts matching 'Ryan Miller' to
    'Lee Rankin', and the cost of a false merge is two people's histories fused
    into one.
    """
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def _near_match(
    email: str, name: str, by_name: dict[str, str], names_by_id: dict[str, str]
) -> tuple[str, str, str] | None:
    """A match worth a human look, on evidence too weak to write on.

    Two signals, both required to be specific: a surname shared with exactly one
    known person, or that person's surname appearing inside the address itself.
    Either alone across a book this size would fire on coincidences.
    """
    local = email.split("@", 1)[0].lower()
    tokens = {w for w in _normalise(name).split() if len(w) > 2}

    hits: list[tuple[str, str, str]] = []
    for key, person_id in by_name.items():
        parts = [w for w in key.split() if len(w) > 2]
        if not parts:
            continue
        surname = parts[-1]
        if surname in tokens:
            hits.append((person_id, names_by_id[person_id], f"surname in '{name}'"))
        elif surname in local and len(surname) > 4:
            hits.append((person_id, names_by_id[person_id], "surname in the address"))
    # Ambiguity is not a suggestion, it is a coin toss.
    return hits[0] if len(hits) == 1 else None


def _events(svc, days: int) -> list[dict]:
    now = datetime.now(UTC)
    resp = execute(
        svc.events().list(
            calendarId="primary",
            timeMin=(now - timedelta(days=days)).isoformat(),
            timeMax=(now + timedelta(days=30)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
        )
    )
    return resp.get("items", [])


def _known(conn: psycopg.Connection) -> tuple[dict[str, str], dict[str, str]]:
    """-> (email -> person_id, normalised name -> person_id)."""
    ws = get_config().workspace_id
    with conn.cursor() as cur:
        cur.execute(
            "select person_id, lower(value) v from zarvis.person_identity "
            "where workspace_id = %s and kind = 'email'",
            (ws,),
        )
        by_email = {r["v"]: str(r["person_id"]) for r in cur.fetchall()}
        cur.execute(
            "select id, full_name from zarvis.person where workspace_id = %s", (ws,)
        )
        rows = cur.fetchall()

    by_name: dict[str, str] = {}
    seen = defaultdict(int)
    for r in rows:
        key = _normalise(r["full_name"])
        seen[key] += 1
        by_name[key] = str(r["id"])
    # An ambiguous name cannot be used to attach an address to anybody.
    for key, count in seen.items():
        if count > 1:
            by_name.pop(key, None)
            log.warning("name %r is not unique, excluded from matching", key)
    return by_email, by_name


def harvest(conn: psycopg.Connection, days: int, apply: bool) -> dict:
    svc = calendar_service()
    operator = get_config().google_impersonate
    by_email, by_name = _known(conn)
    with conn.cursor() as cur:
        cur.execute("select id, full_name from zarvis.person where workspace_id = %s",
                    (get_config().workspace_id,))
        names_by_id = {str(r["id"]): r["full_name"] for r in cur.fetchall()}

    aliases: list[tuple[str, str, str]] = []   # person_id, email, name
    suggested: dict[tuple[str, str], dict] = {}   # (person_id, email) -> evidence
    decided = _already_decided(conn)
    unknown: dict[str, dict] = {}
    counts = {"events": 0, "attendees": 0, "aliases": 0, "unknown": 0}

    for event in _events(svc, days):
        counts["events"] += 1
        for attendee in event.get("attendees") or []:
            email = (attendee.get("email") or "").lower()
            name = attendee.get("displayName") or ""
            if not _is_real_attendee(email, operator):
                continue
            if any(marker in email for marker in NOISE):
                continue
            counts["attendees"] += 1

            if email in by_email:
                continue

            key = _normalise(name)
            person_id = by_name.get(key) if key else None
            if person_id:
                aliases.append((person_id, email, name))
                by_email[email] = person_id
            elif (guess := _near_match(email, name, by_name, names_by_id)):
                # Reported, never written. `f.nakamura.work@example.com` displays
                # as "francis nakamura" and is plainly Frankie Nakamura across 37
                # meetings, which exact matching will never catch. Loosening the
                # automatic rule to find it would also start merging strangers
                # who share a surname, so the loose rule reports and Ryan
                # decides.
                # A question Ryan has already answered is never asked again.
                # Without this the same rejected guess comes back every night
                # forever, which is how a review inbox becomes noise people
                # learn to ignore.
                if (guess[0], email) in decided:
                    continue
                start = (event.get("start") or {}).get("dateTime")
                entry = suggested.setdefault(
                    (guess[0], email),
                    {"person": guess[1], "why": guess[2], "name": name,
                     "n": 0, "first": None, "last": None},
                )
                entry["n"] += 1
                if start:
                    if not entry["first"] or start < entry["first"]:
                        entry["first"] = start
                    if not entry["last"] or start > entry["last"]:
                        entry["last"] = start
                if name and not entry["name"]:
                    entry["name"] = name
            else:
                entry = unknown.setdefault(
                    email, {"name": name, "events": 0, "last": None}
                )
                entry["events"] += 1
                start = (event.get("start") or {}).get("dateTime", "")[:10]
                if start and (entry["last"] is None or start > entry["last"]):
                    entry["last"] = start
                if name and not entry["name"]:
                    entry["name"] = name

    counts["aliases"] = len(aliases)
    counts["suggested"] = len(suggested)
    counts["unknown"] = len(unknown)

    for person_id, email, name in aliases:
        log.info("alias: %s -> person %s (matched on %r)", email, person_id[:8], name)
        if apply:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into zarvis.person_identity
                        (workspace_id, person_id, kind, value)
                    values (%s, %s, 'email', %s)
                    on conflict (workspace_id, kind, value) do nothing
                    """,
                    (get_config().workspace_id, person_id, email),
                )
    if apply:
        conn.commit()
    elif aliases:
        log.warning("DRY RUN: %d aliases NOT written. Re-run with --apply.",
                    len(aliases))

    counts["asked"] = _record_questions(conn, suggested)
    if suggested:
        print("\n  Open questions recorded. Answer them with:\n")
        print("    python -m zarvis.resolve --pending")
        print("    python -m zarvis.resolve --yes <email> --note '...'")
        print("    python -m zarvis.resolve --no  <email> --note '...'\n")

    if unknown:
        print("\n  People in your calendar that Zarvis has never heard of:\n")
        ordered = sorted(unknown.items(), key=lambda kv: -kv[1]["events"])
        print(f"  {'email':<40} {'name':<26} {'meets':>6}  last")
        for email, info in ordered[:40]:
            print(f"  {email[:39]:<40} {(info['name'] or '')[:25]:<26} "
                  f"{info['events']:>6}  {info['last'] or ''}")
        print("\n  Not created automatically: ingest observes, it does not invent")
        print("  people. Add anyone who matters to seed/03-judgment.toml.\n")

    return counts


def _already_decided(conn: psycopg.Connection) -> set[tuple[str, str]]:
    """Questions with an answer, of either kind."""
    with conn.cursor() as cur:
        cur.execute(
            "select person_id, value from zarvis.identity_match "
            "where workspace_id = %s and status <> 'pending'",
            (get_config().workspace_id,),
        )
        return {(str(r["person_id"]), r["value"]) for r in cur.fetchall()}


def _record_questions(conn: psycopg.Connection, suggested: dict) -> int:
    """Persist the open questions, refreshing evidence on ones already asked."""
    if not suggested:
        return 0
    ws = get_config().workspace_id
    with conn.cursor() as cur:
        for (person_id, email), e in suggested.items():
            cur.execute(
                """
                insert into zarvis.identity_match
                    (workspace_id, person_id, kind, value, display_name, reason,
                     occurrences, first_seen, last_seen)
                values (%s, %s, 'email', %s, %s, %s, %s, %s, %s)
                on conflict (workspace_id, person_id, kind, value)
                do update set occurrences = excluded.occurrences,
                              last_seen   = excluded.last_seen,
                              reason      = excluded.reason
                where zarvis.identity_match.status = 'pending'
                """,
                (ws, person_id, email, e["name"], e["why"], e["n"],
                 e["first"], e["last"]),
            )
    conn.commit()
    return len(suggested)


def show_pending(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            select m.value, m.display_name, m.reason, m.occurrences,
                   m.first_seen, m.last_seen, p.full_name, p.primary_email
            from zarvis.identity_match m
            join zarvis.person p on p.id = m.person_id
            where m.workspace_id = %s and m.status = 'pending'
            order by m.occurrences desc
            """,
            (get_config().workspace_id,),
        )
        rows = cur.fetchall()

    if not rows:
        print("\n  No open identity questions.\n")
        return 0

    print(f"\n  {len(rows)} open question(s). Are these the same person?\n")
    for r in rows:
        print(f"  {r['full_name']}  <{r['primary_email'] or '?'}>")
        print(f"      is also  {r['value']}  " 
              f"(shown as {r['display_name'] or '?'})")
        print(f"      {r['occurrences']} shared meetings, "
              f"{str(r['first_seen'])[:10]} to {str(r['last_seen'])[:10]}")
        print(f"      matched because: {r['reason']}")
        print(f"      yes: --yes {r['value']}     no: --no {r['value']}")
        print()
    return len(rows)


def answer(conn: psycopg.Connection, email: str, *, yes: bool, note: str | None) -> int:
    ws = get_config().workspace_id
    with conn.cursor() as cur:
        cur.execute(
            """
            select m.id, m.person_id, m.value, m.display_name, p.full_name
            from zarvis.identity_match m
            join zarvis.person p on p.id = m.person_id
            where m.workspace_id = %s and m.value = %s and m.status = 'pending'
            """,
            (ws, email.lower()),
        )
        row = cur.fetchone()
        if not row:
            log.error("no pending question for %r", email)
            return 1

        cur.execute(
            """
            update zarvis.identity_match
            set status = %s, note = %s, decided_at = now()
            where id = %s
            """,
            ("confirmed" if yes else "rejected", note, row["id"]),
        )

        if yes:
            cur.execute(
                """
                insert into zarvis.person_identity
                    (workspace_id, person_id, kind, value)
                values (%s, %s, 'email', %s)
                on conflict (workspace_id, kind, value) do nothing
                """,
                (ws, row["person_id"], row["value"]),
            )
            # The note is context, not bookkeeping. "I call him Frankie but his
            # real name is Franc" is exactly the sort of thing a draft needs to
            # get the greeting right, so it becomes a signal rather than dying
            # in an admin table.
            if note:
                cur.execute(
                    """
                    insert into zarvis.signal
                        (workspace_id, person_id, source, kind, observed_at,
                         body, authored_by)
                    values (%s, %s, 'overlay', 'note', now(), %s, 'operator')
                    """,
                    (ws, row["person_id"],
                     f"Identity: also uses {row['value']}"
                     + (f" (shown as {row['display_name']})" if row["display_name"] else "")
                     + f". {note}"),
                )
    conn.commit()
    verdict = "CONFIRMED" if yes else "rejected"
    log.info("%s: %s is %s %s", verdict, email,
             "also" if yes else "NOT", row["full_name"])
    if not yes:
        log.info("recorded, so this will not be asked again")
    return 0


def attendees_at(conn: psycopg.Connection, when: datetime, window_minutes: int = 45):
    """Who was in the meeting that started around `when`.

    This is what rescues a recording whose participant list resolves to nobody:
    the calendar knows what was happening at that moment. Used by the Fireflies
    sweep to attribute orphan meetings.
    """
    svc = calendar_service()
    resp = execute(
        svc.events().list(
            calendarId="primary",
            timeMin=(when - timedelta(minutes=window_minutes)).isoformat(),
            timeMax=(when + timedelta(minutes=window_minutes)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
    )
    operator = get_config().google_impersonate
    by_email, _ = _known(conn)
    out: list[tuple[str, str]] = []
    for event in resp.get("items", []):
        for attendee in event.get("attendees") or []:
            email = (attendee.get("email") or "").lower()
            if not _is_real_attendee(email, operator):
                continue
            if email in by_email:
                out.append((by_email[email], email))
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", action="store_true",
                        help="find aliases and unknown attendees in the calendar")
    parser.add_argument("--unknown", action="store_true",
                        help="alias for --harvest, reporting only")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--apply", action="store_true",
                        help="actually write the exact-match aliases found")
    parser.add_argument("--pending", action="store_true",
                        help="show open 'are these the same person?' questions")
    parser.add_argument("--yes", metavar="EMAIL", help="confirm a pending match")
    parser.add_argument("--no", metavar="EMAIL", dest="no_",
                        help="reject a pending match, permanently")
    parser.add_argument("--note", help="optional context for either answer")
    args = parser.parse_args(argv)

    with connect() as conn:
        if args.pending:
            show_pending(conn)
            return 0
        if args.yes or args.no_:
            return answer(conn, args.yes or args.no_,
                          yes=bool(args.yes), note=args.note)
        if not (args.harvest or args.unknown):
            parser.error("nothing to do: pass --harvest, --pending, --yes or --no")
        counts = harvest(conn, args.days, args.apply and not args.unknown)
    log.info("%s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
