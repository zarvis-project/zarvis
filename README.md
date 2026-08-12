# Zarvis

An open-source sales intelligence layer. It reads the tools you already use,
works out which relationships need attention today, and writes the emails into
your drafts folder for you to send.

**Zarvis never sends email.** It writes drafts. A human presses send. That
constraint is enforced by a test, not a promise, and the reasoning is in
[`tests/test_no_send.py`](tests/test_no_send.py).

Runs for about $0.35 to $0.50 a day.

---

## What it actually does

Every morning, in one scheduled run:

1. **Ingest**: pulls mail, calendar and meeting recordings since the last run.
2. **Rank**: scores every relationship, so the list is a standing agenda of
   live decisions rather than a fresh guess each day.
3. **Decide**: a review board of independent voices argues over the top of the
   board and decides who to contact and what to say. Some get a deeper room.
4. **Draft**: writes the emails, in your voice, from an evidence bundle it is
   not allowed to go outside of.
5. **Lint**: blocks anything that states a fact the evidence does not support.
6. **Deliver**: puts them in your Gmail drafts.
7. **Brief**: tells you what it did, what it decided against, and what it cost.

Then it watches what you do with the drafts. Sent as written, sent after edits,
or deleted. That verdict is the training signal, and it is the one piece of
data that cannot be reconstructed later.

## Why the ranking is not a CRM score

Zarvis ranks with a three-dimensional formula. Two of the dimensions are the
obvious ones. The third is the one that makes it usable:

```
Eff = max(1, Ease - Cost)
t   = (Eff - 1) / 9
PS  = 1.5·Eff + (2 - t)·Impact + (1 + t)·Urgency
```

**Ease dominates.** A quick win beats a slightly more valuable slog, because the
list you actually work through beats the list that is theoretically optimal and
sits untouched.

**Urgency matters more for easy things; impact matters more for hard things.**
That is the `t` term. A two-minute reply that is time-sensitive should jump the
queue. A hard, expensive move should be justified by what it is worth, not by
the fact that it is nagging you.

Urgency itself is `max(deadline_pressure, decay_pressure)`, where decay follows
a logistic curve rather than a linear one. Relationships do not cool at a
constant rate, they are fine and then suddenly they are not.

See [`src/zarvis/scoring.py`](src/zarvis/scoring.py). It is pure functions and
readable in one sitting.

## Sources

Anything with an API. The ones that ship:

| Source | What it contributes |
|---|---|
| Gmail | Correspondence, reply detection, the delivery target |
| Google Calendar | Meetings held and booked, identity discovery from invites |
| Fireflies | Meeting summaries. Not transcripts, deliberately |
| Notion | One-way CRM import: contacts, notes, page bodies |

A source's job is to write rows into `zarvis.signal` and `zarvis.touch`.
Everything downstream reads those two tables and nothing else, so adding a
source is a self-contained job that cannot break ranking.

**Meeting summaries matter more than they look.** The first board review here
declined to act on nine of fifteen people, and its stated reason for most of
them was "no correspondence on file". That was true of the email record and
wrong about the relationships, because the selling had happened on calls. A
system reasoning about relationships while blind to the channel where the
conversations happen will be confidently wrong, and no amount of prompt tuning
fixes it.

## Requirements

- Python 3.13+
- PostgreSQL 15+ (Supabase works; so does anything else)
- An LLM API key. Any OpenAI-compatible endpoint, or Anthropic. There is no
  vendor SDK in the tree. [`src/zarvis/llm.py`](src/zarvis/llm.py) is raw
  `urllib` against a documented seam.
- A Google service account with domain-wide delegation, for Gmail and Calendar.
- Optional: Fireflies, Slack, Notion.

## Setup

```bash
git clone https://github.com/zarvis-project/zarvis
cd zarvis
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

cp .env.example .env.local

# Run every migration, in filename order. The first one creates the schema and
# a workspace row; edit the name and owner_email in it first, they are yours.
for f in migrations/*.sql; do psql "$ZARVIS_DATABASE_URL" -f "$f"; done

# The id that goes in ZARVIS_WORKSPACE_ID:
psql "$ZARVIS_DATABASE_URL" -c 'select id, name from zarvis.workspace;'

PYTHONPATH=src python -m zarvis.ingest
PYTHONPATH=src python -m zarvis.queue --dry-run
```

Every module runs standalone and every one takes `--dry-run`. Use it. A dry run
prints exactly what would happen and writes nothing.

Then the whole thing:

```bash
PYTHONPATH=src python -m zarvis.daily
```

## Your voice

`prompts/voice.example.md` and `prompts/humanize.example.md` are templates.
Copy them to `voice.md` and `humanize.md` and rewrite them as yourself. The
examples are deliberately generic and the output will read that way until you
do.

The single highest-leverage edit is a list of things you never write. Banned
words, banned constructions, the punctuation you do not use. Models reach for
the same handful of tells, and naming them is what stops the drafts sounding
like a model.

## Cost

A normal morning is a handful of model calls, because the expensive steps are
skipped when nothing has changed. Every relationship carries an evidence hash;
if it has not moved, last run's conclusion is still true and gets reused.

Costs are recorded per call in `zarvis.llm_call`, attributed to what asked for
them, so a spike can be traced to the escalation that caused it rather than
averaged into a monthly number that tells you nothing.

```bash
PYTHONPATH=src python -m zarvis.costs --days 7
```

## Design notes worth reading before you extend it

- **Ledgers are append-only.** The agent role holds INSERT and SELECT on
  `signal` and `touch`, and no UPDATE or DELETE. A bug cannot rewrite the
  evidence its own decisions are built on. "Undo" appends a retraction.
- **The drafting model sees an evidence bundle and nothing else.** No private
  notes, no "here is everything we know". Signals carry `authored_by`, because
  quoting a user's own words back to them as insight about them is the fastest
  way to destroy trust in a tool like this.
- **Plays are triggers, not scripts.** A play notices that someone needs
  attention and says why. What to actually say is decided by the review, per
  person, against the record.
- **Deciding and writing are separate.** They were the same thing once, and the
  result was a pipeline that cheerfully drafted an email after the room had
  decided not to act. A "no" that downstream ignores is theatre.

## Status

Working, running daily, and young, built in public. The interesting parts are
the bugs, and they are written up in the commit history rather than tidied away.

Contributions welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Site

[zarvis.co](https://zarvis.co) is built from `docs/` in this repo.

## License

MIT. See [`LICENSE`](LICENSE).
