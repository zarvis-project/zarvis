"""Queue reconciliation: signals in, a ranked standing agenda out.

    python -m zarvis.queue --dry-run

This is the piece that turns everything else into a decision. It runs after
ingest and before compose.

**The queue is re-scored, not regenerated.** That distinction is the whole
design:

  * High-value work at position 6 does not evaporate because the cap is 5. It
    sits in Standard and climbs on its own as decay pressure rises.
  * Runs stop re-deriving the same reasoning every morning. If the evidence hash
    is unchanged, the rationale is still accurate and no tokens are spent.
  * `snoozed` and `dismissed` stay distinct. Most skips are deferrals, and
    logging "not today" as a rejection would fill the case log with false
    negatives before the learning loop ever got going.

Tiers are derived from rank, never stored as intent: 1-5 priority, 6-15
standard, 16-40 backlog, beyond that dormant but still scored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Json

from .config import get_config, synthetic_email_exclusion
from .db import close_run, connect, open_run
from .scoring import Candidate, qualifies, rank_and_tier, score, trial_urgency

log = logging.getLogger("zarvis.queue")

# A person with no live signal but a long silence still deserves consideration.
# This pseudo-kind lets the dormancy case reach a play like any other trigger.
DORMANT_KIND = "dormant"
DORMANT_AFTER_WEEKS = 3.0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_plays(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, key, name, channel_hint, ease, energy_cost, base_urgency,
                   trigger_kinds, precedence, drafts
            from zarvis.play
            where status = 'active'
              and (workspace_id is null or workspace_id = %s)
            order by precedence desc
            """,
            (get_config().workspace_id,),
        )
        return cur.fetchall()


def _load_people(conn: psycopg.Connection) -> dict[str, dict]:
    """Everyone eligible, with their live signals and last touch.

    `last_touch` is a real touch or it is NULL. It used to fall back to
    `person.created_at`, to stop 73 freshly-seeded users from all reading as
    "never contacted, maximally decayed" on day one. Sound instinct, wrong
    instrument: seed.py sets created_at to now(), so every person looked
    contacted-today, decay came out ~0 for 74 of 77 people, and the mechanism the
    system was built around was switched off without a word. The first real
    ranking showed twenty people tied at one score with the tier boundary cutting
    through the middle of the tie, which is what a dead input looks like from the
    outside.

    The day-one flood was a real problem with a better answer than a fabricated
    date: read the actual history out of Gmail. See backfill.py. Once that has
    run, a NULL here means "no contact in the backfill window", which is a
    finding rather than a gap.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select
              p.id, p.full_name, p.impact, p.self_sustaining, p.path_override,
              p.suppressed_until, p.suppress_reason, p.preferred_channel,
              (select max(t.at) from zarvis.touch t where t.person_id = p.id)
                as last_touch,
              exists (
                select 1 from zarvis.person_identity i
                where i.person_id = p.id
                  and i.kind = 'email'
                  and i.superseded_at is null
                  /*SYNTH:i.value*/
              ) as has_real_email,
              exists (
                select 1 from zarvis.person_identity i
                where i.person_id = p.id
                  and i.kind = 'email'
                  and lower(i.value) = lower(%s)
              ) as is_operator
            from zarvis.person p
            where p.workspace_id = %s
              and (p.path_override is null or p.path_override <> 'dnc')
            """,
            (get_config().google_impersonate, get_config().workspace_id),
        )
        return {str(r["id"]): r for r in cur.fetchall()}


