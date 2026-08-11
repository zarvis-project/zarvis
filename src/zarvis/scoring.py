"""Ranking. Pure functions, no database, no LLM.

The binding constraint on this whole system is Ryan's attention, not compute.
A cap is the wrong instrument for allocating a scarce resource — it truncates an
arbitrary list instead of choosing the best five. So the queue is *ranked*, and
the tiers fall out of the rank.

The formula is the USI/UBI skeleton he already uses and has intuitions calibrated
to, with the inputs reinterpreted for relationships:

    Eff = max(1, Ease - Cost)
    t   = (Eff - 1) / 9
    RPS = 1.5*Eff + (2 - t)*Impact + (1 + t)*Urgency

Impact is a property of the PERSON. Ease and Cost are properties of the MOVE.
Urgency is the interesting one — see below.

Everything here is deliberately testable without infrastructure, because this is
the part most likely to be wrong and the part worth arguing with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Urgency
# ---------------------------------------------------------------------------
# The first version of this model treated urgency as deadline proximity alone.
# Calibrating against fifteen real relationships exposed the hole immediately: a
# high-value prospect dormant four months scored LOW urgency because no date was
# attached, and Ryan ranked dislodging him third out of everything he owns.
#
# Dormancy is not the absence of urgency. It is a slower, invisible kind — every
# week cold, re-entry gets harder and the "why now" problem gets worse, and
# unlike a deadline nothing ever fires to tell you. That silent decay is exactly
# the failure this system exists to prevent.

# Decay is a LOGISTIC curve, not an exponential one, because the real dynamics
# have three phases and an exponential only captures two:
#
#   0-3 weeks   a normal follow-up gap. Nothing is wrong. Pressure ~0.
#   3-9 weeks   the window where you are actually losing them. Steep.
#   9+ weeks    cold. This is a STATE, not a pressure that keeps climbing.
#
# The plateau is the important part. Two dormant contacts at 17 and 40 weeks are
# not meaningfully different in urgency — both are "cold, needs a changed fact",
# and what separates them is who is worth more, which the impact term already
# handles. An exponential kept creeping (88% at four months, still rising) and
# also carried a tail at the near end, putting real pressure on people contacted
# a fortnight ago who were perfectly fine.
DECAY_MIDPOINT_WEEKS = 6.0   # 50% decayed — "you are losing them" point
DECAY_STEEPNESS = 0.7        # yields ~0 at 1 week, ~0.8 at 8, effectively 1.0 by 12

# Baseline deadline pressure per signal kind, 0..10. The pressure a person
# carries is the MAX across their live signals, not the sum: three moderate
# reasons to reach out is not more urgent than one deadline tomorrow.
SIGNAL_URGENCY: dict[str, float] = {
    "meeting_intent_unconverted": 9.0,   # said yes to meeting, never booked
    "payment_issue": 9.0,
    "reply_received": 8.0,               # they wrote, nobody answered
    "days_prepped_zero": 8.0,            # their pipeline has actually stalled
    "account_parked": 8.0,
    # Retaining paying revenue is cheaper than acquiring it, so churn on an
    # existing customer outranks almost every acquisition signal. Raised from
    # 8.0 on Ryan's judgement: the model had Priya 4th and he had him 2nd, and
    # the principle he was applying was this one.
    "churn_risk": 9.5,                   # STILL PAYING, and visibly coming apart
    # Already gone. A win-back, not a rescue — there is no deadline and no
    # emergency, and scoring it like one buried live deals under resolved ones
    # on the first real run.
    "churned": 3.0,
    "list_exhausted": 7.0,
    "onboarding_incomplete": 7.0,
    "meeting_held": 6.0,                 # follow up while it is warm
    "zero_approvals": 6.0,
    "first_acceptance": 5.0,             # a good moment to reinforce
    "changed_fact_match": 5.0,           # something shipped that answers their objection
    "meeting_scheduled": 3.0,            # prep, not outreach
    "note": 2.0,
}
DEFAULT_SIGNAL_URGENCY = 3.0

# Below this, there is genuinely nothing to say and the person should not be in
# the queue at all, however valuable they are. This is what keeps the biggest
# healthy account off the list every single day: a ten-seat evangelist with no
# live signal and no decay has nothing that needs doing, and ranking him on
# impact alone would crowd out everyone who actually needs something.
#
# Those people are not ignored — they are `watch_only`, which escalates when a
# leading indicator fires rather than generating a daily agenda item.
MIN_URGENCY_TO_QUEUE = 2.5


def decay_pressure(
    weeks_since_touch: float, impact: float, self_sustaining: float = 0.0
) -> float:
    """Urgency that comes from silence rather than from a date.

    Scaled by impact because a top prospect going cold matters more than a cold
    lead going colder, and damped by self_sustaining so a healthy evangelist does
    not crowd the queue purely by being valuable.

    Returns 0..10.
    """
    if weeks_since_touch <= 0:
        return 0.0
    saturation = 1.0 / (
        1.0 + math.exp(-DECAY_STEEPNESS * (weeks_since_touch - DECAY_MIDPOINT_WEEKS))
    )
    return 10.0 * saturation * (impact / 10.0) * (1.0 - self_sustaining)


def trial_urgency(days_left: int) -> float:
    """A trial ending is a real clock. 5 days out -> 5, tomorrow -> 9."""
    return max(0.0, min(10.0, 10.0 - days_left))


def deadline_pressure(
    signal_kinds: list[str], overrides: dict[str, float] | None = None
) -> float:
    """Max urgency across live signals. Max, not sum — see the note above.

    `overrides` carries urgencies that must be computed rather than looked up —
    a trial is more urgent at one day left than at five, so its pressure is a
    function of the data, not a constant.

    Overrides INTRODUCE their kind rather than only modifying one already in the
    list. An earlier version took the max over `signal_kinds` alone and silently
    ignored any override whose kind was absent, which meant a computed trial
    deadline could vanish without a word. Silent no-ops in a ranking function are
    the worst kind of bug: nothing errors, the number is just quietly wrong.
    """
    overrides = overrides or {}
    kinds = set(signal_kinds) | set(overrides)
    if not kinds:
        return 0.0
    return max(
        overrides.get(kind, SIGNAL_URGENCY.get(kind, DEFAULT_SIGNAL_URGENCY))
        for kind in kinds
    )


def urgency(deadline: float, decay: float) -> float:
    """The greater of the two pressures. One change; fixes dormant prospects
    and quiet churn risk in the same stroke."""
    return max(deadline, decay)


# ---------------------------------------------------------------------------
# The score
# ---------------------------------------------------------------------------


def effective_ease(ease: float, energy_cost: float) -> float:
    return max(1.0, ease - energy_cost)


def rps(*, ease: float, energy_cost: float, impact: float, urgency_score: float) -> float:
    """Relationship Priority Score. Same skeleton as USI/UBI, range 4.5..45.

    NOTE the 1.5 coefficient on Eff is inherited from a formula tuned for TASKS,
    where clearing easy things has momentum value. It may be wrong here: losing a
    big prospect is not offset by three cheap check-ins. Do not guess at a new
    value — retune from verdict data once there is some.
    """
    eff = effective_ease(ease, energy_cost)
    t = (eff - 1.0) / 9.0
    score = 1.5 * eff + (2.0 - t) * impact + (1.0 + t) * urgency_score
    return round(score, 2)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (person, play) pair under consideration."""

    person_id: str
    play_key: str
    impact: float
    self_sustaining: float
    ease: float
    energy_cost: float
    signal_kinds: list[str]
    last_touch_at: datetime | None
    signal_overrides: dict[str, float] | None = None

    # Set by score(); kept out of __init__ so Candidate stays a plain input.
    def weeks_since_touch(self, now: datetime) -> float:
        if self.last_touch_at is None:
            # Never touched. Treat as long-dormant rather than brand new — an
            # unworked prospect is the definition of decay. Past the plateau
            # knee, so it lands at "cold" rather than at some arbitrary middle.
            return DECAY_MIDPOINT_WEEKS * 2
        return max(0.0, (now - self.last_touch_at).total_seconds() / (7 * 86400))


