"""Ingest runner.

    python -m zarvis.ingest              # normal
    ZARVIS_DRY_RUN=1 python -m zarvis.ingest    # read everything, write nothing

Design notes worth keeping:

- **One source failing must not kill the run.** Gmail being down should not cost
  you the product signals. Each source is isolated; failures are recorded and
  surfaced, not raised.
- **A run row opens before any work and closes after.** That is what makes
  "ran and decided nothing" distinguishable from "didn't run", and the alert
  fires on zero *candidates*, not zero output. Silent no-ops are how cron agents
  die unnoticed.
- Ingest is **read-only against the product** and writes only to `zarvis.signal`.
  It never drafts, never scores, never contacts anyone.
"""

from __future__ import annotations

import logging
import sys
import urllib.request

from ..config import ConfigError, get_config
from ..db import close_run, connect, open_run
from ..signals import prime_identity_cache, write_signals
from . import calendar as calendar_ingest
from . import gmail as gmail_ingest
from . import product as product_ingest

log = logging.getLogger("zarvis.ingest")

SOURCES = (
    ("product", product_ingest.ingest),
    ("gmail", gmail_ingest.ingest),
    ("calendar", calendar_ingest.ingest),
)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _heartbeat(url: str | None, suffix: str = "") -> None:
    """Dead-man switch. A missing ping is the only reliable 'it stopped running'."""
    if not url:
        return
    try:
        urllib.request.urlopen(url + suffix, timeout=10)
    except Exception as exc:  # noqa: BLE001 - never let telemetry break the run
        log.warning("heartbeat ping failed: %s", exc)


def main() -> int:
    _configure_logging()

    try:
        cfg = get_config()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if cfg.kill_switch:
        # Checked before anything else, and again before any write elsewhere in
        # the system. One flag, always honoured.
        log.warning("ZARVIS_KILL_SWITCH is set. Exiting without doing anything.")
        return 0

    _heartbeat(cfg.heartbeat_url, "/start")

    with connect() as conn:
        run_id = open_run(conn, dry_run=cfg.dry_run)

        # One query instead of one per message / attendee / product row. The
        # identity table is small; the round trip to the pooler is not.
        prime_identity_cache(conn)

        collected = []
        failures: dict[str, str] = {}

        for name, ingest_fn in SOURCES:
            try:
                produced = ingest_fn(conn)
                collected.extend(produced)
                log.info("%s: %d signals", name, len(produced))
            except Exception as exc:  # noqa: BLE001 - isolate per source
                conn.rollback()
                failures[name] = f"{type(exc).__name__}: {exc}"
                log.exception("%s ingest failed", name)

        if cfg.dry_run:
            log.info("DRY RUN: %d signals collected, nothing written", len(collected))
            by_kind: dict[str, int] = {}
            for signal in collected:
                by_kind[signal.kind] = by_kind.get(signal.kind, 0) + 1
            for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
                log.info("  %-24s %d", kind, count)
            written = 0
        else:
            written = write_signals(conn, collected)

        status = "completed" if not failures else "failed"
        close_run(
            conn,
            run_id,
            status=status,
            signals_seen=written,
            suppressed=failures or None,
            error="; ".join(f"{k}: {v}" for k, v in failures.items()) or None,
        )

    # Only ping success when every source actually worked. A partial run that
    # reports healthy is worse than one that reports nothing.
    _heartbeat(cfg.heartbeat_url, "" if not failures else "/fail")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