def _load_live_signals(conn: psycopg.Connection) -> dict[str, list[dict]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, person_id, kind, value, observed_at, is_private,
                   authored_by, body
            from zarvis.signal
            where workspace_id = %s
              and person_id is not null
              and (expires_at is null or expires_at > now())
            order by observed_at desc
            """,
            (get_config().workspace_id,),
        )
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in cur.fetchall():
            grouped[str(row["person_id"])].append(row)
        return grouped


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


# Some signals are not a reason to act. They are a fact about the person that
# changes which actions are available at all.
#
# `churned` is the one that matters today. Twelve of the first twenty ranked
# candidates were people carrying BOTH `churned` and `onboarding_incomplete`, and
# because precedence is a plain maximum, `onboarding_stall_fresh` (70) beat
# `win_back_churned` (25). The queue was about to ask Ryan to nudge a dozen
# departed customers through an onboarding flow they had already left — using a
# play whose name contains the word "fresh".
#
# Raising win_back's precedence above onboarding would not fix it; it would just
# move the collision. The relationship is not "which trigger is more urgent", it
# is "this person is gone, so only win-back moves apply."
STATE_GATES = {"churned"}


def _pick_play(plays: list[dict], kinds: set[str]) -> dict | None:
    """Highest-precedence play whose triggers intersect this person's signals.

    One play per person per run. Several plays applying at once is common —
    trial expiring AND zero approvals AND parked — and firing all of them would
    put the same human in the queue three times. Precedence picks the move that
    matters; the others stay visible in the evidence.

    A live state gate narrows the field before precedence is consulted, so a
    high-precedence play cannot fire against someone it does not apply to.
    """
    gates = kinds & STATE_GATES
    eligible = [
        play for play in plays if gates & set(play["trigger_kinds"] or [])
    ] if gates else plays

    for play in eligible:  # already ordered by precedence desc
        if kinds & set(play["trigger_kinds"] or []):
            return play
    return None


def _signal_overrides(signals: list[dict]) -> dict[str, float]:
    """Urgencies that must be computed rather than looked up.

    A trial is more urgent at one day left than at five.
    """
    overrides: dict[str, float] = {}
    for sig in signals:
        if sig["kind"] == "trial_state":
            days_left = (sig["value"] or {}).get("days_left")
            if isinstance(days_left, int):
                overrides["trial_state"] = trial_urgency(days_left)
    return overrides


def _evidence(signals: list[dict]) -> tuple[dict, str]:
    """Evidence bundle plus a hash of it.

    The hash is what makes "do not re-derive it every morning" cheap: if it has
    not moved, the rationale from the last run is still accurate and gets reused
    instead of regenerated.

    Private signals are counted but their content is excluded — they inform
    ranking and must never reach the drafting model.
    """
    public = [s for s in signals if not s["is_private"]]
    bundle = {
        "signals": [
            {
                "id": str(s["id"]),
                "kind": s["kind"],
                "observed_at": s["observed_at"].isoformat(),
                "value": s["value"],
                # Who wrote this. Calendly "anything to share?" fields are filled
                # in by whoever booked, which is often Ryan, and quoting his own
                # words back to a prospect as insight about them would be the
                # most credibility-destroying thing this system could do. The
                # composer filters on it; it has to survive into the bundle.
                "authored_by": s.get("authored_by"),
                # Their actual words, when the signal carries them. Truncated:
                # long quoted chains add tokens and nothing else.
                "body": (s.get("body") or "")[:4000] or None,
            }
            for s in public
        ],
        "private_count": len(signals) - len(public),
    }
    digest = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, default=str).encode()
    ).hexdigest()
    return bundle, digest


def build_candidates(
    people: dict[str, dict], signals: dict[str, list[dict]], plays: list[dict], now: datetime
) -> list[tuple[Candidate, dict, dict, str]]:
    """-> (candidate, person, play, evidence_hash), plus evidence stashed on the play dict."""
    out = []
    no_address = 0

    for person_id, person in people.items():
        # Deliberate silence is a state, not an absence. Held people are shown
        # in the brief with their resume date, but they do not compete for a
        # queue slot.
        if person["suppressed_until"] and person["suppressed_until"] > now:
            continue
        # A bespoke path means Ryan is driving. Checked before any other rule.
        if person["path_override"] == "custom":
            continue
        # Ryan is in his own book, seeded from the product like any other user,
        # carrying 4,263 touches with himself.
        if person["is_operator"]:
            continue
        # No address, no email play. This is an eligibility gate rather than a
        # scoring penalty, because the distinction is structural: the output of
        # this queue is a draft email, and 27 of 77 people have only a synthetic
        # `managed-<uuid>@managed.example.internal` address that the product
        # manufactured for an agency-run account. There is nowhere to send it.
        #
        # Scoring them anyway is how seven of the ten top-ranked candidates ended
        # up welded together at an identical 34.5 — same default impact, same
        # play, same assumed decay, no way to act on any of them.
        #
        # Their signals are real and stay in the database. What is missing is the
        # routing: a managed account's churn risk belongs in front of whoever
        # manages it, and `zarvis.manages` already models that edge. Until that
        # is built, this is a known gap that gets counted out loud rather than a
        # queue full of people who cannot be contacted.
        if not person["has_real_email"]:
            no_address += 1
            continue

        person_signals = signals.get(person_id, [])
        kinds = {s["kind"] for s in person_signals}

        # No touch on record means no contact inside the backfill window, which
        # is dormancy by any reading. Candidate.weeks_since_touch treats None the
        # same way, so the two agree instead of one quietly overruling the other.
        if person["last_touch"] is None:
            kinds.add(DORMANT_KIND)
        else:
            weeks_quiet = (now - person["last_touch"]).total_seconds() / (7 * 86400)
            if weeks_quiet >= DORMANT_AFTER_WEEKS:
                kinds.add(DORMANT_KIND)

        play = _pick_play(plays, kinds)
        if play is None:
            continue

        bundle, digest = _evidence(person_signals)

        candidate = Candidate(
            person_id=person_id,
            play_key=play["key"],
            impact=float(person["impact"] or 5),
            self_sustaining=float(person["self_sustaining"] or 0),
            ease=float(play["ease"]),
            energy_cost=float(play["energy_cost"]),
            signal_kinds=sorted(kinds),
            last_touch_at=person["last_touch"],
            signal_overrides=_signal_overrides(person_signals),
        )
        out.append((candidate, person, {**play, "_evidence": bundle}, digest))

    if no_address:
        log.warning(
            "%d people skipped: no real email address (agency-managed accounts). "
            "Their signals are live but unroutable until manager routing exists.",
            no_address,
        )
    return out


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(conn: psycopg.Connection, *, dry_run: bool) -> dict:
    cfg = get_config()
    now = datetime.now(UTC)

    plays = _load_plays(conn)
    people = _load_people(conn)
    signals = _load_live_signals(conn)
    log.info(
        "loaded %d plays, %d people, %d people carrying live signals",
        len(plays), len(people), len(signals),
    )

    raw = build_candidates(people, signals, plays, now)
    scored = [(score(c, now=now), person, play, digest) for c, person, play, digest in raw]

    live = [t for t in scored if qualifies(t[0])]
    filtered_out = len(scored) - len(live)

    ranked = rank_and_tier(
        [t[0] for t in live],
        tier_priority=cfg.tier_priority,
        tier_standard=cfg.tier_standard,
        tier_backlog=cfg.tier_backlog,
    )
    by_person = {t[0].candidate.person_id: t for t in live}

    counts = {"created": 0, "updated": 0, "closed": 0, "filtered_below_floor": filtered_out}
    tiers: dict[str, int] = defaultdict(int)
    seen_keys: set[tuple[str, str]] = set()

    with conn.cursor() as cur:
        for item in ranked:
            person_id = item.candidate.person_id
            _, person, play, digest = by_person[person_id]
            tiers[item.tier] += 1
            seen_keys.add((person_id, str(play["id"])))

            cur.execute(
                """
                select id, evidence_hash, times_in_priority, scored_runs
                from zarvis.queue_item
                where workspace_id = %s and person_id = %s and play_id = %s
                  and status in ('open', 'snoozed')
                """,
                (cfg.workspace_id, person_id, play["id"]),
            )
            existing = cur.fetchone()

            fields = {
                "rps": item.rps,
                "urgency": item.urgency,
                "impact": item.candidate.impact,
                "ease": item.candidate.ease,
                "energy_cost": item.candidate.energy_cost,
                "deadline_pressure": item.deadline_pressure,
                "decay_pressure": item.decay_pressure,
                "score_breakdown": Json(item.breakdown()),
                "tier": item.tier,
                "rank": item.rank,
                "evidence": Json(play["_evidence"]),
                "evidence_hash": digest,
                "channel_hint": person["preferred_channel"] or play["channel_hint"],
            }

            if existing:
                # Keep the rationale when the evidence has not moved. This is
                # the whole point of the hash: no tokens spent re-deriving a
                # conclusion that is still true.
                stale = existing["evidence_hash"] != digest
                cur.execute(
                    """
                    update zarvis.queue_item set
                        rps = %(rps)s, urgency = %(urgency)s, impact = %(impact)s,
                        ease = %(ease)s, energy_cost = %(energy_cost)s,
                        deadline_pressure = %(deadline_pressure)s,
                        decay_pressure = %(decay_pressure)s,
                        score_breakdown = %(score_breakdown)s,
                        tier = %(tier)s, rank = %(rank)s,
                        evidence = %(evidence)s, evidence_hash = %(evidence_hash)s,
                        channel_hint = %(channel_hint)s,
                        rationale = case when %(stale)s then null else rationale end,
                        last_scored_at = now(),
                        scored_runs = scored_runs + 1,
                        times_in_priority = times_in_priority
                            + case when %(tier)s = 'priority' then 1 else 0 end,
                        status = case
                            when status = 'snoozed' and snooze_until <= now() then 'open'
                            else status end
                    where id = %(id)s
                    """,
                    {**fields, "id": existing["id"], "stale": stale},
                )
                counts["updated"] += 1
            else:
                cur.execute(
                    """
                    insert into zarvis.queue_item (
                        workspace_id, person_id, play_id, rps, urgency, impact,
                        ease, energy_cost, deadline_pressure, decay_pressure,
                        score_breakdown, tier, rank, evidence, evidence_hash,
                        channel_hint, headline, status, last_scored_at,
                        scored_runs, times_in_priority
                    ) values (
                        %(ws)s, %(person_id)s, %(play_id)s, %(rps)s, %(urgency)s,
                        %(impact)s, %(ease)s, %(energy_cost)s,
                        %(deadline_pressure)s, %(decay_pressure)s,
                        %(score_breakdown)s, %(tier)s, %(rank)s, %(evidence)s,
                        %(evidence_hash)s, %(channel_hint)s, %(headline)s,
                        'open', now(), 1,
                        case when %(tier)s = 'priority' then 1 else 0 end
                    )
                    """,
                    {
                        **fields,
                        "ws": cfg.workspace_id,
                        "person_id": person_id,
                        "play_id": play["id"],
                        # Placeholder until compose writes a real one. Never
                        # shown to anyone but Ryan.
                        "headline": f"{person['full_name']} — {play['name']}",
                    },
                )
                counts["created"] += 1

        # Anything still open that no longer qualifies has had its situation
        # resolved — they replied, the trial ended, the account got unparked.
        # Closed as 'expired', not 'dismissed': nobody made a judgment, so this
        # must never reach the case log as a rejection.
        cur.execute(
            """
            select id, person_id, play_id from zarvis.queue_item
            where workspace_id = %s and status = 'open'
            """,
            (cfg.workspace_id,),
        )
        for row in cur.fetchall():
            if (str(row["person_id"]), str(row["play_id"])) in seen_keys:
                continue
            cur.execute(
                """
                update zarvis.queue_item
                set status = 'expired', closed_at = now()
                where id = %s
                """,
                (row["id"],),
            )
            counts["closed"] += 1

    if dry_run:
        conn.rollback()
        log.info("DRY RUN — rolled back, nothing persisted")
    else:
        conn.commit()

    counts["tiers"] = dict(tiers)
    # Carried in memory rather than re-queried, so a dry run can still show the
    # ranking. Re-reading queue_item after a rollback would return the previous
    # run's rows, which is worse than showing nothing.
    counts["_ranked"] = [
        {
            "rank": item.rank,
            "tier": item.tier,
            "rps": item.rps,
            "urgency": item.urgency,
            "decay": item.decay_pressure,
            "deadline": item.deadline_pressure,
            "name": by_person[item.candidate.person_id][1]["full_name"],
            "play": item.candidate.play_key,
            "signals": ",".join(item.candidate.signal_kinds[:3]),
        }
        for item in ranked
    ]
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stderr
    )
    parser = argparse.ArgumentParser(description="Re-score the standing agenda")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", type=int, default=15, help="print the top N")
    args = parser.parse_args(argv)

    cfg = get_config()
    if cfg.kill_switch:
        log.warning("ZARVIS_KILL_SWITCH is set. Exiting.")
        return 0

    with connect() as conn:
        run_id = open_run(conn, dry_run=args.dry_run)
        try:
            counts = reconcile(conn, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            close_run(conn, run_id, status="failed", error=str(exc))
            raise

        log.info(
            "created %(created)d  updated %(updated)d  closed %(closed)d  "
            "below-floor %(filtered_below_floor)d", counts,
        )
        log.info("tiers: %s", counts["tiers"])

        if not args.dry_run:
            close_run(
                conn, run_id,
                candidates=counts["created"] + counts["updated"],
                queue=counts["tiers"],
            )

        if args.show:
            rows = counts["_ranked"][: args.show]
            print(
                f"\n{'#':<4}{'who':<26}{'play':<34}"
                f"{'RPS':>7}{'urg':>6}{'dline':>7}{'decay':>7}  {'tier':<9}signals"
            )
            print("-" * 118)
            last_tier = None
            for r in rows:
                if last_tier and r["tier"] != last_tier:
                    print("-" * 118)
                last_tier = r["tier"]
                print(
                    f"{r['rank']:<4}{(r['name'] or '')[:24]:<26}{r['play'][:32]:<34}"
                    f"{r['rps']:>7.1f}{r['urgency']:>6.1f}{r['deadline']:>7.1f}"
                    f"{r['decay']:>7.1f}  {r['tier']:<9}{r['signals']}"
                )
            print(f"\n{len(counts['_ranked'])} in queue · {counts['tiers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