@dataclass(frozen=True, slots=True)
class Scored:
    candidate: Candidate
    rps: float
    urgency: float
    deadline_pressure: float
    decay_pressure: float
    rank: int = 0
    tier: str = "dormant"

    def breakdown(self) -> dict:
        """Stored on the queue item so the ranking can be argued with.

        When Zarvis surfaces the wrong person, Ryan should be able to point at
        which term is wrong. That is a far better correction signal than "this
        is bad", and it is what makes the model tunable instead of mystical.
        """
        c = self.candidate
        return {
            "impact": c.impact,
            "ease": c.ease,
            "energy_cost": c.energy_cost,
            "effective_ease": effective_ease(c.ease, c.energy_cost),
            "self_sustaining": c.self_sustaining,
            "urgency": round(self.urgency, 2),
            "deadline_pressure": round(self.deadline_pressure, 2),
            "decay_pressure": round(self.decay_pressure, 2),
            "signals": c.signal_kinds,
            "rps": self.rps,
        }


def score(candidate: Candidate, *, now: datetime) -> Scored:
    deadline = deadline_pressure(candidate.signal_kinds, candidate.signal_overrides)
    decay = decay_pressure(
        candidate.weeks_since_touch(now), candidate.impact, candidate.self_sustaining
    )
    u = urgency(deadline, decay)
    return Scored(
        candidate=candidate,
        rps=rps(
            ease=candidate.ease,
            energy_cost=candidate.energy_cost,
            impact=candidate.impact,
            urgency_score=u,
        ),
        urgency=u,
        deadline_pressure=deadline,
        decay_pressure=decay,
    )


