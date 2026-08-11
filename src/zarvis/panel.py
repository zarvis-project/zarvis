"""The conference room. A panel argues the hard cases; Ryan holds a costly veto.

    PYTHONPATH=src python -m zarvis.panel export --person okafor
    PYTHONPATH=src python -m zarvis.panel decide --person okafor --file panel.md
    PYTHONPATH=src python -m zarvis.panel decide --person okafor --file panel.md \
        --veto "he told me on a call he is out of budget until Q3"

WHAT THIS FIXES IN THE EXISTING DESIGN
---------------------------------------
Routine compose conflates two different jobs. `PLAY_INSTRUCTIONS` hands the
model a pre-made angle and asks it for prose, so the only thing ever decided is
wording. For ninety percent of cases that is correct and cheap. For the ones
that are actually hard, the wording was never the problem: the question is what
move to make at all, and that question is currently answered by a precedence
integer on a play row.

So this separates deciding from writing, the way a sales team already does. The
room decides the move. The salesperson writes the email.

WHY A PANEL AND NOT JUST A LONGER THINK
----------------------------------------
Because the failure modes of a single pass are known and one-directional: it
takes the play it was handed, finds evidence supporting it, and writes a
confident email. Nothing in that loop can produce "do not send anything this
week", which is frequently the right answer.

THE HONEST RISK, AND WHAT IS DONE ABOUT IT
-------------------------------------------
Five personas driven by one model is one model agreeing with itself in five
hats. Debate theatre reads as rigour and is really anchoring: the first
speaker frames the question and everyone else fills in supporting detail.

Three things push against that, and they are the reason this file is opinionated
about the prompt rather than just asking for "a discussion":

  1. **Roles have conflicting objectives, not different labels.** The Skeptic is
     told to WIN, not to raise concerns. If it cannot beat the proposal it must
     say so explicitly, which makes agreement informative instead of polite.
  2. **Every claim cites a dated signal or touch.** An assertion with no date
     attached is to be marked unsupported by the Chair. This is the same
     discipline as the assertion linter, moved upstream to the reasoning.
  3. **"Wait" is permanently on the ballot.** A room convened to decide how to
     re-engage someone will decide to re-engage them. Naming inaction as a
     first-class option is the only reliable counterweight.

THE VETO IS THE POINT
---------------------
Ryan can override the room, and doing so costs him a written justification. That
is not ceremony. From the original brief, the decision tree was supposed to grow
organically out of real executive decisions rather than be authored up front,
and a veto plus its reason is exactly that: a labelled example of judgment the
system did not have. It lands in `decision_case.outside_variables`, which is the
column for "he knew something the data could not see".

Accepting is recorded too. A decision only teaches you something if the times it
was followed are logged alongside the times it was not.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys

import psycopg
from psycopg.types.json import Json

from .config import get_config
from .db import connect
from .escalate import DEFAULT_DIR, _find_person, _gather, _prompts, _render

log = logging.getLogger("zarvis.panel")


ROLES = """\
## The room

Six voices. Play each one properly, in order, out loud. Do not summarise them
away. Each gets its own heading and speaks in the first person.

**1. The Historian.**
States only what the record below shows, with dates. No opinion, no
interpretation, no advice. Its job is to be the thing everyone else has to argue
against. Where the record is silent, it says so plainly, because "we do not
know" is the most commonly skipped fact in a sales conversation.

**2. The Skeptic.**
Argues that reaching out right now is the wrong move, and argues to win. Not
"here are some concerns" — an actual case: the relationship is colder than the
score suggests, the timing is bad, the last three touches went unanswered and a
fourth makes Ryan look like a pest, there is no real reason to make contact and
a manufactured one will be transparent. If after genuinely trying it cannot beat
the proposal, it must say "I cannot make this case" and explain what specifically
defeats it. An easy agreement here is worthless.

**3. The Counterparty.**
Speaks AS the recipient, from their chair. What lands in their inbox, on a
Tuesday, between other things? What do they already believe about Zenith and
about Ryan? What is their actual reason not to reply, and it is rarely
disinterest. What would make replying easy?

