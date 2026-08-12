"""Draft generation. The step that turns a ranked list into queued email.

    PYTHONPATH=src python -m zarvis.compose --dry-run
    PYTHONPATH=src python -m zarvis.compose

**Zarvis does not send.** This module writes rows to `zarvis.draft`. A separate
writer process pushes approved rows into Gmail as drafts, and a human presses
send. Nothing here imports a mail-sending capability, and `tests/test_no_send.py`
asserts that stays true.

WHAT THE MODEL IS AND IS NOT ALLOWED TO KNOW
--------------------------------------------
The prompt receives the evidence bundle and nothing else about the person. No
free-text CRM notes, no private signals, no "here is everything we have". Three
reasons, in order of how badly each one bites:

  1. `is_private` signals inform ranking and must never reach the drafting
     model. Ryan knowing someone is going through a divorce is a legitimate
     reason to wait a week; it is not material for an email.
  2. Signals carry `authored_by`. A Calendly "anything you'd like to share?"
     field is written by whoever booked the meeting, which is frequently Ryan.
     Quoting his own words back to a prospect as insight about them is the
     single most credibility-destroying thing this system could do.
  3. A bounded input is a lintable one. `lint.check()` can only verify claims
     against the bundle if the bundle is genuinely all the model saw.

COST
----
Drafting is capped at the priority tier, and skipped entirely when the evidence
hash is unchanged since the last draft. On a normal morning that means a handful
of calls, not forty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import sys

import psycopg

from .config import get_config, synthetic_email_exclusion
from .db import connect
from .costs import attribute
from .lint import blocking, check, sweep
from .llm import Completion, LLMError, complete

log = logging.getLogger("zarvis.compose")

PROMPT_DIR = pathlib.Path(__file__).resolve().parents[2] / "prompts"
PROMPT_VERSION = "1"

MAX_BODY_TOKENS = 1200


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _load_prompts() -> str:
    voice = (PROMPT_DIR / "voice.md").read_text(encoding="utf-8")
    humanize = (PROMPT_DIR / "humanize.md").read_text(encoding="utf-8")
    return f"{voice}\n\n---\n\n{humanize}"


SYSTEM_TEMPLATE = """You draft emails as Ryan Miller, founder of Zenith Project.

Below are Ryan's voice and copy rules. Follow them exactly.

{prompts}

---

# Hard constraints

1. **Never state a fact that is not in the evidence bundle.** No invented
   numbers, dates, company details, or events. If you want to reference
   something specific and it is not in the evidence, write around it. A vague
   true sentence always beats a specific invented one.
2. **Evidence written by Ryan is not intelligence about them.** Each signal has
   an `authored_by` field. Where it is `operator`, those are Ryan's own words.
   Never reflect them back as though the recipient said or did them.
3. **No em dashes.** Commas and periods.
4. **Short.** Three to six sentences. This is a real email between people who
   know each other, not a marketing asset.
5. **End on an ask**, in Ryan's question-forward style, unless the play says
   otherwise.
6. Do not invent a meeting time, a call length, or how long anything takes.
   "A few minutes" is fine because it is vague and honest. "A 20-minute
   walkthrough" is a commitment Ryan never made. If proposing time, ASK for
   theirs rather than promising an amount of yours.
7. **Always close with a signoff**, on its own lines, exactly like this:

       Best,
       Ryan

   `Cheers,` and `Thanks,` are equally good, vary them. No period after the
   name. An email that ends on the question and stops reads as unfinished, and
   Ryan has to type this line himself before every send.

# Output

Return JSON only, no prose around it:

{{"subject": "...", "body": "..."}}

If replying inside an existing thread the subject is ignored, so keep it short.
Use "\\n\\n" between paragraphs in the body."""


USER_TEMPLATE = """# The move

**Play:** {play_name}
{play_instruction}

# Who

**Writing to:** {full_name}
**First name for the greeting:** {first_name}
{extra_person}
{routing}

# Evidence bundle

This is everything known. Do not go beyond it.

```json
{evidence}
```

