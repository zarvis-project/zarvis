"""Slack: Ryan's control surface. Poll, parse, act, confirm.

    PYTHONPATH=src python -m zarvis.slack --once      # one poll, for testing
    PYTHONPATH=src python -m zarvis.slack --loop      # every 10s, the daemon

WHY POLLING AND NOT SOCKET MODE
--------------------------------
Ryan's requirement, once stated plainly, is "not the next 8am" rather than
"instant". Five minutes is fine, so this runs as a scheduled task every five
minutes and there is no daemon at all.

That is worth more than the latency it gives up. Socket Mode needs a websocket
dependency, a reconnect path that survives every laptop sleep, and it has a
failure mode where the socket looks alive and is not. A persistent poll loop is
milder but still a process that has to be running. A scheduled task is neither:
it inherits the sleep behaviour, the retry semantics and the operational model
already proven by the daily job.

`--loop` remains for when a faster feel is wanted, and Socket Mode would slot in
here without changing anything else.

NATURAL LANGUAGE, WITH A RECEIPT
---------------------------------
Ryan types what he means and a model works out the intent. That buys ergonomics
and risks something worse than a rejected command: silently filing a note
against the wrong person. A real book has repeated first names in it, and two
people who share one are usually the two most confusable records in the whole
database. A wrong fact in the evidence bundle is worse than no fact, because the
drafting model will use it with confidence.

So every write replies with exactly what was recorded and against whom, and
ambiguity is a question rather than a guess.

UNDO IS A RETRACTION, NOT A DELETE
-----------------------------------
`signal` is append-only for the agent. UPDATE and DELETE were revoked so a bug
cannot rewrite the evidence the case log is built on, and an undo button is not
a good enough reason to hand that back. `undo` therefore appends a retraction
that supersedes the note; both remain visible, and the retraction is explicit
enough that a reader cannot mistake which is current.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg
from psycopg.types.json import Json

from .config import get_config
from .db import connect
from .llm import LLMError, complete

log = logging.getLogger("zarvis.slack")

API = "https://slack.com/api/"
CURSOR_SOURCE = "slack"
POLL_SECONDS = 10  # only used by --loop; the scheduled task runs every 5 minutes


# ---------------------------------------------------------------------------
# Slack transport
# ---------------------------------------------------------------------------


def _token() -> str:
    token = os.environ.get("ZARVIS_SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("ZARVIS_SLACK_BOT_TOKEN is not set")
    return token


def _call(method: str, payload: dict | None = None, *, post: bool = False) -> dict:
    url = API + method
    data = None
    headers = {"Authorization": f"Bearer {_token()}"}
    if post:
        data = json.dumps(payload or {}).encode()
        headers["content-type"] = "application/json; charset=utf-8"
    elif payload:
        url += "?" + urllib.parse.urlencode(payload)

    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if post else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    if not body.get("ok"):
        raise RuntimeError(f"slack {method}: {body.get('error')}")
    return body


def say(channel: str, text: str) -> None:
    _call("chat.postMessage", {"channel": channel, "text": text}, post=True)


def _dm_channels(conn: psycopg.Connection) -> list[str]:
    """The DM channels to read, cached in `zarvis.cursor`.

    `conversations.list` sits in Slack's most restricted rate tier and returns
    something that effectively never changes: the id of Ryan's DM with the bot.
    Calling it on every poll was fine at one request per five minutes and is
    wasteful at one per minute, for no benefit.

    Refreshed daily, or whenever the cache is empty, so a new DM still gets
    picked up without anyone thinking about it.
    """
    ws = get_config().workspace_id
    with conn.cursor() as cur:
        cur.execute(
            "select value, updated_at from zarvis.cursor "
            "where workspace_id=%s and source=%s and key='channels'",
            (ws, CURSOR_SOURCE),
        )
        row = cur.fetchone()

    fresh = False
    if row and row["value"]:
        import datetime as _dt

        age = _dt.datetime.now(_dt.UTC) - row["updated_at"]
        fresh = age < _dt.timedelta(days=1)
        if fresh:
            return [c for c in row["value"].split(",") if c]

    body = _call("conversations.list", {"types": "im", "limit": 100})
    channels = [
        c["id"] for c in body.get("channels", [])
        if not c.get("is_user_deleted")
        # Slackbot's DM appears here and no app can read it; it answers
        # `channel_not_found` forever. Dropping it at the source is tidier than
        # catching the error on every poll.
        and c.get("user") != "USLACKBOT"
    ]
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.cursor (workspace_id, source, key, value)
            values (%s, %s, 'channels', %s)
            on conflict (workspace_id, source, key)
            do update set value = excluded.value, updated_at = now()
            """,
            (ws, CURSOR_SOURCE, ",".join(channels)),
        )
    conn.commit()
    log.info("refreshed DM channel cache: %d channel(s)", len(channels))
    return channels


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def _last_ts(conn: psycopg.Connection, channel: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "select value from zarvis.cursor where workspace_id=%s and source=%s and key=%s",
            (get_config().workspace_id, CURSOR_SOURCE, channel),
        )
        row = cur.fetchone()
    return row["value"] if row else "0"


