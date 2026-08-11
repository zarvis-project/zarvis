"""What Zarvis is spending, per call, with attribution.

    PYTHONPATH=src python -m zarvis.costs                 # last 30 days
    PYTHONPATH=src python -m zarvis.costs --days 7 --by person
    PYTHONPATH=src python -m zarvis.costs --spikes

Every call through `llm.complete()` lands in `zarvis.llm_call`. Recording is
automatic rather than something each caller remembers, because the one thing
guaranteed about manual instrumentation is that the expensive path is the one
nobody instrumented. That already happened here: the deliberation room, by far
the costliest thing in the system, was the only module with no accounting, and
its cost had to be reconstructed afterwards from a markdown file.

WHAT IT IS FOR
--------------
Three questions, all Ryan's:

  * what is this costing
  * where are the spikes
  * are the spikes escalations, and were those escalations recommended by the
    review or asked for by hand

The third is why `mode` exists alongside `module`. A seven-call deep room the
review itself flagged is a system decision and its cost is the system's to
justify. The same room because Ryan typed the command is a choice he made
knowingly. Averaging them together would hide which one is growing.

Never let accounting break a run: every write here is inside a try/except. A
missing cost row is an annoyance, a failed morning is not.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager

import psycopg

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.costs")

# Set by `attribute()`. Ambient rather than threaded through every call site,
# because the alternative is adding two arguments to every `complete()` in the
# codebase and still missing one.
_CONTEXT: dict = {"module": "unknown", "mode": None, "label": None,
                  "person_id": None, "run_id": None}


@contextmanager
def attribute(module: str, *, mode: str | None = None, label: str | None = None,
              person_id: str | None = None, run_id: str | None = None):
    """Tag every LLM call made inside this block."""
    global _CONTEXT
    previous = dict(_CONTEXT)
    _CONTEXT = {"module": module, "mode": mode, "label": label,
                "person_id": person_id, "run_id": run_id}
    try:
        yield
    finally:
        _CONTEXT = previous


def record(completion) -> None:
    """Write one call to the ledger. Called by llm.complete()."""
    try:
        cost = completion.cost_usd()
        source = "billed" if completion.billed_ticks is not None else "table"
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into zarvis.llm_call
                  (workspace_id, module, mode, label, person_id, run_id, model_id,
                   input_tokens, cached_tokens, output_tokens, reasoning_tokens,
                   billed_ticks, cost_usd, cost_source)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    get_config().workspace_id,
                    _CONTEXT.get("module") or "unknown",
                    _CONTEXT.get("mode"), _CONTEXT.get("label"),
                    _CONTEXT.get("person_id"), _CONTEXT.get("run_id"),
                    completion.model_id,
                    completion.input_tokens, completion.cached_tokens,
                    completion.output_tokens, completion.reasoning_tokens,
                    completion.billed_ticks, round(cost, 6), source,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
        log.debug("cost recording failed: %s", exc)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _report(conn: psycopg.Connection, days: int, group: str) -> None:
    ws = get_config().workspace_id
    column = {
        "module": "c.module",
        "mode": "coalesce(c.mode, '-')",
        "model": "c.model_id",
        "person": "coalesce(p.full_name, '(none)')",
        "day": "date_trunc('day', c.at)::date::text",
    }[group]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select {column} as k,
                   count(*) calls,
                   sum(c.input_tokens) tin,
                   sum(c.cached_tokens) tcached,
                   sum(c.output_tokens) tout,
                   sum(c.reasoning_tokens) treason,
                   sum(c.cost_usd) usd
            from zarvis.llm_call c
            left join zarvis.person p on p.id = c.person_id
            where c.workspace_id = %s and c.at > now() - make_interval(days => %s)
            group by 1 order by usd desc nulls last
            """,
            (ws, days),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            select count(*) n, coalesce(sum(cost_usd), 0) usd,
                   count(*) filter (where cost_source = 'billed') billed
            from zarvis.llm_call
            where workspace_id = %s and at > now() - make_interval(days => %s)
            """,
            (ws, days),
        )
        total = cur.fetchone()

    if not rows:
        print(f"\n  No calls recorded in the last {days} days.\n")
        return

    print(f"\n  Last {days} days, by {group}\n")
    print(f"  {'':<26} {'calls':>6} {'input':>9} {'cached':>8} {'output':>8} "
          f"{'reason':>8} {'USD':>9}")
    for r in rows:
        print(f"  {str(r['k'])[:25]:<26} {r['calls']:>6,} {r['tin']:>9,} "
              f"{r['tcached']:>8,} {r['tout']:>8,} {r['treason']:>8,} "
              f"{float(r['usd'] or 0):>9.4f}")
    print(f"  {'-'*78}")
    print(f"  {'TOTAL':<26} {total['n']:>6,} {'':>9} {'':>8} {'':>8} {'':>8} "
          f"{float(total['usd']):>9.4f}")
    if total["n"]:
        print(f"\n  {total['billed']}/{total['n']} priced from the provider's own "
              f"billed figure; the rest from the local table.")
        daily = float(total["usd"]) / max(days, 1)
        print(f"  Running at ${daily:.4f}/day, about ${daily * 30:.2f}/month.\n")


def _spikes(conn: psycopg.Connection, days: int, limit: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.at, c.module, coalesce(c.mode, '-') mode, c.model_id,
                   coalesce(p.full_name, '') who,
                   c.input_tokens, c.output_tokens, c.reasoning_tokens, c.cost_usd
            from zarvis.llm_call c
            left join zarvis.person p on p.id = c.person_id
            where c.workspace_id = %s and c.at > now() - make_interval(days => %s)
            order by c.cost_usd desc limit %s
            """,
            (get_config().workspace_id, days, limit),
        )
        rows = cur.fetchall()

    print(f"\n  Most expensive individual calls, last {days} days\n")
    print(f"  {'when':<12} {'module':<10} {'mode':<18} {'who':<20} "
          f"{'in':>8} {'out':>7} {'USD':>9}")
    for r in rows:
        print(f"  {str(r['at'])[:10]:<12} {r['module'][:9]:<10} {r['mode'][:17]:<18} "
              f"{r['who'][:19]:<20} {r['input_tokens']:>8,} "
              f"{r['output_tokens']:>7,} {float(r['cost_usd']):>9.4f}")
    print()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--by", default="module",
        choices=("module", "mode", "model", "person", "day"),
    )
    parser.add_argument("--spikes", action="store_true",
                        help="the most expensive individual calls")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args(argv)

    with connect() as conn:
        if args.spikes:
            _spikes(conn, args.days, args.limit)
        else:
            _report(conn, args.days, args.by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
