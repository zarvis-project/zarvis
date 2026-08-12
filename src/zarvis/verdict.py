"""What Ryan actually did with the draft. The feedback loop.

    PYTHONPATH=src python -m zarvis.verdict
    PYTHONPATH=src python -m zarvis.verdict --dry-run

Zarvis has been writing drafts and learning nothing. `draft.verdict`,
`draft.final_body` and `draft.edit_distance` are columns that have existed since
the first migration and have never held a value, and `decision_case` has three
rows, all written by hand.

That is the whole learning loop missing. The original brief asked for a decision
tree that grows out of real executive decisions rather than being authored up
front, and the decisions are exactly this: sent as written, sent after edits, or
deleted. Nothing else Zarvis records is as informative, and unlike almost
everything else it CANNOT be backfilled. A draft deleted last Tuesday is simply
gone.

HOW A VERDICT IS DETERMINED
---------------------------
Gmail deletes a draft when it is sent, so a missing draft means one of several
things and telling them apart is the entire point:

  the draft is gone AND a matching message is in Sent   -> sent
  the draft is gone, nothing in Sent, gone < 5 days     -> undetermined, wait
  the draft is gone, nothing in Sent, gone > 5 days     -> rejected
  the draft is still there                              -> undecided, ask again

That middle line exists because Ryan schedules mail in Superhuman, and **a
scheduled message is invisible to this API**. It lives on Superhuman's servers
until it fires: not in Sent, not in Drafts, not in Trash, carrying no label,
absent from a whole-mailbox search including spam. Meanwhile the Zarvis draft it
was written from is deleted the moment he takes it over.

So on the day, a scheduled send and a binned draft are indistinguishable. They
stop being indistinguishable later, because a scheduled message lands in Sent
when it fires. Waiting costs a few days of a missing case-log row. Guessing
costs the opposite of the truth, recorded permanently, about mail he considers
already sent.

For a sent draft, the body Ryan actually sent is compared against what Zarvis
proposed. Identical means the draft was good enough to use as written. Heavily
edited means it was a starting point. Both are useful; conflating them is not,
which is why `sent_unedited` and `sent_edited` are separate verdicts and the
edit distance is stored rather than a boolean.

READ-ONLY
---------
This uses the agent's read credential, not the writer's. Determining what
happened requires no write access to the mailbox, and giving the observer a
write capability it does not need would undo the separation `deliver.py` exists
to maintain.
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Json

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.verdict")

# Below this similarity a "sent" draft is recorded as edited rather than used.
# 0.95 allows a signature or a changed greeting without calling it a rewrite.
UNEDITED_THRESHOLD = 0.95

# How long a draft may be missing from the mailbox before its absence counts as
# a rejection. Sized for "scheduled Friday afternoon to go out Monday morning",
# which is the longest ordinary case. Beyond this, a message that was going to
# send has sent.
GRACE_DAYS = 5


def _outstanding(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select d.id, d.person_id, d.gmail_draft_id, d.proposed_body,
                   d.created_at, d.queue_item_id, d.vanished_at, p.full_name,
                   (select i.value from zarvis.person_identity i
                     where i.person_id = p.id and i.kind = 'email'
                     order by i.created_at limit 1) as email
            from zarvis.draft d
            join zarvis.person p on p.id = d.person_id
            where d.workspace_id = %s
              and d.gmail_draft_id is not null
              and d.verdict is null
              -- Only drafts still live in the mailbox on Zarvis's own account.
              --
              -- `expired` means ZARVIS deleted it: `deliver._retire_superseded`
              -- removes a superseded duplicate so Ryan never finds two competing
              -- emails to the same person. Without this filter the poller then
              -- finds it gone from drafts, absent from sent, and records it as a
              -- REJECTION BY RYAN. On the first run that produced exactly that
              -- for Sam Okafor, and the case log would have learned that he
              -- bins drafts he never saw.
              --
              -- `skipped` is a draft the linter blocked or delivery withheld. It
              -- never reached the mailbox, so there is no verdict to observe.
              and d.status = 'pending'
            order by d.created_at
            """,
            (get_config().workspace_id,),
        )
        return cur.fetchall()


def _normalise(text: str) -> str:
    """Strip what a mail client adds so a diff measures Ryan's edits.

    Quoted history, signature blocks and whitespace churn would otherwise swamp
    the comparison and report every sent draft as heavily rewritten.
    """
    text = text or ""
    for marker in ("\nOn ", "\n> ", "\n-----Original", "\nFrom: ", "\n--\n"):
        if marker in text:
            text = text.split(marker)[0]
    return re.sub(r"\s+", " ", text).strip().lower()


