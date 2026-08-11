"""Scoring tests, built from Ryan's real book.

The fixtures below are the fifteen relationships from the 2026-08-08 brain dump
(see zarvis/seed/01-people.md). That makes these regression tests against a
ranking a human actually validated, rather than tests of arithmetic against
itself.

If the model is retuned later, these will fail. That is the point — they encode
what the ranking was supposed to produce, so a change has to be deliberate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zarvis.scoring import (
    Candidate,
    decay_pressure,
    deadline_pressure,
    effective_ease,
    qualifies,
    rank_and_tier,
    rps,
    score,
    trial_urgency,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


# ---------------------------------------------------------------------------
# The real book
# ---------------------------------------------------------------------------

BOOK = {
    # Said "this afternoon or Tuesday", never booked. Both sides had sent
    # scheduling links; neither used them.
    "jj": Candidate(
        person_id="jj",
        play_key="convert_stated_meeting_intent",
        impact=9, self_sustaining=0.0, ease=9, energy_cost=0,
        signal_kinds=["meeting_intent_unconverted", "zero_approvals"],
        last_touch_at=ago(days=1),
    ),
    # Met Friday, blown away, onboarding never finished. ~72h half-life.
    "tamsin": Candidate(
        person_id="tamsin",
        play_key="onboarding_stall_fresh",
        impact=6, self_sustaining=0.0, ease=9, energy_cost=0,
        signal_kinds=["onboarding_incomplete"],
        last_touch_at=ago(days=3),
    ),
    # Four months cold. Sold on the idea with no demo; the demo now exists.
    # Chief AI Officer access. Ryan: "HUGE, if I can get him dislodged."
    "okafor": Candidate(
        person_id="okafor",
        play_key="revive_dormant_with_a_changed_fact",
        impact=10, self_sustaining=0.0, ease=6, energy_cost=1,
        signal_kinds=["changed_fact_match"],
        last_touch_at=ago(weeks=17),
    ),
    # Lost two paying clients to bad template strategies. Those are now fixed
    # and he may not know. Cash-constrained, paying above rate.
    "priya": Candidate(
        person_id="priya",
        play_key="revive_dormant_with_a_changed_fact",
        impact=8, self_sustaining=0.1, ease=6, energy_cost=1,
        signal_kinds=["churn_risk", "changed_fact_match", "zero_approvals"],
        last_touch_at=ago(days=6),
    ),
    # Trial expiring, one extension already burned, still zero approvals.
    # The honest action is a call he is not looking forward to.
    "toni": Candidate(
        person_id="toni",
        play_key="stated_intent_zero_usage",
        impact=7, self_sustaining=0.0, ease=5, energy_cost=2,
        signal_kinds=["zero_approvals"],
        signal_overrides={"trial_state": trial_urgency(3)},
        last_touch_at=ago(days=4),
    ),
    # Ten seats, reseller, affiliate, quitting his job for this. Biggest
    # account, and Ryan ranked him LAST for attention: he's a superfan.
    "kai": Candidate(
        person_id="kai",
        play_key="watch_only",
        impact=10, self_sustaining=0.9, ease=8, energy_cost=0,
        signal_kinds=["note"],
        last_touch_at=ago(days=5),
    ),
}


def scored_book() -> dict:
    return {key: score(c, now=NOW) for key, c in BOOK.items()}


# ---------------------------------------------------------------------------
# Formula mechanics
# ---------------------------------------------------------------------------


def test_effective_ease_floors_at_one():
    assert effective_ease(2, 3) == 1.0
    assert effective_ease(9, 0) == 9.0


def test_rps_matches_usi_ubi_range():
    worst = rps(ease=1, energy_cost=3, impact=1, urgency_score=1)
    best = rps(ease=10, energy_cost=0, impact=10, urgency_score=10)
    assert worst == pytest.approx(4.5, abs=0.01)
    assert best == pytest.approx(45.0, abs=0.01)


def test_urgency_is_max_not_sum():
    """Three moderate reasons is not more urgent than one deadline tomorrow."""
    many = deadline_pressure(["zero_approvals", "meeting_held", "note"])
    one = deadline_pressure(["payment_issue"])
    assert many == 6.0
    assert one == 9.0


def test_trial_urgency_climbs_as_the_clock_runs_out():
    assert trial_urgency(5) == 5.0
    assert trial_urgency(1) == 9.0
    assert trial_urgency(20) == 0.0


def test_computed_override_introduces_its_own_kind():
    """Regression: an override whose kind was absent from signal_kinds used to
    be silently ignored, so a trial deadline could vanish without erroring."""
    without = deadline_pressure(["zero_approvals"])
    with_trial = deadline_pressure(["zero_approvals"], {"trial_state": trial_urgency(1)})
    assert without == 6.0
    assert with_trial == 9.0


# ---------------------------------------------------------------------------
# Decay — the correction that came out of calibration
# ---------------------------------------------------------------------------


def test_fresh_contact_has_no_decay_pressure():
    assert decay_pressure(weeks_since_touch=0.1, impact=10) < 0.2


def test_normal_follow_up_gap_carries_almost_no_pressure():
    """Two weeks of silence is a normal gap, not a problem. The old exponential
    curve put 2.2 of pressure here on a top-impact person, which is noise on
    someone who is perfectly fine."""
    assert decay_pressure(weeks_since_touch=2, impact=10) < 1.0


def test_decay_is_effectively_maxed_by_three_months():
    """Cold is a STATE, not a pressure that keeps climbing. A contact at 17
    weeks and one at 40 weeks are not meaningfully different in urgency."""
    twelve_weeks = decay_pressure(12, impact=10)
    seventeen_weeks = decay_pressure(17, impact=10)
    forty_weeks = decay_pressure(40, impact=10)
    assert twelve_weeks > 9.5
    assert seventeen_weeks - twelve_weeks < 0.5
    assert forty_weeks - seventeen_weeks < 0.1


def test_decay_is_steepest_in_the_losing_them_window():
    """The curve must actually discriminate between 3 and 9 weeks — that is the
    window where the outcome is still in play."""
    early = decay_pressure(3, impact=10)
    late = decay_pressure(9, impact=10)
    assert late - early > 6.0


def test_self_sustaining_damps_decay():
    quiet = decay_pressure(8, impact=10, self_sustaining=0.0)
    healthy = decay_pressure(8, impact=10, self_sustaining=0.9)
    assert healthy == pytest.approx(quiet * 0.1, rel=1e-6)


def test_decay_scales_with_impact():
    big = decay_pressure(12, impact=10)
    small = decay_pressure(12, impact=3)
    assert big > small * 3


# ---------------------------------------------------------------------------
# Regressions against the validated ranking
# ---------------------------------------------------------------------------


def test_jj_ranks_first():
    """A stated meeting intent that never converted, on the hottest deal,
    with the last named option about to pass."""
    s = scored_book()
    assert max(s.values(), key=lambda x: x.rps).candidate.person_id == "jj"


def test_dormancy_alone_lifts_okafor_over_fresher_items():
    """THE correction. Under a deadline-only model he ranked 6th; Ryan put him
    3rd of everything he owns. Four months of silence on a 10-impact
    relationship has to outrank a 6-impact item with a live deadline."""
    s = scored_book()
    assert s["okafor"].decay_pressure > 8.0
    assert s["okafor"].deadline_pressure < s["okafor"].decay_pressure
    assert s["okafor"].rps > s["tamsin"].rps


def test_self_sustaining_superfan_never_enters_the_queue():
    """Biggest account, ranked last for attention. High impact must NOT float
    someone into the queue when nothing is happening and nothing is decaying."""
    s = scored_book()
    assert not qualifies(s["kai"])
    assert s["kai"].urgency < 2.5


def test_everyone_with_a_live_situation_qualifies():
    s = scored_book()
    for key in ("jj", "tamsin", "okafor", "priya", "toni"):
        assert qualifies(s[key]), f"{key} should be in the queue"


def test_expensive_but_urgent_still_makes_the_cut():
    """Toni is a call Ryan is dreading (ease 5, cost 2), so the ease-dominant
    formula pushes him down. He should still be in the queue — if he ever falls
    out entirely, the 1.5x coefficient is wrong and needs retuning."""
    ranked = rank_and_tier([v for v in scored_book().values() if qualifies(v)])
    toni = next(r for r in ranked if r.candidate.person_id == "toni")
    assert toni.tier in ("priority", "standard")


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def test_tiers_are_derived_from_rank():
    items = [
        score(
            Candidate(
                person_id=f"p{i}",
                play_key="x",
                impact=10 - (i * 0.2),
                self_sustaining=0.0,
                ease=9,
                energy_cost=0,
                signal_kinds=["reply_received"],
                last_touch_at=ago(days=2),
            ),
            now=NOW,
        )
        for i in range(45)
    ]
    ranked = rank_and_tier(items, tier_priority=5, tier_standard=10, tier_backlog=25)

    assert [r.tier for r in ranked[:5]] == ["priority"] * 5
    assert [r.tier for r in ranked[5:15]] == ["standard"] * 10
    assert [r.tier for r in ranked[15:40]] == ["backlog"] * 25
    assert all(r.tier == "dormant" for r in ranked[40:])
    assert [r.rank for r in ranked[:3]] == [1, 2, 3]


def test_ranking_is_monotonic_in_rps():
    ranked = rank_and_tier([v for v in scored_book().values() if qualifies(v)])
    scores = [r.rps for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_breakdown_exposes_every_term():
    """Ryan must be able to point at which term is wrong when the ranking is
    wrong. That is a better correction signal than 'this is bad'."""
    s = scored_book()["okafor"]
    b = s.breakdown()
    for field in ("impact", "ease", "energy_cost", "urgency",
                  "deadline_pressure", "decay_pressure", "rps"):
        assert field in b
