"""Every INSERT lists as many columns as it has placeholders.

WHY THIS EXISTS
---------------
On 2026-08-11 the morning run failed at the first step and kept failing, every
thirty minutes, for four hours. The cause was one missing word:

    insert into zarvis.decision_case
      (workspace_id, person_id, queue_item_id, draft_id, situation,
       chosen, verdict, ...)          -- `rationale` was never added here
    values (%s, %s, %s, %s, %s, %s, %s, ...)

...while thirteen parameters were passed. Postgres rejected it, `verdict` runs
first in the daily chain, and a failing first step means no ingest, no ranking,
no review, no drafts and no brief. One absent column name cost a whole day.

It survived review because it was tested in a DRY RUN, and `poll()` returns
before the INSERT when `dry_run` is set. The write path had never once executed.
That is the real lesson and it generalises: a dry run proves the read path and
says nothing about the write path.

WHY STATIC RATHER THAN A LIVE INSERT
------------------------------------
This parses source text. It needs no database, no credentials and no network, so
it runs in the same second as the rest of the suite and therefore actually runs.
A fixture-database test would be stronger and would be skipped on every machine
that has not set one up, which is where the bug was hiding in the first place.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# `insert into <table> (` — the VALUES list is then read by hand, because it
# contains nested parens (`now()`, `%s::uuid`) that a regex cannot balance.
INSERT_HEAD = re.compile(r"insert\s+into\s+[\w.]+\s*\(", re.IGNORECASE)


def _balanced(text: str, start: int) -> tuple[str, int]:
    """Read from an opening paren to its match, respecting quotes and nesting."""
    depth, i, quote = 0, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1: i], i + 1
        i += 1
    return "", len(text)


def _split_top(blob: str) -> list[str]:
    """Comma-split at paren depth zero, so `now()` counts as one value."""
    parts, depth, current, quote = [], 0, [], None
    for ch in blob:
        if quote:
            if ch == quote:
                quote = None
            current.append(ch)
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p for p in (x.strip() for x in parts) if p]


def _columns(blob: str) -> list[str]:
    out = []
    for part in blob.split(","):
        part = re.sub(r"--[^\n]*", "", part).strip()
        if part:
            out.append(part)
    return out


def test_insert_column_and_value_counts_match():
    """Counts VALUES entries, not `%s`.

    Half these statements mix placeholders with literals, as in
    `values (%s, %s, 'meeting', 'mutual', %s, ...)`. Counting only `%s` reports
    every one of those as broken, which is how a check earns its way into
    somebody's ignore list rather than catching the next real one.
    """
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for head in INSERT_HEAD.finditer(text):
            col_blob, after = _balanced(text, head.end() - 1)
            tail = text[after: after + 400]
            values_at = re.search(r"^\s*values\s*\(", tail, re.IGNORECASE)
            if not values_at:
                continue  # INSERT ... SELECT: no value list to compare against
            val_blob, _ = _balanced(tail, values_at.end() - 1)
            if "%(" in val_blob:
                continue  # dict params: position is irrelevant
            columns = _columns(col_blob)
            values = _split_top(val_blob)
            if columns and values and len(columns) != len(values):
                line = text[: head.start()].count("\n") + 1
                problems.append(
                    f"{path.relative_to(SRC)}:{line} "
                    f"{len(columns)} columns vs {len(values)} values\n"
                    f"        cols:   {', '.join(columns)}\n"
                    f"        values: {', '.join(values)}"
                )
    assert not problems, "INSERT arity mismatch:\n  " + "\n  ".join(problems)
