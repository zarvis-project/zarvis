"""The morning pipeline review. One room over the whole board.

    PYTHONPATH=src python -m zarvis.review
    PYTHONPATH=src python -m zarvis.review --dry-run --top 10

This is the decision layer. It runs after `queue` and before `compose`, and it
is what turns a ranked list into a set of decisions.

WHY THE BOARD AND NOT ONE ROOM PER PERSON
------------------------------------------
The deep room in `room.py` is seven sequential calls over one person's complete
record. Measured on Dana Whitfield that is ~90k input tokens and roughly $0.25.
Run per person across a 36-item queue it is $9 a day, and seven calls each
turns the 08:00 job into an hour of latency. Neither is acceptable for something
that has to finish before Ryan opens his laptop.

A real sales team does not hold thirty-six separate conference calls either.
They walk the board once. So this is four voices over every person at once, with
a condensed record each, and the deep room stays available for the two or three
the review itself says are genuinely hard.

WHAT IT REPLACES
----------------
`PLAY_INSTRUCTIONS` in compose.py: a dictionary mapping a play key to a canned
angle. That was the system pretending a precedence integer knows what to say. It
also produced nothing at all for Dana Whitfield, because no play in the library
covers a marketing partnership, so the most active relationship in the book got
silence rather than judgment.

After this, **plays are triggers, not instructions.** A play's job is to notice
that someone needs attention and say why. The room decides what to do about it,
and compose writes only what the room approved.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import psycopg
from psycopg.types.json import Json

from .config import get_config, synthetic_email_exclusion
from .db import connect
from .costs import attribute
from .llm import LLMError, complete

log = logging.getLogger("zarvis.review")

MAX_MESSAGES_PER_PERSON = 4
MAX_MESSAGE_CHARS = 1200


VOICES: list[tuple[str, str]] = [
    (
        "Analyst",
        "Walk the board. For each person in turn, state in two or three lines "
        "what the record shows and WHAT HAS CHANGED since the last contact. "
        "Dates only, no advice, no interpretation. Where the record is thin, say "
        "so: 'we have almost nothing on this person' is a finding the room needs. "
        "What you state is treated as settled for the rest of the meeting.",
    ),
    (
        "Skeptic",
        "Go through the board and name everyone who should NOT be contacted "
        "today, and argue it properly. Look for: we spoke very recently, we have "
        "unanswered outbounds already sitting with them, the reason to make "
        "contact is manufactured, or this simply is not worth Ryan's attention "
        "today against the rest of the list. Be specific and use dates. You are "
        "the only voice paid to protect Ryan's credibility and his morning, so "
        "do not be agreeable.\n\n"
        "ONE THING THAT IS NOT AN OBJECTION: an empty correspondence record. "
        "Where the trigger is a product event, a stalled onboarding, a parked "
        "account, a payment problem, a first contact is exactly the right move "
        "and having never emailed them is normal rather than disqualifying. On "
        "the first run of this meeting that reasoning knocked out nine of "
        "fifteen people, several of them wrongly. Reserve the thin-record "
        "objection for cases where the PLAY depends on a relationship that does "
        "not exist, such as reviving a dormant thread that was never warm.\n\n"
        "Also note that most of Ryan's selling happens on recorded calls. An "
        "empty email history often means the conversation happened somewhere "
        "else, not that nothing happened.",
    ),
    (
        "Closer",
        "For everyone the Skeptic did not knock out, name the single specific "
        "move. Channel and timing included, using the preferred channel in the "
        "record rather than assuming email. One concrete ask each. If you think "
        "the Skeptic was wrong about someone, say so and make the case.",
    ),
]

CHAIR = """You are the Chair. Decide the board.

For every person, in the order they were presented, produce a decision. Be
willing to say wait: a morning where Ryan sends three good emails beats one
where he sends ten forgettable ones, and the Skeptic's objections are there to
be used rather than noted.

Flag `deep_review` only where the situation is genuinely knotty and worth seven
voices on the full record: high value, conflicting signals, or a judgment call
that could go either way. Two or three at most, often zero.

Return ONLY a fenced JSON block, no prose:

