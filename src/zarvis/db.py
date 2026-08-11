"""Database access. Thin on purpose.

Zarvis holds a role with SELECT on `zarvis.v_user_state` and write access confined
to `zarvis.*`. It must never hold write access to any `public.*` table — ingest is
read-only against the product, always.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .config import expand_sql, get_config

log = logging.getLogger(__name__)


class _SentinelCursor(psycopg.Cursor):
    """A cursor that expands `/*SYNTH:col*/` before the query is sent.

    Done here rather than at each call site so that a new query carrying the
    marker is handled without anyone remembering to wrap it. Five sites need it
    today and the sixth is the one that would have been forgotten.
    """

    def execute(self, query, params=None, **kwargs):
        if isinstance(query, str) and "/*SYNTH:" in query:
            query = expand_sql(query, escaped=params is not None)
        return super().execute(query, params, **kwargs)


def _connect_kwargs(url: str) -> dict:
    """Connection options, adjusted for how we are reaching Postgres.

    Supabase gives three routes and they are NOT interchangeable:

      db.<ref>.supabase.co:5432          direct. IPv6-only unless the project
                                         has the paid IPv4 add-on, so it simply
                                         fails to resolve on an IPv4-only host.
      aws-0-<region>.pooler...:5432      Supavisor SESSION mode. IPv4, and
                                         prepared statements work normally.
      aws-0-<region>.pooler...:6543      Supavisor TRANSACTION mode. IPv4, but
                                         prepared statements are NOT supported.

    psycopg3 silently promotes a query to a prepared statement after its fifth
    execution. Against transaction mode that raises at runtime — not at connect —
    so a smoke test passes and the first real run with a loop in it breaks. Rather
    than rely on remembering, detect the port and disable preparation.
    """
    kwargs: dict = {"row_factory": dict_row, "cursor_factory": _SentinelCursor}
    if ":6543" in url:
        kwargs["prepare_threshold"] = None
        log.info("transaction-mode pooler detected; prepared statements disabled")
    return kwargs


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """One connection per run. Runs are sub-second; a pool is not warranted."""
    cfg = get_config()
    with psycopg.connect(cfg.database_url, **_connect_kwargs(cfg.database_url)) as conn:
        # Belt and braces: the role should already carry this, but a runaway
        # query against the production database is not a risk worth leaving to
        # role configuration alone.
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '30s'")
        yield conn


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------
# A run row is opened before any work and closed after. This is what makes
# "ran and decided nothing" distinguishable from "didn't run at all" — the
# silent no-op is the failure mode that kills cron agents quietly, and the
# alert fires on ZERO CANDIDATES, not zero output.


def open_run(conn: psycopg.Connection, *, dry_run: bool) -> str:
    row = fetch_one(
        conn,
        """
        insert into zarvis.run (workspace_id, dry_run)
        values (%(workspace_id)s, %(dry_run)s)
        returning id
        """,
        {"workspace_id": get_config().workspace_id, "dry_run": dry_run},
    )
    assert row is not None
    conn.commit()
    log.info("run %s opened (dry_run=%s)", row["id"], dry_run)
    return str(row["id"])


def close_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str = "completed",
    signals_seen: int | None = None,
    candidates: int | None = None,
    drafted: int | None = None,
    queue: dict | None = None,
    suppressed: dict | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update zarvis.run set
                finished_at  = now(),
                status       = %(status)s,
                signals_seen = %(signals_seen)s,
                candidates   = %(candidates)s,
                drafted      = %(drafted)s,
                queue        = %(queue)s,
                suppressed   = %(suppressed)s,
                cost_usd     = %(cost_usd)s,
                error        = %(error)s
            where id = %(run_id)s
            """,
            {
                "run_id": run_id,
                "status": status,
                "signals_seen": signals_seen,
                "candidates": candidates,
                "drafted": drafted,
                "queue": Json(queue) if queue else None,
                "suppressed": Json(suppressed) if suppressed else None,
                "cost_usd": cost_usd,
                "error": error,
            },
        )
    conn.commit()
    log.info("run %s closed status=%s signals=%s", run_id, status, signals_seen)