def _set_ts(conn: psycopg.Connection, channel: str, ts: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.cursor (workspace_id, source, key, value)
            values (%s, %s, %s, %s)
            on conflict (workspace_id, source, key)
            do update set value = excluded.value, updated_at = now()
            """,
            (get_config().workspace_id, CURSOR_SOURCE, channel, ts),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Understanding
# ---------------------------------------------------------------------------


PARSE_SYSTEM = """You route messages for Zarvis, Ryan Miller's sales assistant.

Ryan writes to you in plain language about people in his book. Work out what he
wants and who each part is about. Return JSON only:

{
  "items": [
    {
      "intent": "note | draft | room | status | ask | undo | idea | unclear",
      "person": "the name as Ryan wrote it, or null",
      "content": "the fact, instruction or direction, in Ryan's own words where possible"
    }
  ]
}

ONE ITEM PER PERSON. THIS IS THE MOST IMPORTANT RULE HERE.

A single message often carries facts about several people, because Ryan is
correcting a report that covered several people. Split it. Each item's `content`
must contain ONLY what is true of that item's person.

  "I sent the draft to marek. I didn't bin it. I also sent the draft to Sunniva"

  -> [{"intent": "note", "person": "marek",
       "content": "Ryan sent the draft. He did not bin it."},
      {"intent": "note", "person": "Sunniva",
       "content": "Ryan sent the draft."}]

Never put Sunniva's sentence in Marek's item. These become evidence bundles, and a
fact about the wrong person is worse than a missing one: the drafting model
cannot tell it is wrong and will write from it with confidence.

Most messages are one item. Do not invent a second person to fill the list.

INTENTS
  note    a fact about someone, for Zarvis to remember. The default when Ryan
          is telling you something rather than asking for something.
  draft   he wants an email written. "draft", "write", "send something to".
  room    he wants the situation deliberated. "what should I do about", "think
          about", "run the room on".
  status  he is asking about the QUEUE. "what's on the list", "who's on the board".
  ask     he is asking a question about a PERSON and their history. "what did I
          say my next steps were with X", "when did we last speak", "what did X
          say about pricing". `content` is the question itself, verbatim.
  idea    a PROJECT or product idea, not about a person. "we should build",
          "idea:", "add to the backlog", "it would be good if Zenith could".
          `person` is null for these.
  undo    he is retracting the last thing he told you.
  unclear you genuinely cannot tell. Use it rather than guessing.

RULES
- `person` is whatever name Ryan used. Do not correct spelling or expand it;
  matching happens elsewhere and needs his exact words.
- If a message contains a fact AND an instruction, prefer `draft` or `room` and
  put the fact in `content`, so nothing is lost.
- A person mentioned only as context for someone else is not their own item.
  "Ask JJ about the intro to Priya" is one item, about JJ.

There is no urgency field. `draft` and `room` both run the moment he asks, so
there is nothing for one to select between. It used to be here, was never read
by anything, and a field the model is asked to fill in that changes no behaviour
is a false promise sitting in the contract."""


def parse(text: str) -> list[dict]:
    """-> a list of {intent, person, content}, one entry per person.

    Always a list, even for the overwhelmingly common single-person message.

    The older contract returned a single object, so a message about two people
    had nowhere to put the second and the whole text was filed against whoever
    was named first. "I sent the draft to Marek. I also sent the draft to Sunniva"
    put a fact about Sunniva into Marek's evidence bundle, where the drafting model
    reads it as being about Marek and writes from it with confidence. That is the
    exact failure this module's docstring calls worse than a rejected command.

    The single-object shape is still accepted, because the model occasionally
    reverts to it and reshaping a stray response beats dropping Ryan's fact.
    """
    completion = complete(PARSE_SYSTEM, text, max_tokens=700)
    body = completion.text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(body)

    if isinstance(parsed, list):
        return parsed
    items = parsed.get("items")
    if isinstance(items, list) and items:
        return items
    return [parsed]


def resolve(conn: psycopg.Connection, name: str) -> list[dict]:
    """Candidate people for a name Ryan typed.

    Active people first and separately from the archive: he has 78 people he
    works with and 433 imported rolodex contacts, and a name that matches one of
    each almost always means the live one.
    """
    if not name:
        return []
    with conn.cursor() as cur:
        cur.execute(
            r"""
            select p.id, p.full_name, p.path_override, p.impact
            from zarvis.person p
            where p.workspace_id = %(ws)s
              -- Word boundary, not substring. A wildcard ILIKE on "ryan"
              -- matched "Elena Vargas" in the middle of his first name,
              -- which is noise in a list Ryan has to choose from.
              and p.full_name ~* ('\y' || %(name)s)
              -- Never offer Ryan himself. He is in his own book, seeded from
              -- the product like every other user, and he is never the person
              -- he means when messaging his own assistant.
              and not exists (
                select 1 from zarvis.person_identity i
                where i.person_id = p.id and i.kind = 'email'
                  and lower(i.value) = lower(%(me)s)
              )
            order by (p.path_override is distinct from 'archive') desc,
                     p.impact desc nulls last
            limit 6
            """,
            {"ws": get_config().workspace_id, "name": name,
             "me": get_config().google_impersonate},
        )
        rows = cur.fetchall()
    live = [r for r in rows if r["path_override"] != "archive"]
    return live or rows



# ---------------------------------------------------------------------------
# Pending question state
# ---------------------------------------------------------------------------
# Zarvis asks "which one?" and Ryan answers with a name. Without state that
# answer is parsed as a brand new message, the original fact is discarded, and
# nothing is written. That is exactly what happened on the first real test:
# "Ryan said his budget is frozen" produced a correct question, "Sam Okafor"
# produced silence, and the fact was lost.
#
# So the unanswered question is held, keyed by channel, and the next message is
# first offered to it as an answer.

# A day, not half an hour. The clock was never the real protection: the danger
# is a LATER message that happens to contain a candidate's name, and that is a
# risk at any interval. `_is_selection` handles it by shape instead, so the TTL
# only has to stop a genuinely ancient question from lingering, and Ryan can
# answer after a meeting without losing what he told Zarvis.
PENDING_TTL_HOURS = 24



def _is_selection(text: str, candidates: list[dict]) -> dict | None:
    """Is this message an ANSWER to "which one?", or a new message?

    Shape, not time. "Sam Okafor" is an answer. "Lee Rankin is churned, drop
    him" names a candidate and is plainly not one, and treating it as one would
    attach an old fact to the wrong person without either party noticing.

    So a selection has to be short and be essentially just the name.
    """
    cleaned = text.strip().strip(".!").lower()
    if len(cleaned.split()) > 4:
        return None
    for candidate in candidates:
        name = candidate["name"].lower()
        if cleaned == name:
            return candidate
        # "okafor" or "the first one's surname" style answers, but only when
        # the message is essentially nothing but that name.
        if cleaned in name and len(cleaned) >= 4 and len(cleaned) / len(name) > 0.4:
            return candidate
    return None


def _set_pending(conn: psycopg.Connection, channel: str, payload: dict | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.cursor (workspace_id, source, key, value)
            values (%s, %s, %s, %s)
            on conflict (workspace_id, source, key)
            do update set value = excluded.value, updated_at = now()
            """,
            (get_config().workspace_id, CURSOR_SOURCE, f"{channel}:pending",
             json.dumps(payload) if payload else ""),
        )
    conn.commit()