**4. The Closer.**
Names the single specific next step that moves this forward, and what has to be
true for it to happen. Rejects vague asks. "Let me know if you're interested" is
not a next step.

**5. The Realist.**
Challenges the channel and the timing. Is email even right for this person, or
is it WhatsApp, LinkedIn, a phone call, or their manager? Is today right, or is
the honest answer next month after something changes? Reads the preferred
channel and timezone in the record rather than assuming.

**6. The Chair.**
Synthesises. Must produce, in this order:
  a. The single recommended move, in one sentence.
  b. The strongest surviving objection to it, named and attributed.
  c. What would change the recommendation, concretely.
  d. Any claim made in the discussion that was NOT supported by a dated item in
     the record, listed and flagged. If someone asserted something, the Chair
     names it.

## Rules of the room

- Every factual claim cites a date from the record. "He went quiet in May" is
  fine, "he's clearly busy" is not, unless something dated says so.
- **"Do nothing this week" is always on the ballot** and must be genuinely
  considered, not dismissed in a clause. A room convened to decide how to
  re-engage someone will decide to re-engage them unless inaction is a real
  option with a real advocate.
- Disagreement is the product. If all six agree immediately, the Chair should
  treat that as a signal the question was framed too narrowly and say so.
- Nobody writes the email. That is a separate job, done after this decision.
"""


OUTPUT_CONTRACT = """\
## Output

Hold the discussion in full, then close with a fenced JSON block, exactly this
shape and nothing else inside the fence:

```json
{
  "recommendation": "one sentence, the move",
  "approach": "2-4 sentences of direction for whoever writes the email: what to lead with, what to ask for, what to avoid",
  "channel": "email | whatsapp | linkedin | call | none",
  "timing": "now | this week | wait, and what for",
  "confidence": "high | medium | low",
  "strongest_objection": "the best surviving argument against this",
  "would_change_my_mind": "what fact would flip this",
  "options_considered": [
    {"move": "...", "argued_by": "...", "rejected_because": "..."}
  ],
  "unsupported_claims": ["anything asserted without a dated source"]
}
```
"""


def _panel_packet(person: dict, data: dict) -> str:
    """The escalation packet, reframed as a decision problem rather than a
    writing task. Deliberately reuses the same evidence rendering so the room
    and the writer are looking at identical facts."""
    base = _render(person, data, _prompts())

    # Strip the writing instructions off the front of the escalation packet. The
    # room is not writing anything, and leaving "write the email" in the prompt
    # is the fastest way to get an email instead of a decision.
    marker = "## Who they are"
    body = base[base.index(marker):] if marker in base else base
    body = body.replace("\n---\n\nWrite the email.", "").rstrip()

    return f"""# Decision review: {person['full_name']}

Zarvis surfaced this person today and the situation is not routine, so it is
being taken to the room instead of drafted automatically.

**You are not writing an email.** You are deciding what move to make. Somebody
else writes the email afterwards, using your direction.

{ROLES}

---

{body}

---

{OUTPUT_CONTRACT}
"""


def _parse_decision(text: str) -> dict:
    """Pull the JSON block out of the room's transcript."""
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    if not blocks:
        blocks = re.findall(r"(\{.*\})", text, re.S)
    if not blocks:
        raise ValueError(
            "no JSON block found. The room must close with the fenced json "
            "object described in the packet."
        )
    return json.loads(blocks[-1].strip())


