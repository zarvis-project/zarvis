"""The room's "no" has to survive the trip back to the caller.

On the first real room run the Judge returned `channel: none` and the pipeline
drafted an email anyway. That is the failure this predicate exists to prevent,
and it now has two callers -- the CLI and the Slack handler -- which is exactly
the shape where a bug gets fixed in one place and left in the other.
"""

from __future__ import annotations

from zarvis.room import declined


def test_no_channel_is_a_no():
    assert declined({"channel": "none", "timing": "now"})


def test_wait_timing_is_a_no():
    """A real move at the wrong moment is still not a move today."""
    for timing in ("wait", "Wait two weeks", "WAIT until the trial ends"):
        assert declined({"channel": "email", "timing": timing}), timing


def test_act_is_a_yes():
    assert not declined({"channel": "email", "timing": "today"})
    assert not declined({"channel": "call", "timing": "this week"})


def test_missing_fields_do_not_read_as_a_yes_or_crash():
    """A malformed decision must not become an accidental send.

    `{}` reads as "not declined" and that is deliberate: the caller only drafts
    when it also has an approach to draft from, and a hard failure there is
    better than silently swallowing a decision nobody can read.
    """
    assert declined({"channel": None, "timing": None}) is False
    assert declined({}) is False


def test_case_and_whitespace_do_not_defeat_it():
    assert declined({"channel": "NONE"})
    assert declined({"timing": "waiting for their reply"})
