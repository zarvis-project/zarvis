"""The morning run. One command for Task Scheduler or cron.

    PYTHONPATH=src python -m zarvis.daily

Runs ingest, then queue reconciliation. Compose slots in here when it exists.

THE ONCE-A-DAY GUARD
--------------------
This is scheduled to fire at 08:00 and then retry every 30 minutes, because the
laptop it runs on is frequently asleep at 08:00 and a missed morning is the one
failure this system cannot tolerate — the whole point is that nobody falls
through the cracks, and a queue that silently skipped Tuesday is worse than no
queue at all.

Retrying on a schedule means the command runs many times a day. So it has to
know whether the work is already done. Without that:

  * ingest is idempotent, but it still pays for a full Gmail scan every 30
    minutes;
  * compose is NOT idempotent in cost — regenerating drafts all day is real
    money for no benefit;
  * and any draft Ryan has already edited would be at risk of being rewritten
    underneath him.

So a successful run records the local date, and any later run that same day
exits immediately. A FAILED run records nothing, which is what makes the retry
loop work: it keeps trying until a run actually succeeds, then stops doing work
for the rest of the day.

`--force` overrides the guard for manual runs.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.daily")

# Ryan is in Brazil and unqualified times in this project are BRT. The guard has
# to key on HIS calendar day, not UTC — a run at 21:30 local is 00:30 UTC the
# next day, and a UTC-keyed guard would let the following morning's run through
# as "a new day" while blocking nothing useful.
DEFAULT_TZ = "America/Fortaleza"

CURSOR_SOURCE = "daily"
CURSOR_KEY = "last_success"


def _local_date(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


def _last_success(conn: psycopg.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select value from zarvis.cursor
            where workspace_id = %s and source = %s and key = %s
            """,
            (get_config().workspace_id, CURSOR_SOURCE, CURSOR_KEY),
        )
        row = cur.fetchone()
    return row["value"] if row else None


def _record_success(conn: psycopg.Connection, day: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into zarvis.cursor (workspace_id, source, key, value)
            values (%s, %s, %s, %s)
            on conflict (workspace_id, source, key)
            do update set value = excluded.value, updated_at = now()
            """,
            (get_config().workspace_id, CURSOR_SOURCE, CURSOR_KEY, day),
        )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Zarvis daily run")
    parser.add_argument(
        "--force", action="store_true", help="run even if today already succeeded"
    )
    parser.add_argument("--tz", default=os.environ.get("ZARVIS_TZ", DEFAULT_TZ))
    args = parser.parse_args(argv)

    cfg = get_config()
    if cfg.kill_switch:
        log.warning("ZARVIS_KILL_SWITCH is set. Exiting without doing anything.")
        return 0

    # A dry run must never satisfy the guard — it does no work, and recording it
    # as success would skip the real run for the rest of the day.
    if cfg.dry_run and not args.force:
        log.error(
            "ZARVIS_DRY_RUN is set. The scheduled run must write, or the morning "
            "is silently skipped. Set ZARVIS_DRY_RUN=0 in the task, or pass --force "
            "to dry-run deliberately."
        )
        return 2

    today = _local_date(args.tz)

    with connect() as conn:
        done = _last_success(conn)
        if done == today and not args.force:
            log.info("already completed for %s (%s). Nothing to do.", today, args.tz)
            return 0

    # Imported here rather than at module scope so that a broken source cannot
    # stop the guard above from running — the guard is what keeps a retry loop
    # from turning into a runaway.
    from .brief import main as brief_main
    from .compose import main as compose_main
    from .verdict import main as verdict_main
    from .deliver import main as deliver_main
    from .fireflies import main as fireflies_main
    from .resolve import main as resolve_main
    from .hydrate import main as hydrate_main
    from .ingest.__main__ import main as ingest_main
    from .queue import main as queue_main
    from .review import main as review_main

    log.info("daily run for %s (%s) starting", today, args.tz)

    # Before anything else: what happened to yesterday's drafts. Gmail deletes
    # a draft when it is sent, so this has to run before compose can supersede
    # anything, and a verdict is the one piece of data that cannot be recovered
    # later.
    if verdict_main([]) != 0:
        log.warning("verdict poll failed; feedback for yesterday is missing")

    rc = ingest_main()
    if rc != 0:
        log.error("ingest failed (rc=%d). Not recording success; will retry.", rc)
        return rc

    rc = queue_main([])
    if rc != 0:
        log.error("queue failed (rc=%d). Not recording success; will retry.", rc)
        return rc

    # Learn second addresses from the calendar before fetching anything, so a
    # newly discovered alias is included in the same night's mail and meeting
    # pulls rather than waiting for tomorrow. Exact full-name matches only;
    # near-matches are reported for Ryan and never written.
    if resolve_main(["--harvest", "--days", "120", "--apply"]) != 0:
        log.warning("identity harvest failed; aliases may be stale")

    # Fetch the mail for whoever the queue surfaced, before anything reasons
    # about them. Incremental, so most mornings this is a handful of messages.
    # Not fatal: a Gmail hiccup should thin the review's context, not cancel the
    # morning.
    if hydrate_main(["--queue"]) != 0:
        log.warning("hydration failed; the review runs on a thinner record")

    # Calls, not just mail. Both are best-effort for the same reason: a thinner
    # record is a worse review, a failed record is no morning at all.
    if fireflies_main(["--queue"]) != 0:
        log.warning("fireflies sync failed; meeting context will be stale")

    # The decision layer. Everything downstream honours what it decides.
    rc = review_main([])
    if rc != 0:
        log.error("review failed (rc=%d). Not recording success; will retry.", rc)
        return rc

    # A ranked list with no drafts is not the morning deliverable, so a compose
    # failure fails the whole run and the 30-minute ticks keep trying. That is
    # deliberately noisy: if the API key is wrong the log will say so every half
    # hour, which is the correct volume for "Zarvis produced nothing today".
    rc = compose_main([])
    if rc != 0:
        log.error("compose failed (rc=%d). Not recording success; will retry.", rc)
        return rc

    # Put the approved drafts in the mailbox. Last, and fatal if it fails: a
    # morning that ranked, decided and composed but delivered nothing is
    # indistinguishable from a morning that did not run, because the only
    # surface Ryan actually looks at is his inbox.
    rc = deliver_main([])
    if rc != 0:
        log.error("deliver failed (rc=%d). Not recording success; will retry.", rc)
        return rc

    # Tell Ryan what happened. Last, and deliberately NOT fatal: the work is
    # already done and safely recorded, and failing the run over an
    # undelivered notification would make the retry loop redo a morning that
    # succeeded.
    if brief_main([]) != 0:
        log.warning("brief not sent; the run itself succeeded")

    if not cfg.dry_run:
        with connect() as conn:
            _record_success(conn, today)
        log.info("daily run for %s complete", today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
