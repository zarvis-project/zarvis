"""The room, run over the API. Six voices, then a judge, then a draft.

    PYTHONPATH=src python -m zarvis.room --person "dana whitfield"
    PYTHONPATH=src python -m zarvis.room --person "dana whitfield" --draft

Each voice is a separate API call that sees the same record plus everything said
before it. They are NOT given different slices of the context: Ryan's objection
to partitioning is decisive, and it is that agents holding different facts spend
the debate arguing about whether something happened instead of what to do about
it. Disagreement about the record is noise. Disagreement about the move is the
product.

ORDER IS THE MECHANISM
----------------------
This is proposal-then-attack, not a free-for-all. The Closer has to commit to a
specific move, and then two voices with incompatible objectives try to break it.
A round table where everyone speaks at once produces six overlapping opinions
and no decision.

  1 Historian    establishes the record. Speaks first, and once it has spoken
                 the facts are SETTLED. Nobody relitigates them.
  2 Mapper       what can this person actually authorise (MEDDIC's economic
                 buyer vs champion distinction).
  3 Counterparty their chair, their inbox, their reason not to reply.
  4 Closer       commits to a move, with channel and timing.
  5 Skeptic      argues to WIN that we should not act.
  6 Pre-mortem   assumes we DID act and it failed. What killed it.
  7 Judge        synthesis, citation audit, and the Tenth Man rule.

Skeptic and Pre-mortem look redundant and are not. One argues do not send. The
other assumes you sent and finds what it broke. Only the second catches an email
that works today and sets an expectation you regret in November.

WHY SEQUENTIAL AND WHY A JUDGE
-------------------------------
The measured failure mode of multi-agent debate is sycophancy: agents reinforce
each other, converge early, and produce consensus that reads as rigour. The
research is blunt that unguided homogeneous debate can be WORSE than a single
model self-correcting, and that a sycophantic judge negates the whole structure.

So: every voice has a different objective, the Skeptic is told to win rather
than to raise concerns, every claim must cite a dated item, and if the room
converges before the Judge speaks the Judge must appoint a dissenter and take
the objection seriously. Agreement is only informative when disagreement was
available.
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
from .escalate import _find_person, _gather, _prompts, _render
from .lint import blocking, check, sweep
from .costs import attribute
from .llm import LLMError, complete

log = logging.getLogger("zarvis.room")

VOICES: list[tuple[str, str]] = [
    (
        "Historian",
        "State what the record shows, with dates. No opinion, no advice, no "
        "interpretation. Where the record is silent, say so plainly, because "
        "'we do not know' is the most commonly skipped fact in a sales "
        "conversation. Everything you state is treated as SETTLED for the rest "
        "of this discussion, so be careful and be complete. If two sources "
        "disagree, say which is the record and which is recollection.",
    ),
    (
        "Stakeholder Mapper",
        "Establish what this person can actually authorise. Are they the "
        "economic buyer, a champion, a user, a gatekeeper, or a distribution "
        "partner? Whose budget would this touch? Who else inside their world "
        "matters, and does the record name them? Do not propose a move. The "
        "point is that an email to a champion and an email to a buyer are "
        "different emails.",
    ),
    (
        "Counterparty",
        "Speak AS them, in the first person, from their chair. What lands in "
        "your inbox on a Tuesday between other things? What do you already "
        "believe about Ryan and about Zenith? What is your real reason not to "
        "reply, and it is rarely disinterest. What would make replying easy? "
        "Be honest, including where Ryan has been annoying or slow.",
    ),
    (
        "Closer",
        "Commit to ONE specific move. Name the channel and the timing, using "
        "the record's stated preferences rather than assuming email. Say what "
        "has to be true for it to work. Reject vague asks: 'let me know if "
        "you're interested' is not a next step. The others are about to attack "
        "this, so make it defensible rather than safe.",
    ),
    (
        "Skeptic",
        "Argue that the Closer's move is wrong and that we should not act, and "
        "argue to WIN. Not a list of concerns. An actual case: the timing is "
        "wrong, the ask is premature, the relationship cannot carry it, we "
        "spoke too recently, the reason for contact is manufactured and will "
        "read that way, this is not worth Ryan's attention today against "
        "everything else on his list. If after genuinely trying you cannot beat "
        "it, say 'I cannot make this case' and explain precisely what defeats "
        "you. Easy agreement here is worthless.",
    ),
    (
        "Pre-mortem",
        "Assume the Closer's move was made. It is now eight weeks later and "
        "this relationship is dead or damaged. Work backwards: what killed it? "
        "You are not arguing against acting, you are finding what acting breaks. "
        "Look for expectations this sets, precedents it establishes, and things "
        "it makes harder to ask for later.",
    ),
]

JUDGE = """You are the Judge. You did not participate and you have no stake in
any position. Do four things, in order, and do not skip the fourth.

1. **The move.** One sentence. What should Ryan do.
2. **The strongest surviving objection**, named and attributed to whoever made it.
3. **What would change this**, concretely. A fact that would flip it.
4. **Citation audit.** Go back through everything said and list every factual
   claim that was NOT supported by a dated item in the record. Name the voice
   that made it. If a voice asserted something about this person's state of mind,
   their budget, or their intentions without a source, that goes on the list.

