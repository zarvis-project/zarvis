"""Linter tests.

The interesting cases here are the NEGATIVE ones. A linter that blocks every
draft is as useless as one that blocks none, and the specific way this one could
go wrong is over-blocking on numbers: "got 15 minutes?" is not a claim about
anybody, and a naive rule would reject Ryan's most natural sentence.
"""

from __future__ import annotations

from zarvis.lint import Finding, blocking, check, sweep

EVIDENCE = """
{
  "signals": [
    {"kind": "onboarding_incomplete", "value": {"has_sales_nav": false},
     "observed_at": "2026-08-08T00:00:00+00:00"},
    {"kind": "reply_received", "authored_by": "counterparty",
     "body": "We ended up with 2,400 connections last quarter. Budget is $500/mo."}
  ]
}
"""


# --- sweep -----------------------------------------------------------------


def test_sweep_removes_em_dashes():
    assert "—" not in sweep("I saw your note — looks great.")
    assert "–" not in sweep("Nice work – really.")


def test_sweep_strips_signoff_period():
    out = sweep("Sounds good.\n\nBest,\nRyan.")
    assert out.endswith("Best,\nRyan")


def test_sweep_leaves_clean_copy_alone():
    clean = "Hey JJ,\n\nGot 15 minutes Thursday?\n\nBest,\nRyan"
    assert sweep(clean) == clean


# --- assertion checks ------------------------------------------------------


def test_blocks_invented_count():
    findings = check(
        "You're at 9,100 connections now, nice work.", evidence_text=EVIDENCE
    )
    assert any(f.rule == "uncited-count" for f in blocking(findings))


def test_allows_cited_count():
    findings = check(
        "You mentioned 2,400 connections last quarter.", evidence_text=EVIDENCE
    )
    assert not blocking(findings)


def test_blocks_invented_money():
    findings = check("I know $2,000/mo is a lot.", evidence_text=EVIDENCE)
    assert any(f.rule == "uncited-money" for f in blocking(findings))


def test_allows_cited_money():
    findings = check("You said $500/mo was the budget.", evidence_text=EVIDENCE)
    assert not blocking(findings)


def test_does_not_block_conversational_numbers():
    """The over-blocking failure. These are proposals, not claims."""
    for body in (
        "Got 15 minutes this week?",
        "I'm free between 2:30 and 5:30.",
        "Want to try it for a couple of days, say 3?",
    ):
        assert not blocking(check(body, evidence_text=EVIDENCE)), body


def test_blocks_invented_url():
    findings = check(
        "Grab a slot: https://calendly.com/made-up-link", evidence_text=EVIDENCE
    )
    assert any(f.rule == "uncited-url" for f in blocking(findings))


def test_blocks_wrong_greeting_name():
    """Greeting one person by another person's name. The unrecoverable mistake.

    The two names here MUST differ. An earlier edit accidentally made them the
    same, and the test kept passing on the assertion it no longer made.
    """
    findings = check(
        "Hey Priya,\n\nQuick one.", evidence_text=EVIDENCE, recipient_first_name="Marek"
    )
    assert any(f.rule == "wrong-name" for f in blocking(findings))


def test_allows_right_greeting_name():
    findings = check(
        "Hey Marek,\n\nQuick one.", evidence_text=EVIDENCE, recipient_first_name="Marek"
    )
    assert not blocking(findings)


# --- non-blocking style notes ----------------------------------------------


def test_banned_vocabulary_warns_but_does_not_block():
    findings = check("Keen to dive in on this.", evidence_text=EVIDENCE)
    rules = {f.rule for f in findings}
    assert "banned-vocabulary" in rules
    assert not blocking(findings)


def test_fluff_closer_warns():
    findings = check(
        "Sounds good. Looking forward to hearing your thoughts!", evidence_text=EVIDENCE
    )
    assert any(f.rule == "fluff-closer" for f in findings)


def test_em_dash_survives_sweep_is_blocking():
    findings = check("Hey — quick one.", evidence_text=EVIDENCE)
    assert any(f.rule == "em-dash" for f in blocking(findings))


# --- promised durations ----------------------------------------------------
# Added after gpt-5 offered a "20-minute walkthrough" and "set up in 10 minutes"
# to two real prospects. Both slipped through: small integers are allowed on
# purpose so "got 15 minutes?" is not blocked.


def test_blocks_promised_walkthrough_length():
    findings = check(
        "I can give you a 20-minute walkthrough so you can see it live.",
        evidence_text=EVIDENCE,
    )
    assert any(f.rule == "uncited-duration" for f in blocking(findings))


def test_blocks_promised_setup_time():
    findings = check(
        "Want me to get you set up in 10 minutes on a quick call?",
        evidence_text=EVIDENCE,
    )
    assert any(f.rule == "uncited-duration" for f in blocking(findings))


def test_allows_asking_for_their_time():
    """The whole reason the numeric rules are loose. Do not regress this."""
    for body in (
        "Got 15 minutes this week?",
        "Do you have 20 minutes Thursday?",
        "Any chance you have 30 mins Friday?",
    ):
        assert not blocking(check(body, evidence_text=EVIDENCE)), body


def test_allows_vague_duration():
    """'A few minutes' is honest precisely because it is not a number."""
    for body in (
        "It only takes a few minutes.",
        "Should be quick, happy to do it with you.",
    ):
        assert not blocking(check(body, evidence_text=EVIDENCE)), body


def test_allows_duration_that_is_in_the_evidence():
    ev = EVIDENCE + '\n"They asked for a 30 minute call."'
    assert not blocking(check("Happy to do the 30 minute call.", evidence_text=ev))