{thread}Write the email."""


# PLAY_INSTRUCTIONS is gone.
#
# It mapped a play key to a canned angle: "they signed up and did not finish
# setup, offer to get them unstuck". That is a precedence integer pretending to
# know what to say, and it failed in both directions. It had nothing at all for
# Dana Whitfield, because no play in the library covers a marketing partnership.
# And on the first board review the room threw out `onboarding_stall_fresh` for
# half the queue after noticing the play says "fresh" while firing on people who
# signed up in March.
#
# Plays are triggers now. They notice that someone needs attention and say why.
# review.py decides what to do about it and writes the direction to
# `queue_item.suggested_action`, which is what the prompt below reads.
DEFAULT_INSTRUCTION = "Move this relationship forward by one concrete step."


def _build_prompt(item: dict) -> tuple[str, str, str]:
    """-> (system, user, evidence_text_for_linting)"""
    evidence = item["evidence"] or {}

    # Private content never enters the bundle in the first place (queue.py drops
    # it), but the count is kept so the model is not told a partial truth is the
    # whole truth.
    evidence_json = json.dumps(evidence, indent=2, default=str)

    thread = ""
    last_inbound = _last_inbound_body(evidence)
    if last_inbound:
        thread = (
            "# Their last message\n\nThis is what they actually wrote. Reply to it.\n\n"
            f"```\n{last_inbound[:4000]}\n```\n\n"
        )

    # The recipient is the agency when one manages this person. Ryan's rule:
    # the agency is the go-between and their client is not emailed directly.
    # The email is still ABOUT the client, which is what `routing` explains.
    subject_name = item["full_name"] or ""
    manager = item.get("route_full_name")
    full_name = manager or subject_name
    first_name = full_name.split()[0] if full_name else ""

    routing = ""
    if manager:
        routing = (
            f"**This is an agency account.** {subject_name} is a client of "
            f"{manager}, who resells Zenith to them.\n\n"
            f"You are writing to {manager}, ABOUT {subject_name}. Do not write "
            f"to {subject_name}, do not greet them, and do not write as though "
            f"they will read this. The agency owns the relationship with their "
            f"own client, and Ryan going around them would be a real breach "
            f"rather than a style problem.\n\n"
            f"Ask {manager} what they want to do, or tell them what you are "
            f"seeing. They decide whether their client hears about it."
        )

    extra = []
    if item.get("title"):
        extra.append(f"**Title:** {item['title']}")
    if item.get("style_notes"):
        extra.append(f"**How Ryan writes to them:** {item['style_notes']}")

    # The direction comes from the review, which read the correspondence and
    # decided. DEFAULT_INSTRUCTION is only a floor for compose run by hand
    # without a review having happened.
    if item.get("suggested_action"):
        instruction = (
            "A decision review read this relationship's full history and "
            "concluded:\n\n"
            f"{item['suggested_action']}\n\n"
            "Follow that direction. It was reached with more of the record "
            "than you can see here."
        )
    else:
        instruction = DEFAULT_INSTRUCTION

    system = SYSTEM_TEMPLATE.format(prompts=_load_prompts())
    user = USER_TEMPLATE.format(
        play_name=item["play_name"],
        play_instruction=instruction,
        full_name=full_name,
        first_name=first_name,
        routing=routing,
        extra_person="\n".join(extra),
        evidence=evidence_json,
        thread=thread,
    )
    return system, user, evidence_json + "\n" + (last_inbound or "")


def _last_inbound_body(evidence: dict) -> str | None:
    """The counterparty's own words, if any signal carries them.

    Filtered on authored_by so Ryan's own text can never be presented to the
    model as something the recipient said.
    """
    for signal in evidence.get("signals", []):
        if signal.get("kind") != "reply_received":
            continue
        if signal.get("authored_by") not in (None, "counterparty"):
            continue
        body = (signal.get("value") or {}).get("body") or signal.get("body")
        if body:
            return body
    return None


def _parse(text: str) -> tuple[str, str]:
    """Pull subject/body out of the model's reply, tolerating code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model did not return JSON: {text[:300]}") from exc
    return (data.get("subject") or "").strip(), (data.get("body") or "").strip()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _draftable(conn: psycopg.Connection, limit: int,
               person_id: str | None = None) -> list[dict]:
    """Open priority-tier items whose play produces an email.

    `drafts` and `channel_hint` are properties of the play, so a WhatsApp rescue
    or a phone call never reaches the email composer.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select
              q.id as queue_item_id, q.person_id, q.play_id, q.rank, q.tier,
              q.evidence, q.evidence_hash, q.suggested_action,
              pl.key as play_key, pl.name as play_name,
              p.full_name, p.title, p.style_notes,
              (select i.value from zarvis.person_identity i
                where i.person_id = p.id and i.kind = 'email'
                  and i.superseded_at is null
                  /*SYNTH:i.value*/
                order by i.created_at limit 1) as email,
              (select array_agg(d.idempotency_key) from zarvis.draft d
                where d.queue_item_id = q.id) as draft_keys,
              -- WHO THE EMAIL IS ACTUALLY ADDRESSED TO.
              --
              -- For a client sitting under an agency, that is the AGENCY, not
              -- the client. Ryan's rule: the agency is the go-between, and
              -- mailing their client directly goes around the person who owns
              -- the relationship. It is the reseller's account to manage.
              --
              -- Null when nobody manages them, and every consumer falls back to
              -- the person themselves. The join is deliberately not filtered on
              -- scope: any management relationship means somebody stands
              -- between Ryan and this person, which is the whole question.
              mgr.id as route_person_id,
              mgr.full_name as route_full_name,
              (select i.value from zarvis.person_identity i
                where i.person_id = mgr.id and i.kind = 'email'
                  and i.superseded_at is null
                  /*SYNTH:i.value*/
                order by i.created_at limit 1) as route_email
            from zarvis.queue_item q
            join zarvis.play pl on pl.id = q.play_id
            join zarvis.person p on p.id = q.person_id
            left join zarvis.manages m on m.managed_id = p.id
            left join zarvis.person mgr on mgr.id = m.manager_id
            where q.workspace_id = %s
              and q.status = 'open'
              -- When Ryan names someone in Slack, the play's own opinion about
              -- whether it drafts is irrelevant: he asked for an email. The
              -- filters below exist to stop the DAILY run drafting for a
              -- brief-only play, not to argue with a direct request.
              -- Every parameter here is cast explicitly. An uncast
              -- placeholder in a null test gives the planner nothing to infer
              -- a type from and Postgres refuses the whole statement.
              and (%s::uuid is not null or (pl.drafts is true
                                            and pl.channel_hint = 'email'))
              and (%s::uuid is null or q.person_id = %s::uuid)
              -- The review decides who gets drafted, not the tier. It writes
              -- `suggested_action` for the people it says to act on and leaves
              -- it null for the waits, so this clause IS the decision being
              -- honoured. Drafting the whole priority tier regardless would
              -- make the review advisory, and a review nobody has to obey is
              -- a log file.
              and q.suggested_action is not null
            order by q.rank
            limit %s
            """,
            (get_config().workspace_id, person_id, person_id, person_id, limit),
        )
        return cur.fetchall()