```json
{
  "decisions": [
    {
      "n": 1,
      "name": "exact name as presented",
      "act": true,
      "approach": "2-3 sentences of direction for whoever writes the email: what to lead with, what to ask for, what to avoid. Empty string if act is false.",
      "channel": "email | whatsapp | linkedin | call | none",
      "timing": "now | this week | wait, and what for",
      "reason": "one sentence, why this decision",
      "deep_review": false
    }
  ],
  "unsupported_claims": ["voice: claim asserted without a dated source"]
}
```"""


def _board(conn: psycopg.Connection, top: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select q.id, q.person_id, q.rank, q.tier, q.rps, q.urgency,
                   q.decay_pressure, q.evidence,
                   p.full_name, p.impact, p.preferred_channel, p.style_notes,
                   pl.key as play_key, pl.name as play_name
            from zarvis.queue_item q
            join zarvis.person p on p.id = q.person_id
            join zarvis.play pl on pl.id = q.play_id
            where q.workspace_id = %s and q.status = 'open'
              and q.tier in ('priority', 'standard')
            order by q.rank
            limit %s
            """,
            (get_config().workspace_id, top),
        )
        rows = cur.fetchall()

        for row in rows:
            cur.execute(
                """
                select channel, direction, at, subject, body from zarvis.touch
                where workspace_id = %s and person_id = %s and body is not null
                order by at desc limit %s
                """,
                (get_config().workspace_id, row["person_id"], MAX_MESSAGES_PER_PERSON),
            )
            row["messages"] = list(reversed(cur.fetchall()))

            # Who introduced them, and who they introduced. Deliberately NOT in
            # the scoring: Ryan's call, and the right one, since an introduction
            # is context for a judgment rather than a quantity. An intro from a
            # Zenith evangelist is worth more than one from a stranger, and that
            # is exactly the sort of thing a room can weigh and a formula
            # cannot. The other person's impact and status come along so the
            # room can tell those two cases apart.
            cur.execute(
                """
                select 'incoming' as direction, o.full_name, o.impact,
                       o.path_override, l.kind
                from zarvis.link l
                join zarvis.person o on o.id = l.from_person
                where l.workspace_id = %(ws)s and l.to_person = %(pid)s
                  and l.status = 'active'
                union all
                select 'outgoing', o.full_name, o.impact, o.path_override, l.kind
                from zarvis.link l
                join zarvis.person o on o.id = l.to_person
                where l.workspace_id = %(ws)s and l.from_person = %(pid)s
                  and l.status = 'active'
                """,
                {"ws": get_config().workspace_id, "pid": row["person_id"]},
            )
            row["links"] = cur.fetchall()

            # Who to actually address.
            #
            # The person carrying the signal is frequently not the person to
            # email, and this has come up three times in one day: Marek Thorne
            # holds a seat inside Priya's agency and does not run his own account,
            # Lee Rankin is on the board only because JJ wants an introduction,
            # and 27 people have no real address at all because the product
            # manufactured one for an agency-run seat.
            #
            # `zarvis.manages` has held these edges since the first migration
            # and nothing has ever read it. Surfaced rather than acted on: the
            # room decides whether the manager is the right recipient, because
            # sometimes it is the account holder and sometimes it is both.
            cur.execute(
                """
                select mgr.full_name as manager, m.scope, mgr.impact,
                       (select i.value from zarvis.person_identity i
                         where i.person_id = mgr.id and i.kind = 'email'
                           /*SYNTH:i.value*/
                         limit 1) as manager_email
                from zarvis.manages m
                join zarvis.person mgr on mgr.id = m.manager_id
                where m.workspace_id = %s and m.managed_id = %s
                """,
                (get_config().workspace_id, row["person_id"]),
            )
            row["managers"] = cur.fetchall()

            # Does this person have an address of their own at all?
            cur.execute(
                """
                select count(*) c from zarvis.person_identity
                where person_id = %s and kind = 'email'
                  /*SYNTH:value*/
                """,
                (row["person_id"],),
            )
            row["reachable"] = (cur.fetchone()["c"] or 0) > 0
    return rows