def _sent_body(svc, email: str, since: datetime) -> str | None:
    """The message Ryan actually sent to this person, if there is one."""
    from .google_auth import execute
    from .ingest.gmail import _plaintext_body

    after = (since - timedelta(days=1)).strftime("%Y/%m/%d")
    resp = execute(
        svc.users().messages().list(
            userId="me", q=f"in:sent to:{email} after:{after}", maxResults=5
        )
    )
    for stub in resp.get("messages", []):
        message = execute(
            svc.users().messages().get(userId="me", id=stub["id"], format="full")
        )
        internal = message.get("internalDate")
        if internal and datetime.fromtimestamp(int(internal) / 1000, tz=UTC) < since:
            continue
        body = _plaintext_body(message)
        if body:
            return body
    return None



def _within_grace(conn: psycopg.Connection, row: dict, *, dry_run: bool) -> bool:
    """Has this draft been missing for less than the grace period?

    Stamps `vanished_at` on the first poll that cannot find it, then answers
    from that. Returns True while it is too early to call the disappearance a
    rejection.

    A scheduled send is the case this exists for, but it also covers a draft
    moved to another client, a Boomerang outbox, and Ryan forwarding the text
    into a different thread. All of them resolve themselves once the message
    actually goes out; none of them are rejections.
    """
    if row.get("vanished_at") is None:
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "update zarvis.draft set vanished_at = now(), updated_at = now() "
                    "where id = %s",
                    (row["id"],),
                )
            conn.commit()
            _ask_scheduled_or_binned(conn, row["full_name"])
        return True
    return datetime.now(UTC) - row["vanished_at"] < timedelta(days=GRACE_DAYS)


def _ask_scheduled_or_binned(conn: psycopg.Connection, name: str) -> None:
    """Ask once, on the day it vanished, while he still remembers.

    Cheaper and far more accurate than inferring from a mailbox that cannot see
    the answer. Best effort: a Slack outage must never fail the morning run.
    """
    try:
        from .slack import _dm_channels, say

        for channel in _dm_channels(conn):
            say(channel,
                f"Your draft to *{name}* is gone from the mailbox and nothing "
                f"matching it is in Sent. Scheduled, or binned? If you scheduled "
                f"it I will pick it up when it fires and record it as sent, so "
                f"there is nothing to do. Say so and I will log the reason.")
    except Exception as exc:  # noqa: BLE001
        log.debug("could not ask about the vanished draft: %s", exc)


def _operator_context(conn: psycopg.Connection, person_id: str) -> str | None:
    """Anything Ryan said about this person around the time he binned the draft.

    A bare "rejected" is the least useful thing this system can record. It says
    the draft was wrong without saying which kind of wrong, and the obvious
    reading is that the copy was bad. Usually it is not: the copy was fine and
    the RECIPIENT was wrong, or the timing was, or the situation had already
    moved.

    Ryan's own words are the correction, and he tends to give them in Slack
    around the same time. They are attached here as CONTEXT, explicitly hedged,
    because a note written near a rejection is evidence of the reason rather
    than proof of it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select body, observed_at from zarvis.signal
            where workspace_id = %s and person_id = %s
              and authored_by = 'operator' and source in ('slack', 'overlay')
              and observed_at > now() - interval '72 hours'
            order by observed_at desc limit 3
            """,
            (get_config().workspace_id, person_id),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    return " | ".join(f"{str(r['observed_at'])[:16]}: {r['body'][:200]}" for r in rows)