def do_export(args) -> int:
    with connect() as conn:
        person = _find_person(conn, args.person)
        if not person:
            log.error("no person matching %r", args.person)
            return 1
        data = _gather(conn, person, include_private=False)

    text = _panel_packet(person, data)
    out_dir = args.out or DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = person["full_name"].lower().replace(" ", "-")
    path = out_dir / f"{slug}-panel.md"
    path.write_text(text, encoding="utf-8")

    log.info("wrote %s (~%d tokens)", path, len(text) // 4)
    log.info("paste it in, save the whole reply, then:")
    log.info("  python -m zarvis.panel decide --person %r --file <reply>", args.person)
    return 0


def _record(
    conn: psycopg.Connection,
    person: dict,
    data: dict,
    decision: dict,
    *,
    veto: str | None,
    model: str,
) -> None:
    q = data.get("queue")
    chosen = "veto" if veto else decision.get("recommendation")

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.decision_case
              (workspace_id, person_id, queue_item_id, play_offered, situation,
               options, chosen, rationale, outside_variables, verdict, reason_code)
            values (%s, %s, %s,
                    (select play_id from zarvis.queue_item
                      where workspace_id = %s and person_id = %s and status = 'open'
                      limit 1),
                    %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                get_config().workspace_id, person["id"],
                q["id"] if q and "id" in q else None,
                get_config().workspace_id, person["id"],
                json.dumps({
                    "rank": q["rank"] if q else None,
                    "play": q["play"] if q else None,
                    "urgency": float(q["urgency"]) if q else None,
                }, default=str),
                Json({
                    "recommendation": decision.get("recommendation"),
                    "approach": decision.get("approach"),
                    "channel": decision.get("channel"),
                    "timing": decision.get("timing"),
                    "confidence": decision.get("confidence"),
                    "strongest_objection": decision.get("strongest_objection"),
                    "would_change_my_mind": decision.get("would_change_my_mind"),
                    "options_considered": decision.get("options_considered"),
                    "unsupported_claims": decision.get("unsupported_claims"),
                    "panel_model": model,
                }),
                chosen,
                veto or decision.get("approach"),
                # The column that exists for exactly this: what Ryan knew that
                # the data could not see. Empty on an accept, which is itself
                # worth recording.
                Json({"veto_reason": veto} if veto else {}),
                "vetoed" if veto else "accepted",
                "operator_veto" if veto else "panel_accepted",
            ),
        )

        # Hand the decision to the writer. `suggested_action` is read by compose
        # in place of the generic play instruction, so the room's direction
        # actually reaches the email rather than sitting in a log.
        if q and not veto:
            cur.execute(
                """
                update zarvis.queue_item
                set suggested_action = %s
                where workspace_id = %s and person_id = %s and status = 'open'
                """,
                (decision.get("approach"), get_config().workspace_id, person["id"]),
            )
    conn.commit()


def do_decide(args) -> int:
    text = args.file.read_text(encoding="utf-8")
    try:
        decision = _parse_decision(text)
    except (ValueError, json.JSONDecodeError) as exc:
        log.error("%s", exc)
        return 1

    with connect() as conn:
        person = _find_person(conn, args.person)
        if not person:
            log.error("no person matching %r", args.person)
            return 1
        data = _gather(conn, person, include_private=False)
        _record(conn, person, data, decision, veto=args.veto, model=args.model)

    print()
    print(f"  ROOM SAYS   {decision.get('recommendation')}")
    print(f"  channel     {decision.get('channel')}   timing {decision.get('timing')}"
          f"   confidence {decision.get('confidence')}")
    print(f"  objection   {decision.get('strongest_objection')}")
    if decision.get("unsupported_claims"):
        print("  UNSUPPORTED CLAIMS FLAGGED BY THE CHAIR:")
        for c in decision["unsupported_claims"]:
            print(f"    - {c}")
    print()
    if args.veto:
        print(f"  VETOED      {args.veto}")
        print("  Recorded in decision_case.outside_variables. No draft direction set.")
    else:
        print("  ACCEPTED    direction written to queue_item.suggested_action")
        print("  Next: PYTHONPATH=src python -m zarvis.compose")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="convene the room")
    e.add_argument("--person", required=True)
    e.add_argument("--out", type=pathlib.Path)
    e.set_defaults(fn=do_export)

    d = sub.add_parser("decide", help="record the outcome")
    d.add_argument("--person", required=True)
    d.add_argument("--file", type=pathlib.Path, required=True)
    d.add_argument("--model", default="unknown", help="which chat ran the room")
    d.add_argument(
        "--veto", metavar="REASON",
        help="override the room. Requires a justification, which is the entire "
             "value of the veto: it records what you knew that the data did not.",
    )
    d.set_defaults(fn=do_decide)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
