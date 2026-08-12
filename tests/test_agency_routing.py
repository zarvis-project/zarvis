"""An agency's client is written ABOUT, to their agency. Never to them.

Ryan's rule, stated 2026-08-12: the agency is the go-between. Mailing their
client directly goes around the person who owns that relationship, and for a
reseller it is their account to manage.

The system had the data and ignored it. `zarvis.manages` held twelve reseller
relationships, and the review board surfaced the routing correctly, so the
drafting model greeted the agency by name entirely of its own accord. But the
ADDRESS was resolved straight off the queued person, and the linter checked the
greeting against that same person, so two correctly-routed
drafts were killed as `wrong-name` while the one that survived, written days
earlier, was addressed to the client directly.

Both halves had to move: the envelope and the prompt. These tests cover the
prompt half, which is pure. The envelope is SQL and is verified against the live
`manages` table instead.
"""

from __future__ import annotations

from zarvis.compose import _build_prompt

BASE = {
    "play_name": "unblock_parked_account",
    "evidence": {"signals": [], "private_count": 0},
    "suggested_action": "Find out whether this account is worth reviving.",
    "title": None,
    "style_notes": None,
}


def _prompt(**over) -> str:
    _, user, _ = _build_prompt({**BASE, **over})
    return user


def test_direct_person_is_addressed_normally():
    user = _prompt(full_name="Dana Whitfield", route_full_name=None, route_email=None)
    assert "**Writing to:** Dana Whitfield" in user
    assert "First name for the greeting:** Dana" in user
    assert "agency account" not in user


def test_managed_person_is_written_to_the_agency():
    user = _prompt(full_name="Wrenfield Osei", route_full_name="Vossberg Media",
                   route_email="hello@vossberg.example.com")
    # The greeting must be the agency's, or the linter is right to block it.
    assert "**Writing to:** Vossberg Media" in user
    assert "First name for the greeting:** Vossberg" in user


def test_managed_person_is_still_the_subject():
    """Routing must not lose WHO the mail is about. That would be a worse bug.

    An email to the agency that never names their client is useless, so the
    client's name has to survive into the prompt as the subject.
    """
    user = _prompt(full_name="Wrenfield Osei", route_full_name="Vossberg Media",
                   route_email="hello@vossberg.example.com")
    assert "Wrenfield Osei" in user
    assert "ABOUT Wrenfield Osei" in user


def test_the_model_is_told_not_to_write_to_the_client():
    user = _prompt(full_name="Wrenfield Osei", route_full_name="Vossberg Media",
                   route_email="hello@vossberg.example.com")
    assert "Do not write to Wrenfield Osei" in user
    assert "agency account" in user.lower()