# ---------------------------------------------------------------------------
# Ranking and tiers
# ---------------------------------------------------------------------------


def qualifies(scored: Scored) -> bool:
    """Is there actually anything to do here?

    Filters out the valuable-but-quiet. See MIN_URGENCY_TO_QUEUE.
    """
    return scored.urgency >= MIN_URGENCY_TO_QUEUE


def rank_and_tier(
    scored: list[Scored],
    *,
    tier_priority: int = 5,
    tier_standard: int = 10,
    tier_backlog: int = 25,
) -> list[Scored]:
    """Sort by RPS descending, then assign 1-based rank and derived tier.

    Tier is ALWAYS derived from rank and never stored as intent. That is what
    lets high-value work at position 6 climb into Priority on its own as decay
    pressure rises, instead of evaporating at a cap.

    Ties break on impact, then on urgency — mirroring the USI/UBI tiebreak of
    raw total then impact, and biased toward the thing that costs more to lose.
    """
    ordered = sorted(
        scored,
        key=lambda s: (-s.rps, -s.candidate.impact, -s.urgency, s.candidate.person_id),
    )

    priority_end = tier_priority
    standard_end = tier_priority + tier_standard
    backlog_end = standard_end + tier_backlog

    out: list[Scored] = []
    for index, item in enumerate(ordered):
        rank = index + 1
        if rank <= priority_end:
            tier = "priority"
        elif rank <= standard_end:
            tier = "standard"
        elif rank <= backlog_end:
            tier = "backlog"
        else:
            tier = "dormant"
        out.append(
            Scored(
                candidate=item.candidate,
                rps=item.rps,
                urgency=item.urgency,
                deadline_pressure=item.deadline_pressure,
                decay_pressure=item.decay_pressure,
                rank=rank,
                tier=tier,
            )
        )
    return out


def weeks(n: float) -> timedelta:
    """Readability helper for callers and tests."""
    return timedelta(weeks=n)