**The Tenth Man rule.** If the voices largely agreed, that is a warning rather
than a confirmation. Appoint the voice with the weakest position, argue its case
properly yourself, and only then decide. Say that you did this.

Close with a fenced JSON block, exactly this shape:

```json
{
  "recommendation": "one sentence, the move",
  "approach": "2-4 sentences of direction for whoever writes the email: what to lead with, what to ask for, what to avoid",
  "channel": "email | whatsapp | linkedin | call | none",
  "timing": "now | this week | wait, and what for",
  "confidence": "high | medium | low",
  "strongest_objection": "the best surviving argument against this",
  "would_change_my_mind": "what fact would flip this",
  "converged_early": true,
  "tenth_man": "the case you argued if the room converged, else null",
  "unsupported_claims": ["voice: claim asserted without a dated source"]
}
```"""


def _context(person: dict, data: dict) -> str:
    """The shared record. Same rendering as the escalation packet, minus the
    instructions to write an email, because the room is not writing one."""
    base = _render(person, data, "")
    marker = "## Who they are"
    body = base[base.index(marker):] if marker in base else base
    return body.replace("\n---\n\nWrite the email.", "").rstrip()


def run_room(person: dict, data: dict, *, model: str | None) -> tuple[dict, str, dict]:
    """-> (decision, transcript, cost).

    The cost dict exists because the first run of this was not measured. Seven
    calls over a growing transcript is the most expensive thing Zarvis does, and
    reconstructing it afterwards from a markdown file is guesswork that misses
    reasoning tokens entirely. Measure it at the point it is spent.
    """
    spend = {"calls": 0, "input": 0, "output": 0, "reasoning": 0, "usd": 0.0}
    record = _context(person, data)
    system = (
        "You are one voice in a sales deal review for Zenith Project, a "
        "LinkedIn outreach product founded by Ryan Miller. Speak only as the "
        "role you are given. Be concise and concrete: this is a working "
        "meeting, not an essay.\n\n"
        "RULES OF THE ROOM\n"
        "- Every factual claim cites a date from the record. 'He went quiet in "
        "May' is fine. 'He's clearly busy' is not, unless something dated says "
        "so.\n"
        "- 'Do nothing this week' is always a legitimate conclusion and must be "
        "genuinely available, not dismissed in a clause.\n"
        "- Do not write the email. That is a separate job done afterwards."
    )

    transcript: list[str] = []
    for name, brief in VOICES:
        prior = "\n\n".join(transcript) if transcript else "(you are speaking first)"
        user = (
            f"# The record\n\n{record}\n\n"
            f"---\n\n# The discussion so far\n\n{prior}\n\n"
            f"---\n\n# You are: {name}\n\n{brief}\n\n"
            f"Speak now, as {name}."
        )
        completion = complete(system, user, max_tokens=1400, model=model)
        text = completion.text.strip()
        transcript.append(f"## {name}\n\n{text}")
        log.info("  %-18s %5d tok in / %4d out", name,
                 completion.input_tokens, completion.output_tokens)

    user = (
        f"# The record\n\n{record}\n\n---\n\n# The discussion\n\n"
        + "\n\n".join(transcript)
        + f"\n\n---\n\n{JUDGE}"
    )
    verdict = complete(system, user, max_tokens=2000, model=model)
    _tally(spend, verdict)
    log.info("  %-18s %6d in / %5d out (%d reasoning)", "Judge",
             verdict.input_tokens, verdict.output_tokens, verdict.reasoning_tokens)
    log.info("  room total: %d calls, %d in / %d out, ~$%.4f",
             spend["calls"], spend["input"], spend["output"], spend["usd"])
    transcript.append(f"## Judge\n\n{verdict.text.strip()}")

    blocks = re.findall(r"```json\s*(.*?)```", verdict.text, re.S)
    if not blocks:
        raise LLMError("the Judge did not return the JSON block")
    return json.loads(blocks[-1].strip()), "\n\n".join(transcript)


def draft_from(person: dict, data: dict, decision: dict, *, model: str | None) -> str:
    """Write the email the room decided on. A separate call on purpose: the
    room's job was judgment, and asking the same context to also produce prose
    is what collapses the two jobs back together."""
    record = _context(person, data)
    system = (
        "You draft emails as Ryan Miller, founder of Zenith Project.\n\n"
        + _prompts()
        + "\n\n# Hard constraints\n"
        "1. Never state a fact that is not in the record below.\n"
        "2. No em dashes. Commas and periods.\n"
        "3. Do not promise how long anything takes. Ask for their time instead.\n"
        "4. Close with a signoff, `Best,` or `Cheers,` then `Ryan`, no period.\n"
        "5. Do not name a detail just to prove you read it. A specific earns its "
        "place only if it changes what Ryan is asking for.\n"
        "6. Three to six sentences.\n\n"
        'Return JSON only: {"subject": "...", "body": "..."}'
    )
    user = (
        f"# The record\n\n{record}\n\n---\n\n"
        f"# The decision review concluded\n\n"
        f"**Move:** {decision.get('recommendation')}\n\n"
        f"**Direction:** {decision.get('approach')}\n\n"
        f"**Channel:** {decision.get('channel')}  ·  "
        f"**Timing:** {decision.get('timing')}\n\n"
        f"Follow that direction. Write the email."
    )
    completion = complete(system, user, max_tokens=1200, model=model)
    text = completion.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text).get("body", "")


def declined(decision: dict) -> bool:
    """Did the room decide NOT to act?

    Extracted because two callers need it and a copy that drifts would drift in
    the dangerous direction: a caller that reads "wait" as "act" drafts an email
    the room specifically decided against, which is the exact
    deciding-versus-writing collapse this module exists to prevent.

    Both fields matter. `channel: none` is the room saying there is no move at
    all; `timing: wait two weeks` is a real move at the wrong moment. Either one
    is a no today.
    """
    return (
        (decision.get("channel") or "").lower() == "none"
        or (decision.get("timing") or "").lower().startswith("wait")
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person", required=True)
    parser.add_argument(
        "--mode", default="requested", choices=("requested", "recommended"),
        help="who asked for this room. 'recommended' means the board review "
             "flagged it; 'requested' means Ryan did.",
    )
    parser.add_argument("--model", help="override the configured model")
    parser.add_argument("--draft", action="store_true", help="also write the email")
    parser.add_argument(
        "--force", action="store_true",
        help="draft even if the room decided to wait. Overriding the room is a "
             "decision; the veto path in panel.py records why.",
    )
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("escalations"))
    args = parser.parse_args(argv)

    with connect() as conn:
        person = _find_person(conn, args.person)
        if not person:
            log.error("no person matching %r", args.person)
            return 1
        data = _gather(conn, person, include_private=False)

        log.info("convening the room for %s", person["full_name"])
        # `mode` is what lets the ledger answer "are the spikes escalations, and
        # who asked for them". A deep room the review flagged is the system
        # spending Ryan's money; the same room because he typed the command is a
        # choice he made. Averaging them hides which one is growing.
        with attribute("room", mode=args.mode, person_id=str(person["id"]),
                       label=person["full_name"]):
            decision, transcript, spend = run_room(person, data, model=args.model)

        # If the room decided not to act, do not act. This was a real bug on the
        # first run: the Judge returned "do nothing this week", channel `none`,
        # and the pipeline cheerfully drafted an email anyway. That is exactly
        # the deciding-versus-writing collapse this whole module exists to fix,
        # reintroduced one layer down. A room whose "no" is ignored is theatre.
        body = None
        if args.draft and declined(decision) and not args.force:
            log.warning(
                "the room decided NOT to act (channel=%s, timing=%s). "
                "No draft written. Use --force to override.",
                decision.get("channel"), decision.get("timing"),
            )
        elif args.draft:
            body = sweep(draft_from(person, data, decision, model=args.model))
            evidence = json.dumps(data["signals"], default=str) + "".join(
                (t["body"] or "") for t in data["touches"]
            )
            first = (person["full_name"] or "").split()[0] or None
            for f in check(body, evidence_text=evidence, recipient_first_name=first):
                (log.error if f.blocking else log.info)("[%s] %s", f.rule, f.detail)

        _record(conn, person, data, decision, model=args.model or get_config().llm_model)

    args.out.mkdir(parents=True, exist_ok=True)
    slug = person["full_name"].lower().replace(" ", "-")
    (args.out / f"{slug}-room.md").write_text(transcript, encoding="utf-8")
    if body:
        (args.out / f"{slug}-room-draft.txt").write_text(body, encoding="utf-8")

    print()
    print(f"  MOVE        {decision.get('recommendation')}")
    print(f"  channel     {decision.get('channel')}  ·  timing {decision.get('timing')}"
          f"  ·  confidence {decision.get('confidence')}")
    print(f"  objection   {decision.get('strongest_objection')}")
    if decision.get("converged_early"):
        print(f"  TENTH MAN   {decision.get('tenth_man')}")
    for c in decision.get("unsupported_claims") or []:
        print(f"  UNSUPPORTED {c}")
    print()
    print(f"  cost        {spend['calls']} calls · {spend['input']:,} in / "
          f"{spend['output']:,} out ({spend['reasoning']:,} reasoning) · "
          f"~${spend['usd']:.4f}")
    print(f"  transcript  {args.out / (slug + '-room.md')}")
    return 0


def _record(conn: psycopg.Connection, person, data, decision, *, model) -> None:
    q = data.get("queue")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.decision_case
              (workspace_id, person_id, queue_item_id, situation, options,
               chosen, rationale, verdict, reason_code)
            values (%s, %s, %s, %s, %s, %s, %s, 'accepted', 'room_recommended')
            """,
            (
                get_config().workspace_id, person["id"],
                q["id"] if q and "id" in q else None,
                json.dumps({"queued": bool(q), "play": q["play"] if q else None}),
                Json({**decision, "panel_model": model, "mode": "api_room"}),
                decision.get("recommendation"),
                decision.get("approach"),
            ),
        )
    conn.commit()


if __name__ == "__main__":
    sys.exit(main())