def _idempotency_key(item: dict) -> str:
    """Identity of a draft: this item, this evidence, this direction, this prompt.

    All four matter. Evidence alone is not enough because the review can change
    its mind about unchanged facts, and the direction is what the model is
    actually told to do.
    """
    direction = hashlib.sha256(
        (item.get("suggested_action") or "").encode()
    ).hexdigest()[:12]
    return f"{item['queue_item_id']}:{item['evidence_hash']}:{direction}:{PROMPT_VERSION}"


def _insert_draft(
    conn: psycopg.Connection,
    item: dict,
    *,
    subject: str,
    body: str,
    completion: Completion,
    status: str,
    skip_reason: str | None,
    run_id: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.draft
              (workspace_id, queue_item_id, person_id, run_id, idempotency_key,
               channel, evidence_hash, subject, proposed_body, model_id,
               prompt_version, status, skip_reason)
            values (%s, %s, %s, %s, %s, 'email', %s, %s, %s, %s, %s, %s, %s)
            on conflict (workspace_id, idempotency_key) do nothing
            """,
            (
                get_config().workspace_id,
                item["queue_item_id"],
                item["person_id"],
                run_id,
                _idempotency_key(item),
                item["evidence_hash"],
                subject,
                body,
                completion.model_id,
                PROMPT_VERSION,
                status,
                skip_reason,
            ),
        )


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def compose(conn: psycopg.Connection, *, dry_run: bool, run_id: str | None = None,
            person_id: str | None = None) -> dict:
    cfg = get_config()
    counts = {
        "considered": 0, "skipped_unchanged": 0, "drafted": 0, "blocked": 0,
        # `failed` is transient and retryable. `no_address` is permanent, and
        # keeping them apart matters: the daily runner retries on failure every
        # 30 minutes, and an unroutable person would otherwise keep the whole
        # morning in a retry loop that can never succeed.
        "failed": 0, "no_address": 0,
    }
    spend = 0.0

    items = _draftable(conn, cfg.tier_priority if not person_id else 5, person_id)
    log.info("compose: %d priority items with an email play", len(items))

    for item in items:
        counts["considered"] += 1
        who = item["full_name"]
        # The address follows the routing, not the subject. An agency's client
        # is written ABOUT, to their agency; mailing them directly goes around
        # the person who owns that relationship.
        item["recipient_name"] = item.get("route_full_name") or item["full_name"]
        item["recipient_email"] = item.get("route_email") or item["email"]
        if item.get("route_full_name"):
            who = f"{item['route_full_name']} (about {item['full_name']})"

        if not item["recipient_email"]:
            log.warning("compose: %s has no address, skipping", who)
            counts["no_address"] += 1
            continue

        # Skip only when NOTHING that shapes the draft has moved.
        #
        # This used to compare evidence hashes alone, which was wrong the moment
        # the review became the decision layer: the board can read the same
        # evidence and reach a different conclusion, and on the first run it did
        # exactly that for Sam Okafor. Comparing evidence only meant a fresh
        # decision silently produced no draft, which is the worst kind of bug
        # here because the log says "unchanged" and everything looks fine.
        #
        # The key now covers the direction too, so a changed decision is a
        # changed draft.
        key = _idempotency_key(item)
        if key in (item["draft_keys"] or []):
            log.info("compose: %s unchanged since last draft, skipping", who)
            counts["skipped_unchanged"] += 1
            continue

        if spend >= cfg.cost_ceiling_usd:
            log.warning(
                "compose: cost ceiling $%.2f reached, stopping with %d items unprocessed",
                cfg.cost_ceiling_usd,
                len(items) - counts["considered"] + 1,
            )
            break

        system, user, evidence_text = _build_prompt(item)

        if dry_run:
            log.info("DRY RUN: would draft %s (%s)", who, item["play_key"])
            continue

        try:
            with attribute("compose", mode="routine",
                           person_id=str(item["person_id"]), label=who):
                completion = complete(system, user, max_tokens=MAX_BODY_TOKENS)
            subject, body = _parse(completion.text)
        except LLMError as exc:
            log.error("compose: %s failed: %s", who, exc)
            counts["failed"] += 1
            continue

        spend += completion.cost_usd()
        body = sweep(body)

        # Lint the greeting against whoever actually receives it. Checking it
        # against the client is what blocked two correct agency drafts as
        # "wrong-name": the model greeted the agency, which was right.
        recipient = item["recipient_name"] or ""
        first_name = recipient.split()[0] if recipient else None
        findings = check(body, evidence_text=evidence_text, recipient_first_name=first_name)
        hard = blocking(findings)

        for finding in findings:
            level = log.error if finding.blocking else log.info
            level("compose: %s [%s] %s", who, finding.rule, finding.detail)

        if hard:
            # Held, not discarded. A blocked draft is the most informative
            # artifact this system produces — it is the model reaching for
            # something it was not given, and the pattern is worth reading.
            _insert_draft(
                conn, item, subject=subject, body=body, completion=completion,
                # The schema's vocabulary, not a new one: pending / approved /
                # skipped / expired. A linter-blocked draft is `skipped` with
                # the reason recorded, which is what skip_reason is for.
                status="skipped",
                skip_reason="; ".join(f"{f.rule}: {f.detail}" for f in hard),
                run_id=run_id,
            )
            counts["blocked"] += 1
            continue

        _insert_draft(
            conn, item, subject=subject, body=body, completion=completion,
            status="pending", skip_reason=None, run_id=run_id,
        )
        counts["drafted"] += 1
        log.info("compose: drafted for %s (%s)", who, item["play_key"])

    if dry_run:
        conn.rollback()
        log.info("DRY RUN, nothing written")
    else:
        conn.commit()

    counts["cost_usd"] = round(spend, 4)
    log.info("compose: %s", counts)
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Generate drafts for the priority tier")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--person-id", help="draft for one person, on request")
    args = parser.parse_args(argv)

    cfg = get_config()
    if cfg.kill_switch:
        log.warning("ZARVIS_KILL_SWITCH is set. Exiting.")
        return 0

    dry = args.dry_run or cfg.dry_run

    # Fail fast rather than discovering it once per item. A missing key is a
    # configuration error, not four separate drafting errors.
    if not dry and not cfg.llm_api_key:
        log.error("ZARVIS_LLM_API_KEY is not set. Compose cannot run without it.")
        return 1

    with connect() as conn:
        try:
            counts = compose(conn, dry_run=dry, person_id=args.person_id)
        except LLMError as exc:
            log.error("%s", exc)
            return 1

    # A run that drafted nothing because everything errored is NOT a success.
    # Returning 0 here would let daily.py record the day as done and go quiet
    # until tomorrow, which is the exact silent-skip this system exists to
    # prevent. Blocked drafts are a real outcome and do not count as failure.
    if counts["failed"]:
        log.error(
            "compose: %d item(s) failed. Reporting failure so the run is retried.",
            counts["failed"],
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