def poll(conn: psycopg.Connection, *, dry_run: bool) -> dict:
    from googleapiclient.errors import HttpError

    from .google_auth import execute, gmail_service

    counts = {"checked": 0, "still_open": 0, "sent_unedited": 0,
              "sent_edited": 0, "rejected": 0, "awaiting": 0}
    rows = _outstanding(conn)
    if not rows:
        log.info("no drafts awaiting a verdict")
        return counts

    svc = gmail_service()

    for row in rows:
        counts["checked"] += 1
        still_exists = True
        try:
            execute(svc.users().drafts().get(userId="me", id=row["gmail_draft_id"]))
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            still_exists = False

        if still_exists:
            counts["still_open"] += 1
            continue

        sent = _sent_body(svc, row["email"], row["created_at"]) if row["email"] else None

        if sent is None and _within_grace(conn, row, dry_run=dry_run):
            # Gone from drafts, nothing in sent, and it only just vanished.
            #
            # This is NOT yet a rejection. Ryan schedules mail in Superhuman,
            # and a scheduled message is held on Superhuman's servers until it
            # fires: not in Sent, not in Drafts, not in Trash, no label,
            # invisible to this API in every way. The Zarvis draft it came from
            # is deleted the moment he takes it over, so a scheduled send and a
            # binned draft look identical on the day.
            #
            # They stop looking identical later, because a scheduled message
            # lands in Sent when it fires. So the honest move is to wait. The
            # cost of waiting is a few days of a missing case-log entry; the
            # cost of guessing is teaching the model that Ryan bins mail he
            # actually sent.
            counts["awaiting"] += 1
            log.info("%s: draft gone, nothing sent yet. Scheduled, or binned? "
                     "Holding for up to %d days.", row["full_name"], GRACE_DAYS)
            continue

        if sent is None:
            # Gone for longer than anything could plausibly stay scheduled, and
            # still nothing in sent. Now it is a rejection, and the most
            # informative outcome there is: the situation was misjudged rather
            # than the wording.
            verdict, distance, final = "rejected", None, None
            counts["rejected"] += 1
            context = _operator_context(conn, str(row["person_id"]))
        else:
            context = None
            ratio = difflib.SequenceMatcher(
                None, _normalise(row["proposed_body"]), _normalise(sent)
            ).ratio()
            distance = round(1.0 - ratio, 4)
            if ratio >= UNEDITED_THRESHOLD:
                verdict, final = "sent_unedited", sent
                counts["sent_unedited"] += 1
            else:
                verdict, final = "sent_edited", sent
                counts["sent_edited"] += 1

        log.info(
            "%s: %s%s", row["full_name"], verdict,
            f" (edit distance {distance})" if distance is not None else "",
        )
        if dry_run:
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                update zarvis.draft
                set verdict = %s, final_body = %s, edit_distance = %s,
                    verdict_at = now(), status = %s, updated_at = now()
                where id = %s
                """,
                (verdict, final, distance,
                 "approved" if verdict.startswith("sent") else "skipped", row["id"]),
            )
            # The case log. This is the artifact the decision tree grows from,
            # so it records the proposal AND the outcome together: a verdict
            # without the draft it judged teaches nothing later.
            cur.execute(
                """
                insert into zarvis.decision_case
                  (workspace_id, person_id, queue_item_id, draft_id, situation,
                   chosen, rationale, verdict, reason_code, proposed_body,
                   final_body, edit_distance, options)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (get_config().workspace_id, row["person_id"], row["queue_item_id"],
                 row["id"], "draft delivered to Gmail", verdict,
                 # The rationale is Ryan's, where he gave one. Without this the
                 # case log records only that he said no.
                 context or ("no reason recorded" if verdict == "rejected" else None),
                 verdict, f"operator_{verdict}", row["proposed_body"], final,
                 distance,
                 Json({"gmail_draft_id": row["gmail_draft_id"],
                       "operator_context": context})),
            )
        conn.commit()

    if counts["rejected"] and not dry_run:
        _ask_why(conn)
    return counts


def _ask_why(conn: psycopg.Connection) -> None:
    """Ask, once, about rejections with no reason attached.

    The moment after Ryan bins a draft is when he knows exactly why and when it
    costs him one sentence to say. A week later the reason is gone and the case
    log keeps a rejection that teaches the wrong lesson forever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.full_name from zarvis.decision_case c
            join zarvis.person p on p.id = c.person_id
            where c.workspace_id = %s and c.verdict = 'rejected'
              and c.rationale = 'no reason recorded'
              and c.created_at > now() - interval '1 hour'
            """,
            (get_config().workspace_id,),
        )
        names = [r["full_name"] for r in cur.fetchall()]
    if not names:
        return
    try:
        from .slack import _dm_channels, say

        listed = ", ".join(names)
        for channel in _dm_channels(conn):
            say(channel,
                f"You binned the draft{'s' if len(names) > 1 else ''} to *{listed}*. "
                f"Worth a line on why, if it was not the writing. "
                f"Wrong person, wrong moment, situation moved, all useful and all "
                f"invisible to me otherwise.")
    except Exception as exc:  # noqa: BLE001 - never fail the run over a nudge
        log.debug("could not ask about rejections: %s", exc)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = get_config()
    with connect() as conn:
        counts = poll(conn, dry_run=args.dry_run or cfg.dry_run)
    log.info("%s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
