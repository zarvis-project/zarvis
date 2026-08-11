# Contributing

Zarvis is built in public. The interesting parts are the bugs, and they are
written up in the code rather than tidied away.

## Before anything else: it does not send

`tests/test_no_send.py` asserts that no module calls Gmail's send endpoints. It
is worth reading the docstring, because the reasoning corrects a mistake:

> The original plan claimed this was structural, enforced by holding
> `gmail.readonly` instead of a write scope. That was wrong. `gmail.compose`
> grants `messages.send` as well as draft creation, and so does `gmail.modify`.
> **There is no Gmail scope that permits drafting but forbids sending.**

So the guarantee lives in that test. If you genuinely need sending, delete the
test in the same commit, deliberately and visibly, rather than weakening it.

## What good looks like here

**Comments explain why, not what.** The codebase is unusually heavily
commented, and specifically about decisions that look wrong until you know what
happened. If you fix a bug, write down what the bug taught, not what the code
now does.

**Every new module takes `--dry-run`, and it must write nothing.** More than one
bug here was caught only because a dry run printed a name that had no business
being in the output.

**Ledgers stay append-only.** `signal` and `touch` are INSERT and SELECT for the
agent role. If you find yourself wanting UPDATE, you probably want to append a
superseding row instead. The evidence a decision was based on has to survive the
decision.

**The drafting model sees the evidence bundle and nothing else.** Do not widen
that input for convenience. In particular, signals carry `authored_by`: content
written by the operator must never be reflected back to the recipient as insight
about them.

## Adding a source

A source writes rows into `zarvis.signal` and `zarvis.touch`. That is the entire
contract. Everything downstream reads those two tables, so a new source cannot
break ranking, drafting or the review.

Look at `src/zarvis/fireflies.py` as the reference. Roughly:

1. Fetch incrementally. Store a cursor in `zarvis.cursor`. Do not refetch
   history every night.
2. Set `direction` honestly. A meeting is `mutual`. Recording one as `outbound`
   makes it read as another unanswered message from the operator in exactly the
   two places that count them.
3. Use `external_ref` and the conflict target so a re-run updates rather than
   duplicates.
4. Store what a decision needs, not everything available. Fireflies summaries
   are stored; transcripts are not, because tens of thousands of tokens of
   filler in every review's context is real money for no better decision.

## Setting up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest tests/ -q
```

The tests are pure functions and run in under a second with no database and no
network. Keep them that way. A test that needs credentials is a test nobody
runs.

## Pull requests

- One idea per PR.
- Say what you observed, not just what you changed. "This drafted twice for the
  same person when X" is worth more than "fix duplicate drafts".
- If it changes behaviour that costs money, say what it costs.
- No em dashes in copy the system generates. This is enforced in `lint.py` and
  it is not a style preference, it is the most reliable machine-writing tell
  there is.

## Reporting a security issue

Do not open a public issue. The system holds mailbox access and a CRM. Email the
maintainer and give it a few days before disclosing.