def _get_pending(conn: psycopg.Connection, channel: str) -> dict | None:
    import datetime as _dt

    with conn.cursor() as cur:
        cur.execute(
            "select value, updated_at from zarvis.cursor "
            "where workspace_id=%s and source=%s and key=%s",
            (get_config().workspace_id, CURSOR_SOURCE, f"{channel}:pending"),
        )
        row = cur.fetchone()
    if not row or not row["value"]:
        return None
    # A question answered an hour later is probably about something else.
    if _dt.datetime.now(_dt.UTC) - row["updated_at"] > _dt.timedelta(
        hours=PENDING_TTL_HOURS
    ):
        return None
    return json.loads(row["value"])


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _write_note(conn: psycopg.Connection, person_id: str, body: str, *, retract=False) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.signal
                (workspace_id, person_id, source, kind, observed_at, body,
                 authored_by)
            values (%s, %s, 'slack', 'note', now(), %s, 'operator')
            """,
            (get_config().workspace_id, person_id,
             ("RETRACTION, ignore the previous Slack note: " + body) if retract else body),
        )
    conn.commit()


def _set_direction(conn: psycopg.Connection, person_id: str, direction: str) -> bool:
    """Point compose at a specific approach. Returns False if not queued."""
    with conn.cursor() as cur:
        cur.execute(
            """
            update zarvis.queue_item
            set suggested_action = %s, updated_at = now()
            where workspace_id = %s and person_id = %s and status = 'open'
            """,
            (f"Ryan asked for this directly: {direction}",
             get_config().workspace_id, person_id),
        )
        changed = cur.rowcount
    conn.commit()
    return changed > 0


# ---------------------------------------------------------------------------
# Doing it now
# ---------------------------------------------------------------------------
#
# Everything below exists because "the draft goes to your inbox on the next run"
# is the wrong answer to "draft an email to Dana". The next run is tomorrow
# at 08:00. Ryan asked at 23:40 because he wanted it now, and a system that
# answers a direct request with a promise about tomorrow morning is one he
# stops asking.
#
# The daily run stays the default. This is the override, and it only fires on
# an explicit request naming a person, which is the one case where the cost is
# unambiguously authorised: he typed it.


def _ensure_queue_item(conn: psycopg.Connection, person_id: str) -> str | None:
    """The person's open queue item, creating one if they are not on the board.

    Most people Ryan names are not queued. The board is what SURFACED itself
    that morning; the rolodex is 470 people and the overwhelming majority of
    them are quiet, which is exactly why he has to ask by name.

    Returns the queue item id, or None if the `operator_request` play is
    missing, which means migration 20260810000017 has not been run.
    """
    ws = get_config().workspace_id
    with conn.cursor() as cur:
        cur.execute(
            """
            select id from zarvis.queue_item
            where workspace_id = %s and person_id = %s and status = 'open'
            order by rank nulls last limit 1
            """,
            (ws, person_id),
        )
        row = cur.fetchone()
        if row:
            return str(row["id"])

        cur.execute(
            "select id from zarvis.play where workspace_id is null "
            "and key = 'operator_request'"
        )
        play = cur.fetchone()
        if not play:
            return None

        # The evidence bundle is built the same way the nightly run builds it,
        # by the same function, so an on-demand draft sees exactly what a
        # scheduled one would. Reimplementing it here would drift, and the first
        # symptom would be a draft that quietly ignored a private-signal filter.
        cur.execute(
            """
            select id, person_id, kind, value, observed_at, is_private,
                   authored_by, body
            from zarvis.signal
            where workspace_id = %s and person_id = %s
              and (expires_at is null or expires_at > now())
            order by observed_at desc
            """,
            (ws, person_id),
        )
        signals = cur.fetchall()

    from .queue import _evidence

    bundle, digest = _evidence(signals)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.queue_item
              (workspace_id, person_id, play_id, tier, status, headline,
               channel_hint, evidence, evidence_hash)
            values (%s, %s, %s, 'priority', 'open', %s, 'email', %s, %s)
            returning id
            """,
            (ws, person_id, play["id"], "Ryan asked for this directly",
             Json(bundle), digest),
        )
        new_id = str(cur.fetchone()["id"])
    conn.commit()
    return new_id


