"""The morning brief. What Zarvis did, and what it wants from Ryan.

    PYTHONPATH=src python -m zarvis.brief
    PYTHONPATH=src python -m zarvis.brief --dry-run

Runs last in the daily chain, after delivery.

WHY THIS EXISTS
---------------
Without it the 08:00 run does everything and says nothing. Drafts appear in
Superhuman with no indication of what was decided, what was declined, or why,
and Ryan has to go looking for the output of a system whose entire purpose is
that he should not have to go looking.

WHAT IT DELIBERATELY DOES NOT DO
---------------------------------
It does not restate the board. That is available on demand by asking, and a
message listing fifteen people every morning is the kind of notification a
person mutes inside a week. This is short by design: what needs your hands,
what changed, what it decided against, and what it cost.

The "declined" section is the one worth reading. A morning where Zarvis sends
nothing is a real outcome, and a brief that only reported action would quietly
train Ryan to equate silence with failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import psycopg

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.brief")


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(hours=18)


def gather(conn: psycopg.Connection) -> dict:
    ws = get_config().workspace_id
    since = _since()
    out: dict = {}

    with conn.cursor() as cur:
        # Drafts waiting in the mailbox. The only section that asks for hands.
        cur.execute(
            """
            select p.full_name, d.subject, q.suggested_action
            from zarvis.draft d
            join zarvis.person p on p.id = d.person_id
            left join zarvis.queue_item q on q.id = d.queue_item_id
            where d.workspace_id = %s and d.status = 'pending'
              and d.gmail_draft_id is not null and d.verdict is null
            order by d.created_at desc
            """,
            (ws,),
        )
        out["waiting"] = cur.fetchall()

        # What it decided against, and why. The section that stops "no drafts"
        # from reading as "nothing happened".
        # LATEST decision per person only, and never someone who has a draft
        # waiting. The first version reported both review runs, so Robin
        # Kelly appeared in the inbox AND in "left alone" in the same message.
        # A brief that contradicts itself is worse than no brief: it teaches
        # Ryan not to trust the parts he cannot immediately verify.
        cur.execute(
            """
            select distinct on (c.person_id)
                   p.full_name,
                   c.options->>'reason' as reason,
                   (c.options->>'deep_review') = 'true' as deep,
                   c.reason_code
            from zarvis.decision_case c
            join zarvis.person p on p.id = c.person_id
            where c.workspace_id = %s and c.created_at > %s
              and c.reason_code in ('review_wait', 'review_act')
              and not exists (
                select 1 from zarvis.draft d
                where d.person_id = c.person_id and d.status = 'pending'
                  and d.gmail_draft_id is not null and d.verdict is null
              )
            order by c.person_id, c.created_at desc
            """,
            (ws, since),
        )
        out["declined"] = [
            r for r in cur.fetchall() if r["reason_code"] == "review_wait"
        ]

        # Verdicts on yesterday's drafts. The learning loop reporting in.
        cur.execute(
            """
            select p.full_name, d.verdict, d.edit_distance
            from zarvis.draft d
            join zarvis.person p on p.id = d.person_id
            where d.workspace_id = %s and d.verdict_at > %s
            order by d.verdict_at
            """,
            (ws, since),
        )
        out["verdicts"] = cur.fetchall()

        cur.execute(
            """
            select p.full_name, s.kind
            from zarvis.signal s
            join zarvis.person p on p.id = s.person_id
            where s.workspace_id = %s and s.created_at > %s
              and s.kind in ('reply_received', 'meeting_held', 'payment_issue')
            order by s.observed_at desc limit 8
            """,
            (ws, since),
        )
        out["new_signals"] = cur.fetchall()

        cur.execute(
            "select coalesce(sum(cost_usd), 0) c, count(*) n from zarvis.llm_call "
            "where workspace_id = %s and at > %s",
            (ws, since),
        )
        out["cost"] = cur.fetchone()

        cur.execute(
            """
            select count(*) c from zarvis.identity_match
            where workspace_id = %s and status = 'pending'
            """,
            (ws,),
        )
        out["questions"] = cur.fetchone()["c"]

    return out


def render(data: dict) -> str:
    lines: list[str] = ["*Good morning.*", ""]

    if data["waiting"]:
        lines.append(f"*{len(data['waiting'])} draft(s) in your inbox*")
        for row in data["waiting"]:
            lines.append(f"• *{row['full_name']}* — {row['subject'] or '(no subject)'}")
            if row["suggested_action"]:
                lines.append(f"   _{row['suggested_action'][:120]}_")
        lines.append("")
    else:
        lines.append("*No drafts today.* Nothing met the bar, which is a decision "
                     "rather than an absence.")
        lines.append("")

    if data["verdicts"]:
        lines.append("*What you did with yesterday's*")
        for row in data["verdicts"]:
            detail = (
                f" (edit distance {float(row['edit_distance']):.2f})"
                if row["edit_distance"] is not None else ""
            )
            lines.append(f"• {row['full_name']}: {row['verdict']}{detail}")
        lines.append("")

    if data["new_signals"]:
        lines.append("*New since yesterday*")
        for row in data["new_signals"]:
            lines.append(f"• {row['full_name']} — {row['kind'].replace('_', ' ')}")
        lines.append("")

    if data["declined"]:
        deep = [r for r in data["declined"] if r["deep"]]
        lines.append(f"*Considered and left alone: {len(data['declined'])}*")
        # Three, not all of them. The full list is one question away and a
        # fifteen-line morning message gets muted.
        for row in data["declined"][:3]:
            lines.append(f"• {row['full_name']} — _{(row['reason'] or '')[:80]}_")
        if len(data["declined"]) > 3:
            lines.append(f"• _…and {len(data['declined']) - 3} more. Ask me why "
                         f"about any of them._")
        if deep:
            names = ", ".join(r["full_name"] for r in deep)
            lines.append(f"🧠 Worth a full room: *{names}*")
        lines.append("")

    if data["questions"]:
        lines.append(f"_{data['questions']} identity question(s) waiting — "
                     f"`resolve --pending`_")

    cost = data["cost"]
    lines.append(
        f"_{cost['n']} model calls, ${float(cost['c']):.3f}._"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = get_config()
    with connect() as conn:
        data = gather(conn)
        text = render(data)

        if args.dry_run or cfg.dry_run:
            print(text)
            return 0

        # Imported here so a missing Slack token degrades to "no brief" rather
        # than failing the morning run that produced the work.
        try:
            from .slack import _dm_channels, say

            for channel in _dm_channels(conn):
                say(channel, text)
            log.info("brief sent")
        except Exception as exc:  # noqa: BLE001
            log.error("could not send the brief: %s", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
