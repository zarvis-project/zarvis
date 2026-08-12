"""Put approved drafts into Gmail, as drafts. Never sends.

    PYTHONPATH=src python -m zarvis.deliver --dry-run
    PYTHONPATH=src python -m zarvis.deliver

THE NEVER-SEND GUARANTEE, HONESTLY STATED
------------------------------------------
This is the only module that holds a Gmail write credential, and it is worth
being exact about what protects you, because I got this wrong earlier in the
project and said so.

The original plan claimed the guarantee was structural: the agent holds
`gmail.readonly`, the writer holds `gmail.compose`, so the writer can draft but
not send. **That is false.** `gmail.compose` grants `messages.send` as well as
draft creation, and so does `gmail.modify`. There is no Gmail scope that permits
drafting and forbids sending. Any process able to create a draft in the mailbox
is able to send it.

So the guarantee is enforced three ways, none of them Google's:

  1. This module calls exactly one write endpoint, `users.drafts.create`, and
     nothing else.
  2. `tests/test_no_send.py` scans every module in `src/` for the Gmail send
     endpoints and fails the build if one appears. Deleting that test is a
     visible act in a diff.
  3. The deciding process (compose, review, room) holds read-only scopes and
     cannot reach this credential at all.

That is weaker than a scope boundary would have been, and it is the strongest
thing actually available. Ryan accepted it knowingly.

THREADING
---------
A reply that starts a new thread is worse than no reply. Gmail threads on
`threadId` plus the `In-Reply-To` and `References` headers, and both are needed:
`threadId` alone puts the message in the thread's conversation but clients like
Superhuman use the headers to place it correctly in the reply chain.

Where the draft answers a `reply_received` signal, that signal carries both the
Gmail `thread_id` and the RFC `Message-ID`, so the reply lands exactly where it
should. Where there is no thread, it is a fresh message and gets no headers.
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from email.message import EmailMessage

import psycopg

from .config import get_config, synthetic_email_exclusion
from .db import connect

log = logging.getLogger("zarvis.deliver")


def _pending(conn: psycopg.Connection, limit: int) -> list[dict]:
    """Drafts ready to go into the mailbox.

    `status = 'pending'` only. A draft the linter blocked is `skipped` with its
    reason recorded, and must never reach Gmail: the whole point of blocking it
    was that it asserted something the evidence did not support.
    """
    with conn.cursor() as cur:
        # Supersede, do not stack.
        #
        # Compose can legitimately write a second draft for the same situation:
        # the evidence moved, or the review changed the direction. Both then sit
        # pending, and delivering both puts two competing emails to the same
        # person in the mailbox. The newest carries the current decision, so it
        # wins and the older ones are closed as expired rather than left to be
        # picked up by a later run.
        cur.execute(
            """
            update zarvis.draft old
            set status = 'expired', updated_at = now()
            from zarvis.draft newer
            where old.workspace_id = %s
              and old.status = 'pending'
              and old.gmail_draft_id is null
              and newer.person_id = old.person_id
              and newer.status = 'pending'
              and newer.gmail_draft_id is null
              and newer.created_at > old.created_at
            """,
            (get_config().workspace_id,),
        )
        superseded = cur.rowcount
        if superseded:
            log.info("%d older draft(s) superseded by a newer one", superseded)

        cur.execute(
            """
            select d.id, d.person_id, d.subject, d.proposed_body, d.model_id,
                   p.full_name,
                   mgr.full_name as route_full_name,
                   -- coalesce, not two columns, because delivery only ever
                   -- needs one answer: the address this actually goes to. For
                   -- an agency's client that is the AGENCY. Compose already
                   -- wrote the body to the agency about their client, and an
                   -- address resolved independently here would put that body
                   -- in front of the client instead, which is the worst of
                   -- both: the routing rule honoured in the text and broken in
                   -- the envelope.
                   coalesce(
                     (select i.value from zarvis.person_identity i
                       where i.person_id = mgr.id and i.kind = 'email'
                         and i.superseded_at is null
                         /*SYNTH:i.value*/
                       order by i.created_at limit 1),
                     (select i.value from zarvis.person_identity i
                       where i.person_id = p.id and i.kind = 'email'
                         and i.superseded_at is null
                         /*SYNTH:i.value*/
                       order by i.created_at limit 1)
                   ) as email
            from zarvis.draft d
            join zarvis.person p on p.id = d.person_id
            left join zarvis.manages m on m.managed_id = p.id
            left join zarvis.person mgr on mgr.id = m.manager_id
            left join zarvis.queue_item q on q.id = d.queue_item_id
            where d.workspace_id = %s
              and d.status = 'pending'
              and d.gmail_draft_id is null
              -- The decision has to still stand.
              --
              -- A draft is written at one moment and delivered at another, and
              -- in between the board review may have decided to wait. On the
              -- first dry run four of six pending drafts were exactly that:
              -- composed in the morning, then declined by the review an hour
              -- later. Delivering them would put mail in Ryan's inbox that the
              -- decision layer had explicitly refused, which is the same defect
              -- as a room whose "no" gets ignored, one layer further down.
              --
              -- `suggested_action` is null for anyone the review said wait on.
              -- A draft with no queue item at all is a hand escalation, which
              -- Ryan asked for personally and which nothing has overruled.
              and (d.queue_item_id is null or q.suggested_action is not null)
            order by d.created_at
            limit %s
            """,
            (get_config().workspace_id, limit),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _thread_for(conn: psycopg.Connection, person_id: str) -> tuple[str | None, str | None]:
    """-> (gmail thread id, rfc message id) of their most recent inbound mail.

    Read from the `reply_received` signal rather than from `touch`, because the
    signal stores the RFC Message-ID and the touch does not. Without that header
    the reply threads loosely or not at all in some clients.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select value from zarvis.signal
            where workspace_id = %s and person_id = %s and kind = 'reply_received'
            order by observed_at desc limit 1
            """,
            (get_config().workspace_id, person_id),
        )
        row = cur.fetchone()
    if not row or not row["value"]:
        return None, None
    value = row["value"]
    return value.get("thread_id"), value.get("rfc_message_id")


def _build(to: str, subject: str, body: str, rfc_id: str | None) -> str:
    message = EmailMessage()
    message["To"] = to
    # Gmail wants the subject to match for threading, and a reply conventionally
    # carries the Re: prefix. Only add it when it is not already there.
    if rfc_id and subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message["Subject"] = subject or "(no subject)"
    if rfc_id:
        message["In-Reply-To"] = rfc_id
        message["References"] = rfc_id
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()




ON_HOLD_LABEL = "Zarvis/On hold"


def _label_id(svc, name: str) -> str | None:
    for label in svc.users().labels().list(userId="me").execute().get("labels", []):
        if label["name"] == name:
            return label["id"]
    return None


def _flag_unendorsed(conn: psycopg.Connection, svc) -> int:
    """Label drafts the room has since decided against. Never delete them.

    Ryan's call, and the right one. A draft that vanishes from the mailbox
    overnight is unsettling and removes his option to send it anyway; a draft
    that sits there unmarked silently implies Zarvis still recommends it. A
    label says "the situation moved" and leaves the decision where it belongs.

    The trigger is `suggested_action` going null, which is what the review
    writes when it decides to wait. Delivery already refuses to send those; this
    covers the ones delivered before the room changed its mind.
    """
    from googleapiclient.errors import HttpError

    label = _label_id(svc, ON_HOLD_LABEL)
    if not label:
        log.warning("label %r not found, skipping the on-hold pass", ON_HOLD_LABEL)
        return 0

    with conn.cursor() as cur:
        cur.execute(
            """
            select d.id, d.gmail_draft_id, p.full_name
            from zarvis.draft d
            join zarvis.person p on p.id = d.person_id
            join zarvis.queue_item q on q.id = d.queue_item_id
            where d.workspace_id = %s and d.status = 'pending'
              and d.gmail_draft_id is not null and d.verdict is null
              and q.suggested_action is null
            """,
            (get_config().workspace_id,),
        )
        stale = cur.fetchall()

    flagged = 0
    for row in stale:
        try:
            draft = svc.users().drafts().get(
                userId="me", id=row["gmail_draft_id"], format="minimal"
            ).execute()
            message_id = draft.get("message", {}).get("id")
            if not message_id:
                continue
            current = svc.users().messages().get(
                userId="me", id=message_id, format="minimal"
            ).execute()
            if label in (current.get("labelIds") or []):
                continue
            # `messages.modify` on the draft's own message. Explicitly allowed by
            # tests/test_no_send.py; trashing or deleting a message is not.
            svc.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [label]}
            ).execute()
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            continue
        flagged += 1
        log.info("flagged %s as on hold: the review no longer endorses it",
                 row["full_name"])
    return flagged


def _retire_superseded(conn: psycopg.Connection, svc, row: dict) -> str | None:
    """Clear an older delivered draft for the same person, if it is safe to.

    Compose writes a new draft whenever the evidence or the room's direction
    moves. Without this, the new one is delivered alongside the old and Ryan
    finds two competing emails to the same person in his mailbox. That already
    happened: Sam Okafor had drafts from 19:44 and 23:58 sitting side by
    side.

    The trap is that deleting a draft Ryan has EDITED destroys his work, and his
    edits are the most valuable output this system has. They are what
    `edit_distance` measures and what the case log learns from.

    So the old draft is fetched back from Gmail and compared with what Zarvis
    wrote. Untouched, it is safe to remove. Touched, it is his and the new draft
    is held rather than delivered, because two drafts is worse than a slightly
    stale one and silently overwriting him is worse than both.

    Returns a reason string when the NEW draft should be withheld.
    """
    from googleapiclient.errors import HttpError

    from .ingest.gmail import _plaintext_body

    with conn.cursor() as cur:
        cur.execute(
            """
            select id, gmail_draft_id, proposed_body
            from zarvis.draft
            where workspace_id = %s and person_id = %s and status = 'pending'
              and gmail_draft_id is not null and verdict is null and id <> %s
            """,
            (get_config().workspace_id, row["person_id"], row["id"]),
        )
        older = cur.fetchall()

    for old in older:
        try:
            existing = svc.users().drafts().get(
                userId="me", id=old["gmail_draft_id"], format="full"
            ).execute()
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            # Already gone: sent or deleted by hand. The verdict poller owns
            # that story, so leave the row alone for it to classify.
            continue

        current = _normalise(_plaintext_body(existing.get("message", {})) or "")
        if current and current != _normalise(old["proposed_body"]):
            return (
                f"an earlier draft is open in Gmail and has been edited; "
                f"leaving it alone rather than replacing your work"
            )

        try:
            svc.users().drafts().delete(userId="me", id=old["gmail_draft_id"]).execute()
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
        with conn.cursor() as cur:
            cur.execute(
                "update zarvis.draft set status = 'expired', updated_at = now() "
                "where id = %s",
                (old["id"],),
            )
        conn.commit()
        log.info("retired an untouched earlier draft for %s", row["full_name"])
    return None


def _normalise(text: str) -> str:
    import re

    text = text or ""
    # Quoted history and signature blocks are the client's, not Ryan's, and
    # would otherwise read as an edit on every single draft.
    for marker in ("\nOn ", "\n> ", "\n-----Original", "\nFrom: "):
        if marker in text:
            text = text.split(marker)[0]
    return re.sub(r"\s+", " ", text).strip().lower()


def deliver(conn: psycopg.Connection, *, dry_run: bool, limit: int) -> dict:
    from .google_auth import gmail_writer_service

    counts = {"considered": 0, "created": 0, "no_address": 0, "failed": 0,
              "withheld": 0}
    rows = _pending(conn, limit)
    svc = None if dry_run else gmail_writer_service()
    if not rows:
        log.info("no new drafts to deliver")

    for row in rows:
        counts["considered"] += 1
        if not row["email"]:
            log.warning("%s has no address, skipping", row["full_name"])
            counts["no_address"] += 1
            continue

        if not dry_run:
            withhold = _retire_superseded(conn, svc, row)
            if withhold:
                log.warning("%s: %s", row["full_name"], withhold)
                with conn.cursor() as cur:
                    cur.execute(
                        "update zarvis.draft set status = 'skipped', "
                        "skip_reason = %s, updated_at = now() where id = %s",
                        (withhold, row["id"]),
                    )
                conn.commit()
                counts["withheld"] = counts.get("withheld", 0) + 1
                continue

        thread_id, rfc_id = _thread_for(conn, str(row["person_id"]))
        raw = _build(row["email"], row["subject"] or "", row["proposed_body"], rfc_id)

        if dry_run:
            log.info(
                "DRY RUN: would draft to %s <%s>%s",
                row["full_name"], row["email"],
                f" in thread {thread_id}" if thread_id else " (new thread)",
            )
            continue

        payload: dict = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id

        try:
            created = svc.users().drafts().create(userId="me", body=payload).execute()
        except Exception as exc:  # noqa: BLE001 - one bad draft must not stop the rest
            log.error("%s: %s", row["full_name"], exc)
            counts["failed"] += 1
            continue

        with conn.cursor() as cur:
            cur.execute(
                "update zarvis.draft set gmail_draft_id = %s, updated_at = now() "
                "where id = %s",
                (created["id"], row["id"]),
            )
        conn.commit()
        counts["created"] += 1
        log.info(
            "drafted to %s <%s>%s",
            row["full_name"], row["email"],
            " (threaded)" if thread_id else "",
        )

    # Sweep for duplicates that predate this run.
    #
    # `_pending` only looks at UNDELIVERED drafts, so the retire step above only
    # fires when something new arrives. Anything that stacked up before it
    # existed would sit in the mailbox for ever. Sam Okafor had two.
    if not dry_run:
        counts["tidied"] = _tidy(conn, svc)
        counts["on_hold"] = _flag_unendorsed(conn, svc)

    return counts


def _tidy(conn: psycopg.Connection, svc) -> int:
    """Retire older delivered drafts where a newer one exists for the same person."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct on (d.person_id) d.id, d.person_id, p.full_name
            from zarvis.draft d
            join zarvis.person p on p.id = d.person_id
            where d.workspace_id = %s and d.status = 'pending'
              and d.gmail_draft_id is not null and d.verdict is null
            order by d.person_id, d.created_at desc
            """,
            (get_config().workspace_id,),
        )
        newest = cur.fetchall()

    tidied = 0
    for row in newest:
        before = _count_pending(conn, row["person_id"])
        if before < 2:
            continue
        if _retire_superseded(conn, svc, row) is None:
            tidied += before - _count_pending(conn, row["person_id"])
    return tidied


def _count_pending(conn: psycopg.Connection, person_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) c from zarvis.draft where workspace_id = %s "
            "and person_id = %s and status = 'pending' and gmail_draft_id is not null "
            "and verdict is null",
            (get_config().workspace_id, person_id),
        )
        return cur.fetchone()["c"]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args(argv)

    cfg = get_config()
    if cfg.kill_switch:
        log.warning("ZARVIS_KILL_SWITCH is set. Exiting.")
        return 0

    with connect() as conn:
        counts = deliver(conn, dry_run=args.dry_run or cfg.dry_run, limit=args.limit)
    log.info("%s", counts)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