def _draft_now(conn: psycopg.Connection, person: dict) -> str:
    """Compose and deliver for one person, right now."""
    from .compose import compose
    from .deliver import deliver

    counts = compose(conn, dry_run=False, person_id=person["id"])
    if not counts.get("drafted"):
        # Distinguish the three ways this ends with nothing in his inbox,
        # because they need three different responses from him and a single
        # "could not draft" would send him looking in the wrong place.
        if counts.get("no_address"):
            return (
                f"No usable email address on file for *{person['name']}*, so "
                f"there is nothing to draft to. Send me the address and I will "
                f"write it."
            )
        if counts.get("blocked"):
            return (
                f"Wrote it and the linter blocked it for *{person['name']}*. "
                f"That usually means it stated something the evidence does not "
                f"support. Ask me `why {person['name']}` for the detail."
            )
        if counts.get("skipped_unchanged"):
            return (
                f"There is already a current draft for *{person['name']}* in "
                f"your inbox saying the same thing, so I left it alone."
            )
        if counts.get("failed"):
            return (
                f"The model call failed for *{person['name']}*. It is worth "
                f"trying again in a minute; if it keeps failing the API key or "
                f"the provider is the problem, not the request."
            )
        return f"Nothing came back for *{person['name']}*: {counts}"

    # limit=5 rather than the CLI's 25: this is a targeted request, and a
    # sweep that also pushed four unrelated drafts into his inbox as a side
    # effect of asking about one person would be a surprise.
    delivered = deliver(conn, dry_run=False, limit=5)
    if delivered.get("created"):
        return f"Drafted and it is in your inbox now for *{person['name']}*."
    return (
        f"Drafted for *{person['name']}*, but delivery to Gmail did not land "
        f"({delivered}). It is saved and the next run will retry."
    )