def _render_board(rows: list[dict]) -> str:
    out: list[str] = []
    a = out.append
    for i, r in enumerate(rows, 1):
        a(f"## {i}. {r['full_name']}")
        a("")
        a(f"- Trigger: **{r['play_name']}** (`{r['play_key']}`)")
        a(f"- Rank {r['rank']} ({r['tier']}), score {r['rps']}, impact {r['impact']}, "
          f"urgency {r['urgency']} (decay {r['decay_pressure']})")
        if r["preferred_channel"]:
            a(f"- Prefers: {r['preferred_channel']}")
        if r["style_notes"]:
            a(f"- Voice note: {r['style_notes']}")
        for mgr in r.get("managers") or []:
            a(f"- **Managed by {mgr['manager']}** ({mgr['scope']}, impact "
              f"{mgr['impact']:.0f}{'' if mgr['manager_email'] else ', no address'})"
              f" — an email about this person may belong to them, not to "
              f"{r['full_name']}.")
        if not r.get("reachable"):
            a("- **No email address of their own.** Anything actioned here has "
              "to go through their manager or another channel.")
        for link in r.get("links") or []:
            # A decision-maker edge is ROUTING, not colour. Orsolya Moreau
            # carries the unapproved-request signal and Priya Raman holds the
            # budget, so the email about Orsolya's inactivity goes to JJ.
            # Direction matters and the first version got it backwards.
            # `incoming` means the edge points AT this person, so the other end
            # is the actor. `outgoing` means this person is the actor. Rendering
            # both the same way told the room that Orsolya was JJ's decision
            # maker, which is precisely inverted.
            if link.get("kind") in ("decision_maker_for", "manages_account"):
                if link["direction"] == "incoming":
                    a(f"- **{link['full_name']} is the decision maker for "
                      f"{r['full_name']}** — an email about this probably goes "
                      f"to {link['full_name']}, who can actually make it happen.")
                else:
                    a(f"- Decision maker for **{link['full_name']}**, who "
                      f"carries their own signals separately.")
                continue
            who = link["full_name"]
            verb = "Introduced by" if link["direction"] == "incoming" else "Introduced"
            # Flag whether the other end is someone Ryan still works with, since
            # an introduction from a live evangelist reads very differently from
            # one buried in the archived rolodex.
            standing = (
                f", impact {link['impact']:.0f}"
                if link["path_override"] != "archive" else ", archived contact"
            )
            a(f"- {verb}: **{who}**{standing}")
        kinds = [s.get("kind") for s in (r["evidence"] or {}).get("signals", [])]
        if kinds:
            a(f"- Live signals: {', '.join(kinds)}")
        for s in (r["evidence"] or {}).get("signals", []):
            if s.get("kind") == "note" and s.get("body"):
                a("")
                a(f"  > {s['body'][:1200].strip()}")
        a("")
        if r["messages"]:
            a(f"  Last {len(r['messages'])} messages:")
            a("")
            for m in r["messages"]:
                who = ("CALL SUMMARY" if m["channel"] == "meeting"
                       else "Ryan" if m["direction"] == "outbound" else r["full_name"])
                body = m["body"] or ""
                for cut in ("\nOn ", "\n> ", "\n-----Original", "\nFrom: "):
                    if cut in body:
                        body = body.split(cut)[0]
                a(f"  **{str(m['at'])[:10]} · {who} · {m['subject'] or '(no subject)'}**")
                a("  ```")
                a("  " + body.strip()[:MAX_MESSAGE_CHARS].replace("\n", "\n  "))
                a("  ```")
                a("")
        else:
            a("  No correspondence on file. The record is thin.")
            a("")
    return "\n".join(out)


SYSTEM = (
    "You are one voice in the morning pipeline review at Zenith Project, a "
    "LinkedIn outreach product founded by Ryan Miller. Zarvis has ranked who "
    "might need attention today; this meeting decides what actually happens.\n\n"
    "RULES\n"
    "- Every factual claim cites a date from the board. 'He went quiet in May' "
    "is fine. 'He's probably busy' is not.\n"
    "- 'Wait' is always available and is frequently right. Ryan's attention is "
    "the scarce resource, not the emails.\n"
    "- Nobody writes an email here. Composition happens afterwards.\n"
    "- Be terse. This is a stand-up, not an essay. Two or three lines per person."
)


def run_review(rows: list[dict], *, model: str | None) -> tuple[dict, str, dict]:
    board = _render_board(rows)
    spend = {"calls": 0, "input": 0, "output": 0, "reasoning": 0, "usd": 0.0}
    transcript: list[str] = []

    for name, brief in VOICES:
        prior = "\n\n".join(transcript) if transcript else "(you are speaking first)"
        user = (
            f"# The board\n\n{board}\n\n---\n\n# The meeting so far\n\n{prior}\n\n"
            f"---\n\n# You are: {name}\n\n{brief}\n\nSpeak now, as {name}."
        )
        c = complete(SYSTEM, user, max_tokens=2000, model=model)
        transcript.append(f"## {name}\n\n{c.text.strip()}")
        _tally(spend, c)
        log.info("  %-10s %6d in / %5d out (%d reasoning)", name,
                 c.input_tokens, c.output_tokens, c.reasoning_tokens)

    user = (
        f"# The board\n\n{board}\n\n---\n\n# The meeting\n\n"
        + "\n\n".join(transcript)
        + f"\n\n---\n\n{CHAIR}"
    )
    verdict = complete(SYSTEM, user, max_tokens=4000, model=model)
    _tally(spend, verdict)
    log.info("  %-10s %6d in / %5d out (%d reasoning)", "Chair",
             verdict.input_tokens, verdict.output_tokens, verdict.reasoning_tokens)
    transcript.append(f"## Chair\n\n{verdict.text.strip()}")

    blocks = re.findall(r"```json\s*(.*?)```", verdict.text, re.S)
    if not blocks:
        raise LLMError("the Chair did not return the JSON block")
    return json.loads(blocks[-1].strip()), "\n\n".join(transcript), spend


