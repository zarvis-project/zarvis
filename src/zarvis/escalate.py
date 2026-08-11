"""Escalation packet: hand one hard email to a bigger brain, by hand.

    PYTHONPATH=src python -m zarvis.escalate export --person okafor
    PYTHONPATH=src python -m zarvis.escalate import --person okafor --file reply.md

WHY THIS IS NOT JUST "SEND MORE CONTEXT"
-----------------------------------------
Routine compose deliberately sends a thin, bounded evidence bundle. Measured on
the current queue, the situation is 7-18% of the prompt and the voice rules are
the rest. That looks stingy until you notice what the boundary buys:

  * The assertion linter can only separate cited from invented if it knows
    exactly what the model saw.
  * A model given everything reaches for specifics to prove it read them. That
    is what produced "I genuinely think it'll click for what you're driving at
    VensureHR" -- true, retrieved, and still a random detail bolted on.

So the answer to a nuanced case is not a permanently fatter prompt for
everyone. It is more JUDGMENT on the few that need it, with a human deciding
which those are. Ninety percent of days are the same three plays and want the
cheap deterministic path. The tenth needs someone to actually think, and that
someone is Ryan plus whichever model he trusts most that week.

Cost is not the driver. Three heavy escalations a day runs $0.34-$3.44 a month
depending on how xAI's rate units resolve. The driver is that a bounded prompt
produces better routine email, and an unbounded one produces better hard email,
and no single setting is right for both.

PRIVACY
-------
Private signals are EXCLUDED by default. The standing rule is that they inform
ranking and never reach a drafting model, and pasting this packet into a web
chat is reaching a drafting model. `--include-private` overrides it, loudly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

import psycopg

from .config import get_config
from .db import connect
from .lint import blocking, check, sweep

log = logging.getLogger("zarvis.escalate")

DEFAULT_DIR = pathlib.Path("escalations")


# ---------------------------------------------------------------------------
# Gather
# ---------------------------------------------------------------------------


def _find_person(conn: psycopg.Connection, needle: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.*, o.name as org_name, o.domain as org_domain, o.notes as org_notes
            from zarvis.person p
            left join zarvis.org o on o.id = p.org_id
            where p.workspace_id = %s and p.full_name ilike %s
            order by p.impact desc nulls last
            limit 1
            """,
            (get_config().workspace_id, f"%{needle}%"),
        )
        return cur.fetchone()


def _gather(conn: psycopg.Connection, person: dict, *, include_private: bool) -> dict:
    """Everything Zarvis knows. The opposite of the compose bundle, on purpose."""
    pid = person["id"]
    ws = get_config().workspace_id
    data: dict = {}

    with conn.cursor() as cur:
        # Every signal, including expired ones. Routine compose sees only live
        # signals; history is exactly what makes a hard case legible.
        cur.execute(
            """
            select kind, source, value, body, authored_by, is_private,
                   observed_at, expires_at
            from zarvis.signal
            where workspace_id = %s and person_id = %s
            order by observed_at desc
            """,
            (ws, pid),
        )
        signals = cur.fetchall()
        data["private_withheld"] = sum(1 for s in signals if s["is_private"])
        data["signals"] = [
            s for s in signals if include_private or not s["is_private"]
        ]

        cur.execute(
            """
            select channel, direction, at, external_ref, subject, body, source_ref
            from zarvis.touch
            where workspace_id = %s and person_id = %s
            order by at desc
            limit 60
            """,
            (ws, pid),
        )
        data["touches"] = cur.fetchall()

        cur.execute(
            "select role, status, since, notes from zarvis.relationship "
            "where workspace_id = %s and person_id = %s",
            (ws, pid),
        )
        data["roles"] = cur.fetchall()

        cur.execute(
            """
            select m.scope, mgr.full_name as manager, mgd.full_name as managed
            from zarvis.manages m
            left join zarvis.person mgr on mgr.id = m.manager_id
            left join zarvis.person mgd on mgd.id = m.managed_id
            where m.workspace_id = %s and (m.manager_id = %s or m.managed_id = %s)
            """,
            (ws, pid, pid),
        )
        data["management"] = cur.fetchall()

        # What Ryan did with previous drafts. This is the case log, and it is
        # the single most useful thing to show a model being asked to do better.
        cur.execute(
            """
            select d.created_at, d.model_id, d.status, d.verdict, d.skip_reason,
                   d.proposed_body, d.final_body, pl.name as play
            from zarvis.draft d
            left join zarvis.queue_item q on q.id = d.queue_item_id
            left join zarvis.play pl on pl.id = q.play_id
            where d.workspace_id = %s and d.person_id = %s
            order by d.created_at desc
            limit 10
            """,
            (ws, pid),
        )
        data["past_drafts"] = cur.fetchall()

        cur.execute(
            """
            select q.rank, q.tier, q.rps, q.urgency, q.deadline_pressure,
                   q.decay_pressure, q.score_breakdown, q.status,
                   pl.name as play, pl.key as play_key
            from zarvis.queue_item q
            join zarvis.play pl on pl.id = q.play_id
            where q.workspace_id = %s and q.person_id = %s and q.status = 'open'
            """,
            (ws, pid),
        )
        data["queue"] = cur.fetchone()

    return data


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _fmt(value) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()[:19]
    return str(value)