def _room_now(conn: psycopg.Connection, person: dict, content: str) -> str:
    """Convene the full room for one person, right now.

    Attributed as `mode='requested'` so the cost ledger keeps Ryan's own
    escalations separate from the ones the board recommended. A room is the
    most expensive single thing Zarvis does and the question "is the spend the
    system's idea or mine" is unanswerable once those are averaged together.
    """
    from .costs import attribute
    from .escalate import _find_person, _gather
    from .room import _record, declined, run_room

    full = _find_person(conn, person["name"])
    if not full:
        return f"Could not load the full record for *{person['name']}*."

    data = _gather(conn, full, include_private=False)
    with attribute("room", mode="requested", person_id=str(full["id"]),
                   label=full["full_name"]):
        decision, transcript, spend = run_room(full, data, model=None)
    _record(conn, full, data, decision, model=get_config().llm_model)

    lines = [
        f"*Room on {full['full_name']}* — {decision.get('recommendation')}",
        f"_{decision.get('channel')} · {decision.get('timing')} · "
        f"confidence {decision.get('confidence')}_",
    ]
    if decision.get("strongest_objection"):
        lines.append(f"Strongest objection: _{decision['strongest_objection']}_")
    for claim in (decision.get("unsupported_claims") or [])[:2]:
        lines.append(f":warning: unsupported: _{claim}_")
    lines.append(f"_{spend['calls']} calls · ${spend['usd']:.3f}_")

    if declined(decision):
        lines.append(
            "It decided to wait, so I have not written anything. Say "
            f"`draft {full['full_name']} anyway` if you disagree, that is your "
            "call to make and it gets recorded as a veto."
        )
        return "\n".join(lines)

    # The room said act, so act. Making him ask twice for a decision he already
    # paid seven voices to reach is the deciding-versus-writing gap in reverse.
    _set_direction(conn, str(full["id"]), decision.get("approach")
                   or decision.get("recommendation") or content)
    lines.append("")
    lines.append(_draft_now(conn, person))
    return "\n".join(lines)




ANSWER_SYSTEM = """You answer Ryan's questions about one person, using only the
record below.

RULES
- Answer from the record and nothing else. If it does not say, say so plainly:
  "the record does not show that" is a good answer and an invented one is not.
- Cite dates. "On 24 April you agreed to..." beats "you agreed to...".
- Distinguish what RYAN said from what THEY said. Outbound email and his own
  notes are his words; inbound email and call summaries carry both, and getting
  this backwards is the most damaging mistake available here.
- Be brief. This is a Slack reply, not a report. Three or four lines usually.
- Where a meeting summary is the source, say so, because Ryan trusts those
  differently from his own written notes.
- If he asks WHY Zarvis decided something, answer from the "Decisions" section
  and quote its reasoning. Do not reconstruct a plausible rationale. If that
  section is absent, say no decision has been recorded rather than inferring
  what the board would probably have thought."""