def _tally(spend: dict, c) -> None:
    spend["calls"] += 1
    spend["input"] += c.input_tokens
    spend["output"] += c.output_tokens
    spend["reasoning"] += c.reasoning_tokens
    spend["usd"] += c.cost_usd()


def apply_decisions(
    conn: psycopg.Connection, rows: list[dict], decisions: dict, *, model: str
) -> dict:
    """Write the room's direction where compose will read it."""
    by_index = {i: r for i, r in enumerate(rows, 1)}
    counts = {"act": 0, "wait": 0, "deep": 0, "unmatched": 0}

    with conn.cursor() as cur:
        for d in decisions.get("decisions", []):
            row = by_index.get(d.get("n"))
            if not row or row["full_name"].lower() != (d.get("name") or "").lower():
                # Match on BOTH index and name. A Chair that renumbers or drops a
                # line would otherwise write one person's direction onto another,
                # which is the worst possible failure here and completely silent.
                row = next(
                    (r for r in rows
                     if r["full_name"].lower() == (d.get("name") or "").lower()),
                    None,
                )
            if not row:
                log.error("decision for unknown person: %r", d.get("name"))
                counts["unmatched"] += 1
                continue

            act = bool(d.get("act"))
            counts["act" if act else "wait"] += 1
            if d.get("deep_review"):
                counts["deep"] += 1

            cur.execute(
                """
                update zarvis.queue_item
                set suggested_action = %s
                where id = %s
                """,
                (d.get("approach") if act else None, row["id"]),
            )
            cur.execute(
                """
                insert into zarvis.decision_case
                  (workspace_id, person_id, queue_item_id, situation, options,
                   chosen, rationale, verdict, reason_code)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    get_config().workspace_id, row["person_id"], row["id"],
                    json.dumps({"rank": row["rank"], "play": row["play_key"],
                                "tier": row["tier"]}, default=str),
                    Json({**d, "review_model": model, "mode": "board_review"}),
                    "act" if act else "wait",
                    d.get("reason"),
                    "accepted",
                    "review_act" if act else "review_wait",
                ),
            )
    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--model")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = get_config()
    if cfg.kill_switch:
        log.warning("ZARVIS_KILL_SWITCH is set. Exiting.")
        return 0

    with connect() as conn:
        rows = _board(conn, args.top)
        if not rows:
            log.warning("nothing in the priority or standard tiers")
            return 0
        log.info("reviewing %d people", len(rows))

        try:
            with attribute("review", mode="board", label=f"top{len(rows)}"):
                decisions, transcript, spend = run_review(rows, model=args.model)
        except LLMError as exc:
            log.error("%s", exc)
            return 1

        import pathlib
        out = pathlib.Path("escalations")
        out.mkdir(parents=True, exist_ok=True)
        (out / "review.md").write_text(transcript, encoding="utf-8")

        if args.dry_run or cfg.dry_run:
            log.info("DRY RUN, decisions not written")
            counts = {"act": sum(1 for d in decisions.get("decisions", []) if d.get("act"))}
        else:
            counts = apply_decisions(
                conn, rows, decisions, model=args.model or cfg.llm_model
            )

    print()
    for d in decisions.get("decisions", []):
        mark = "ACT " if d.get("act") else "wait"
        deep = "  [DEEP]" if d.get("deep_review") else ""
        print(f"  {mark}  {d.get('name','?')[:24]:<26} {d.get('timing','')[:28]:<30}"
              f" {d.get('reason','')[:60]}{deep}")
    for c in decisions.get("unsupported_claims") or []:
        print(f"  UNSUPPORTED  {c}")
    print()
    print(f"  {counts}")
    print(f"  cost {spend['calls']} calls · {spend['input']:,} in / {spend['output']:,} out"
          f" ({spend['reasoning']:,} reasoning) · ~${spend['usd']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