def _render(person: dict, data: dict, prompts: str) -> str:
    out: list[str] = []
    a = out.append

    a(f"# Draft an email to {person['full_name']}")
    a("")
    a("You are writing AS Ryan Miller, founder of Zenith Project. Everything below")
    a("is what Zarvis knows about this relationship. Unlike the routine daily")
    a("drafts, this one was escalated BY HAND because the situation is not")
    a("standard, so use judgment rather than a template.")
    a("")
    a("Return the email as plain text: subject line, blank line, then the body.")
    a("")
    a("---")
    a("")
    a("## Ryan's voice and copy rules")
    a("")
    a(prompts)
    a("")
    a("---")
    a("")
    a("## Hard constraints")
    a("")
    a("1. Never state a fact that is not somewhere in this document. If you want a")
    a("   specific and it is not here, write around it.")
    a("2. Signals marked `authored_by: operator` are RYAN'S OWN WORDS. Never")
    a("   reflect them back as though this person said or did them.")
    a("3. No em dashes. Commas and periods.")
    a("4. Do not promise how long anything takes. Ask for their time instead.")
    a("5. Close with a signoff, `Best,` or `Cheers,` then `Ryan`, no trailing period.")
    a("6. Do not name a detail just to prove you read it. A specific earns its place")
    a("   only if it changes what Ryan is asking for.")
    a("")
    a("---")
    a("")

    a("## Who they are")
    a("")
    a(f"- **Name:** {person['full_name']}")
    for label, key in (
        ("Title", "title"), ("Company", "org_name"), ("Email", "primary_email"),
        ("Timezone", "timezone"), ("Preferred channel", "preferred_channel"),
    ):
        if person.get(key):
            a(f"- **{label}:** {person[key]}")
    a(f"- **Impact (1-10):** {person.get('impact')}")
    if person.get("style_notes"):
        a(f"- **How Ryan writes to them:** {person['style_notes']}")
    if person.get("org_notes"):
        a(f"- **About the company:** {person['org_notes']}")
    if data["roles"]:
        roles = ", ".join(
            f"{r['role']}" + (f" ({r['status']})" if r["status"] else "")
            for r in data["roles"]
        )
        a(f"- **Relationship:** {roles}")
    for m in data["management"]:
        a(f"- **Managed:** {m['manager']} manages {m['managed']} ({m['scope']})")
    a("")

    q = data.get("queue")
    if not q:
        # Manual escalation must not require the queue to have surfaced them.
        # Dana Whitfield is the case that proved it: no play in the library
        # triggers on a marketing partnership, so the routine path had nothing
        # to say about the most active relationship in the book.
        a("## Zarvis did NOT surface this person")
        a("")
        a("No play in the current library matches their situation, so the daily")
        a("loop would not have drafted anything. This was escalated by hand.")
        a("Treat the absence as information: the situation does not fit the")
        a("standard patterns, which is usually why it is worth thinking about.")
        a("")
    if q:
        a("## Why Zarvis surfaced them today")
        a("")
        a(f"- **Play chosen:** {q['play']} (`{q['play_key']}`)")
        a(f"- **Rank {q['rank']}** in the {q['tier']} tier, score {q['rps']}")
        a(f"- Urgency {q['urgency']} = max(deadline {q['deadline_pressure']}, "
          f"decay {q['decay_pressure']})")
        a("")
        a("The play is a starting point, not an instruction. If the history below")
        a("suggests a better move, take it and say so.")
        a("")

    # The correspondence itself, when hydration has fetched it. This is the
    # difference between a room deliberating over a relationship and one
    # deliberating over a table of dates.
    hydrated = [t for t in data["touches"] if t.get("body")]
    if hydrated:
        a("## The correspondence")
        a("")
        a(f"{len(hydrated)} messages, oldest first. This is what was actually said.")
        a("")
        for m in sorted(hydrated, key=lambda x: x["at"]):
            if m["channel"] == "meeting":
                who = "CALL"
            else:
                who = "Ryan" if m["direction"] == "outbound" else person["full_name"]
            a(f"### {_fmt(m['at'])[:10]}  ·  {who}  ·  {m['subject'] or '(no subject)'}")
            a("")
            a("```")
            # Quoted reply chains repeat the whole thread on every message and
            # add nothing but tokens.
            body = m["body"]
            for cut in ("\nOn ", "\n> ", "\n-----Original", "\nFrom: "):
                if cut in body:
                    body = body.split(cut)[0]
            a(body.strip()[:3000])
            a("```")
            a("")

    a("## Contact history")
    a("")
    if data["touches"]:
        inbound = sum(1 for t in data["touches"] if t["direction"] == "inbound")
        outbound = len(data["touches"]) - inbound
        a(f"{len(data['touches'])} recorded touches ({outbound} out, {inbound} in). "
          f"Most recent first.")
        a("")
        a("| When | Direction | Channel |")
        a("|---|---|---|")
        for t in data["touches"][:25]:
            a(f"| {_fmt(t['at'])[:10]} | {t['direction']} | {t['channel']} |")
    else:
        a("No recorded contact. Either genuinely never contacted, or it happened")
        a("on a channel Zarvis cannot see (LinkedIn, WhatsApp, phone).")
    a("")

    a("## Everything known, newest first")
    a("")
    if data["private_withheld"]:
        a(f"> {data['private_withheld']} private signal(s) withheld. They inform")
        a("> ranking and are deliberately not shown to a drafting model.")
        a("")
    for s in data["signals"]:
        live = "live" if (not s["expires_at"] or s["expires_at"] > dt.datetime.now(dt.UTC)) else "expired"
        author = s["authored_by"] or "unknown"
        a(f"### {s['kind']}  ·  {_fmt(s['observed_at'])[:10]}  ·  {live}  ·  "
          f"written by {author}")
        a("")
        if s["value"]:
            a("```json")
            a(json.dumps(s["value"], indent=2, default=str))
            a("```")
        if s["body"]:
            a("")
            a("```")
            a(s["body"][:6000])
            a("```")
        a("")

    if data["past_drafts"]:
        a("## What Ryan did with previous drafts")
        a("")
        a("This is the most useful signal in the document. If he edited or rejected")
        a("something, do not repeat it.")
        a("")
        for d in data["past_drafts"]:
            a(f"### {_fmt(d['created_at'])[:10]} · {d['play'] or '?'} · "
              f"{d['model_id']} · status {d['status']} · verdict {d['verdict'] or 'none yet'}")
            if d["skip_reason"]:
                a(f"*Blocked:* {d['skip_reason']}")
            a("")
            a("```")
            a((d["proposed_body"] or "")[:2000])
            a("```")
            if d["final_body"] and d["final_body"] != d["proposed_body"]:
                a("")
                a("**What Ryan actually sent:**")
                a("")
                a("```")
                a(d["final_body"][:2000])
                a("```")
            a("")

    a("---")
    a("")
    a("Write the email.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _prompts() -> str:
    base = pathlib.Path(__file__).resolve().parents[2] / "prompts"
    return (
        (base / "voice.md").read_text(encoding="utf-8")
        + "\n\n"
        + (base / "humanize.md").read_text(encoding="utf-8")
    )


def do_export(args) -> int:
    with connect() as conn:
        person = _find_person(conn, args.person)
        if not person:
            log.error("no person matching %r", args.person)
            return 1
        data = _gather(conn, person, include_private=args.include_private)

    if args.include_private and data["private_withheld"]:
        log.warning(
            "INCLUDING %d private signal(s). These were recorded as not for a "
            "drafting model. Read the packet before pasting it anywhere.",
            data["private_withheld"],
        )

    text = _render(person, data, _prompts())
    out_dir = args.out or DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = person["full_name"].lower().replace(" ", "-")
    path = out_dir / f"{slug}.md"
    path.write_text(text, encoding="utf-8")

    log.info(
        "wrote %s  (%d chars, ~%d tokens, %d signals, %d touches, %d past drafts)",
        path, len(text), len(text) // 4, len(data["signals"]),
        len(data["touches"]), len(data["past_drafts"]),
    )
    log.info("paste it into whichever chat you trust, then: "
             "python -m zarvis.escalate import --person %r --file <reply>", args.person)
    return 0


def do_import(args) -> int:
    raw = args.file.read_text(encoding="utf-8").strip()

    # Subject on line one, blank line, body. Tolerates a "Subject:" prefix and
    # a model that wrapped the whole thing in a code fence.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    parts = raw.split("\n", 1)
    subject = parts[0].removeprefix("Subject:").strip()
    body = sweep(parts[1].strip() if len(parts) > 1 else "")

    if not body:
        log.error("no body found. Expected: subject, blank line, then the email.")
        return 1

    with connect() as conn:
        person = _find_person(conn, args.person)
        if not person:
            log.error("no person matching %r", args.person)
            return 1
        data = _gather(conn, person, include_private=False)

        # Lint it exactly like a generated draft. A human relaying a model's
        # output is not a reason to skip the check, it is a reason to keep it:
        # the packet is bigger, so there is more to accidentally assert.
        evidence_text = json.dumps(data["signals"], default=str)
        first = (person["full_name"] or "").split()[0] or None
        findings = check(body, evidence_text=evidence_text, recipient_first_name=first)
        for f in findings:
            (log.error if f.blocking else log.info)("[%s] %s", f.rule, f.detail)
        hard = blocking(findings)

        # A hand-escalated draft may have no queue item, because the queue may
        # never have surfaced this person. That is a fact about the draft, not
        # an error: see migration 20260809000007.
        q = data.get("queue")

        with conn.cursor() as cur:
            cur.execute(
                """
                insert into zarvis.draft
                  (workspace_id, queue_item_id, person_id, idempotency_key, channel,
                   subject, proposed_body, model_id, prompt_version, status, skip_reason)
                values (%s, (select id from zarvis.queue_item
                             where workspace_id = %s and person_id = %s
                               and status = 'open' limit 1),
                        %s, %s, 'email', %s, %s, %s, 'escalation-1', %s, %s)
                on conflict (workspace_id, idempotency_key) do nothing
                """,
                (
                    get_config().workspace_id,
                    get_config().workspace_id, person["id"],
                    person["id"],
                    f"escalation:{person['id']}:{args.file.stem}",
                    subject, body,
                    # Tagged so the case log never confuses a hand-escalated draft
                    # with one the daily loop produced. They are different
                    # processes and should not be averaged together.
                    f"web:{args.model}",
                    "skipped" if hard else "pending",
                    "; ".join(f"{f.rule}: {f.detail}" for f in hard) or None,
                ),
            )
        conn.commit()

    if hard:
        log.error("saved as SKIPPED: %d blocking finding(s) above", len(hard))
        return 1
    log.info("saved as pending draft for %s", person["full_name"])
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="write a packet to paste into a web chat")
    e.add_argument("--person", required=True, help="substring of their name")
    e.add_argument("--out", type=pathlib.Path)
    e.add_argument(
        "--include-private", action="store_true",
        help="include private signals. They were recorded as not for a model.",
    )
    e.set_defaults(fn=do_export)

    i = sub.add_parser("import", help="save a reply back as a draft")
    i.add_argument("--person", required=True)
    i.add_argument("--file", type=pathlib.Path, required=True)
    i.add_argument("--model", default="unknown", help="which chat wrote it, for the case log")
    i.set_defaults(fn=do_import)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