def _reasoning(conn: psycopg.Connection, person_id: str) -> str:
    """What Zarvis decided about this person, and why it said so.

    `decision_case` holds the board's verdict, the approach it chose, the
    strongest surviving objection, what would change its mind, and any claim the
    Chair flagged as unsupported. None of that is in the evidence bundle, so
    without this `ask` could explain why someone surfaced but not why it told
    Ryan to leave them alone, which is the question it actually owes an answer
    to.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.created_at, c.chosen, c.rationale, c.verdict, c.reason_code,
                   c.options, c.outside_variables
            from zarvis.decision_case c
            where c.workspace_id = %s and c.person_id = %s
            order by c.created_at desc limit 6
            """,
            (get_config().workspace_id, person_id),
        )
        rows = cur.fetchall()
    if not rows:
        return ""

    out = ["", "## Decisions Zarvis has made about this person", ""]
    for r in rows:
        out.append(f"### {str(r['created_at'])[:16]} - {r['chosen'] or r['verdict']}")
        if r["rationale"]:
            out.append(f"Reason given: {r['rationale']}")
        opts = r["options"] or {}
        for key in ("approach", "channel", "timing", "reason",
                    "strongest_objection", "would_change_my_mind", "tenth_man"):
            if opts.get(key):
                out.append(f"- **{key.replace('_', ' ')}**: {opts[key]}")
        if opts.get("deep_review"):
            out.append("- flagged for a full deliberation room")
        for claim in (opts.get("unsupported_claims") or [])[:4]:
            out.append(f"- flagged as unsupported: {claim}")
        veto = (r["outside_variables"] or {}).get("veto_reason")
        if veto:
            out.append(f"- **Ryan overruled this**: {veto}")
        out.append("")
    return "\n".join(out)


def _answer(conn: psycopg.Connection, person_id: str, name: str, question: str) -> str:
    """Answer a question about one person from their record.

    Reuses the escalation packet's gather, so the answer sees exactly what a
    deliberation would: every signal, the correspondence, the meeting summaries,
    the contact history. Private signals are excluded, same as everywhere else.
    """
    from .escalate import _gather, _render

    with conn.cursor() as cur:
        cur.execute("select * from zarvis.person where id = %s", (person_id,))
        person = cur.fetchone()
    data = _gather(conn, person, include_private=False)

    record = _render(person, data, "")
    marker = "## Who they are"
    if marker in record:
        record = record[record.index(marker):]
    record = record.replace("\n---\n\nWrite the email.", "")
    # The evidence explains why someone SURFACED. It does not explain why the
    # board said wait, what the Skeptic argued, or which objection survived.
    record += _reasoning(conn, person_id)

    try:
        completion = complete(
            ANSWER_SYSTEM,
            f"# The record for {name}\n\n{record[:60000]}\n\n---\n\n"
            f"# Ryan asks\n\n{question}",
            max_tokens=700,
        )
    except LLMError as exc:
        return f"Could not answer that: {exc}"
    return completion.text.strip()


def _file_idea(content: str) -> str:
    """Put a project idea in the mailbox for exec to score and file.

    NOT written to `idea-backlog.json` directly, deliberately. The
    prioritisation app holds that file, `withStoreLock()` is in-process only,
    and `PUT /api/tasks` clobbers keys it does not recognise. A background agent
    writing it while the app is open could silently destroy work.

    The mailbox is the ecosystem's existing protocol for exactly this, and it
    keeps a human in the loop for scoring, which is the part that needs
    judgment.
    """
    import datetime as _dt
    import re as _re

    mailbox = pathlib.Path(__file__).resolve().parents[3] / "mailbox"
    mailbox.mkdir(exist_ok=True)
    slug = _re.sub(r"[^a-z0-9]+", "-", content.lower())[:60].strip("-") or "idea"
    path = mailbox / f"UNREAD - zarvis to exec - idea - {slug}.md"
    path.write_text(
        "# Idea from Slack\n\n"
        f"**From:** Ryan, via Slack · **Date:** {_dt.date.today()}\n"
        "**Action:** score with USI/UBI and add to `idea-backlog.json`\n\n"
        f"---\n\n{content}\n\n"
        "---\n\n"
        "Captured verbatim. Not scored, and not written to the backlog "
        "directly: the prioritisation app owns that file and a background "
        "write could clobber it.\n",
        encoding="utf-8",
    )
    return f"Filed as an idea for exec to score:\n> {content}\n_{path.name}_"


def handle(conn: psycopg.Connection, channel: str, text: str) -> str:
    # An unanswered question gets first refusal on this message.
    pending = _get_pending(conn, channel)
    dropped = None
    if pending:
        chosen = _is_selection(text, pending["candidates"])
        if chosen:
            _set_pending(conn, channel, None)
            return _act(conn, pending["intent"], chosen, pending["content"], channel)
        # Not an answer. Drop the question, but SAY SO. Silently discarding the
        # fact Ryan told Zarvis is the worst outcome available here: he would
        # believe it was recorded and it would simply be gone.
        _set_pending(conn, channel, None)
        dropped = pending["content"]

    try:
        items = parse(text)
    except (LLMError, json.JSONDecodeError) as exc:
        log.error("parse failed: %s", exc)
        return "I could not read that one. Try naming the person and what you want."

    replies = []
    for index, item in enumerate(items):
        # Only the first ambiguous item may occupy the pending slot; there is
        # one slot per channel and a second question would silently evict the
        # first, losing the fact attached to it.
        reply = _route(conn, channel, item, text,
                       may_ask=not any(r.startswith("Which one?") for r in replies))
        replies.append(reply)

    joined = "\n\n".join(replies)
    if dropped:
        joined = (
            "_Dropping my earlier question. That note is not recorded:_\n"
            f"> {dropped}\n\n{joined}"
        )
    return joined


def _route(conn: psycopg.Connection, channel: str, parsed: dict, text: str,
           *, may_ask: bool = True) -> str:
    """Handle ONE parsed item. Everything below is per-person."""
    dropped = None  # the drop notice is emitted once, by handle()

    intent = parsed.get("intent")
    name = parsed.get("person")
    content = (parsed.get("content") or text).strip()

    if intent == "status":
        with conn.cursor() as cur:
            cur.execute(
                """
                select p.full_name, q.tier, q.rank,
                       (q.suggested_action is not null) as acting,
                       -- Which of these the board thought were genuinely hard.
                       -- `deep_review` is the Chair saying seven voices on the
                       -- full record would earn their cost, and it belongs on
                       -- the board rather than buried in a transcript: it marks
                       -- where Ryan's own judgment is most likely needed.
                       exists (
                         select 1 from zarvis.decision_case dc
                         where dc.queue_item_id = q.id
                           and (dc.options->>'deep_review') = 'true'
                       ) as deep,
                       (select dc.options->>'reason'
                          from zarvis.decision_case dc
                         where dc.queue_item_id = q.id
                         order by dc.created_at desc limit 1) as why
                from zarvis.queue_item q join zarvis.person p on p.id = q.person_id
                where q.workspace_id = %s and q.status = 'open'
                  and q.tier in ('priority','standard')
                order by q.rank limit 12
                """,
                (get_config().workspace_id,),
            )
            rows = cur.fetchall()
        lines = []
        for r in rows:
            mark = "▸" if r["acting"] else "·"
            deep = "  :brain:" if r["deep"] else ""
            why = f"  _{r['why'][:60]}_" if r["why"] else ""
            lines.append(f"{mark} {r['rank']}. *{r['full_name']}*{deep}{why}")
        return (
            "*Current board*\n" + "\n".join(lines)
            + "\n\n▸ = act   · = wait   :brain: = flagged for a full room"
            + "\n_Ask me why about any of them._"
        )

    # The "dropped an unanswered question" notice belongs to the message, not
    # to one item in it, so handle() emits it once around the whole reply.

    if intent == "idea":
        return _file_idea(content)

    if intent == "ask" and name:
        people = resolve(conn, name)
        if len(people) == 1:
            return _answer(conn, str(people[0]["id"]), people[0]["full_name"], content)

    if intent == "unclear" or not name:
        return (
            "Not sure what to do with that. Name the person and tell me whether "
            "you want it remembered, drafted, or talked through."
        )

    people = resolve(conn, name)
    if not people:
        return (
            f"I have nobody matching *{name}*. Different spelling, or not in the "
            f"book yet?"
        )
    if len(people) > 1:
        if not may_ask:
            return (
                f"*{name}* matches more than one person and I already have a "
                f"question open from this message. Send that one on its own and "
                f"I will file it."
            )
        _set_pending(conn, channel, {
            "intent": intent, "content": content,
            "candidates": [
                {"id": str(x["id"]), "name": x["full_name"]} for x in people
            ],
        })
        listed = "\n".join(
            f"• {p['full_name']}"
            + (" _(archived)_" if p["path_override"] == "archive" else "")
            for p in people
        )
        # Ambiguity is a question, never a guess. Filing a fact against the
        # wrong person is worse than asking.
        return f"Which one?\n{listed}"

    return _act(conn, intent, {"id": str(people[0]["id"]),
                              "name": people[0]["full_name"]}, content, channel)


def _act(conn: psycopg.Connection, intent: str, person: dict, content: str,
         channel: str | None = None) -> str:
    """Do the thing, now that we know who it is about."""
    if intent == "undo":
        _write_note(conn, person["id"], content, retract=True)
        return (
            f"Retracted for *{person['name']}*. The original note stays in "
            f"the record with a retraction on top, because Zarvis cannot delete "
            f"evidence, only supersede it."
        )

    if intent == "note":
        _write_note(conn, person["id"], content)
        return (
            f"Noted for *{person['name']}*:\n> {content}\n"
            f"Wrong person? Reply `undo`."
        )

    if intent == "draft":
        if not _ensure_queue_item(conn, person["id"]):
            _write_note(conn, person["id"], f"Ryan wants an email: {content}")
            return (
                f"Saved as context for *{person['name']}*, but I could not put "
                f"them on the board: the `operator_request` play is missing. "
                f"Run migration `20260810000017_operator_request_play.sql`."
            )
        _set_direction(conn, person["id"], content)
        if channel:
            say(channel, f"Writing to *{person['name']}* now.")
        return _draft_now(conn, person)

    if intent == "room":
        _write_note(conn, person["id"], f"Ryan asked the room to consider: {content}")
        # Said before the work, not after. The room takes two to four minutes,
        # and silence for that long reads as a bot that died rather than one
        # that is thinking.
        if channel:
            say(channel,
                f"Convening the room on *{person['name']}*. Six voices and a "
                f"judge over the whole record, so give me a few minutes.")
        return _room_now(conn, person, content)

    return "I understood the person but not the ask."


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------


def poll_once(conn: psycopg.Connection) -> int:
    handled = 0
    for channel in _dm_channels(conn):
        since = _last_ts(conn, channel)
        try:
            body = _call("conversations.history",
                         {"channel": channel, "oldest": since, "limit": 20})
        except RuntimeError as exc:
            # `conversations.list` includes the Slackbot DM, which no app can
            # read. It answers `channel_not_found`, and letting that propagate
            # aborted the whole poll AFTER the real message had been handled,
            # so a successful run reported itself as a crash.
            log.debug("skipping %s: %s", channel, exc)
            continue
        messages = [
            m for m in reversed(body.get("messages", []))
            # Skip our own messages, or the bot answers itself forever.
            if m.get("type") == "message" and not m.get("bot_id")
            and m.get("ts", "0") > since
        ]
        for message in messages:
            text = (message.get("text") or "").strip()
            if not text:
                continue
            log.info("in: %s", text[:120])
            try:
                reply = handle(conn, channel, text)
            except Exception as exc:  # noqa: BLE001 - never die on one message
                log.exception("handling failed")
                reply = f"That broke: {exc}"
            say(channel, reply)
            _set_ts(conn, channel, message["ts"])
            handled += 1
    return handled


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS)
    args = parser.parse_args(argv)

    if not (args.once or args.loop):
        parser.error("pass --once or --loop")

    with connect() as conn:
        if args.once:
            log.info("handled %d message(s)", poll_once(conn))
            return 0
        log.info("polling every %ds. Ctrl-C to stop.", args.interval)
        while True:
            try:
                poll_once(conn)
            except Exception as exc:  # noqa: BLE001 - a poll failure is not fatal
                log.error("poll failed: %s", exc)
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
