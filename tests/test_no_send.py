"""The never-send guarantee, as an executable assertion.

Zarvis queues drafts. A human presses send. That was the first hard constraint
Ryan set, and the reason is the failure mode he named: an agent that confidently
acts at scale on a wrong inference, and by the time anyone notices, two thousand
cans of cherries have been ordered.

The original plan claimed this was structural, enforced by holding
`gmail.readonly` instead of a write scope. That was wrong and is worth recording
plainly: `gmail.compose` grants `messages.send` as well as draft creation, and so
does `gmail.modify`. **There is no Gmail scope that permits drafting but forbids
sending.** Any process that can create a draft in the mailbox can also send it.

So the guarantee is enforced here instead: no module in this codebase may call
the Gmail send endpoints. It is a weaker guarantee than a scope would have been,
and it is the strongest one available, which is exactly why it needs a test
rather than a comment.

If a future feature genuinely needs to send, this test should be deleted in the
same commit, deliberately and visibly, rather than quietly weakened.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# Gmail's send surface. `.send(` alone is too broad — it collides with ordinary
# words — so these target the API shapes specifically.
FORBIDDEN_SEND = (
    re.compile(r"messages\(\)\s*\.\s*send\b"),
    re.compile(r"drafts\(\)\s*\.\s*send\b"),
    re.compile(r"['\"]https://www\.googleapis\.com/auth/gmail\.send['\"]"),
    re.compile(r"\bsmtplib\b"),
    re.compile(r"\bsendmail\b"),
)

# Ryan's actual mail, as opposed to Zarvis's own drafts.
#
# Deleting a DRAFT is fine and is used: `deliver._retire_superseded` removes a
# superseded draft so Ryan never finds two competing emails to the same person.
# Trashing or deleting a MESSAGE is his received and sent mail, and nothing here
# has any business touching it.
#
# This matters more once `gmail.modify` is granted for draft labelling, because
# that single scope hands over drafts, labels, archiving and trashing together.
# Google offers nothing narrower, so the scope cannot express the restriction.
# This test is what expresses it.
FORBIDDEN_MAIL = (
    re.compile(r"messages\(\)\s*\.\s*trash\b"),
    re.compile(r"messages\(\)\s*\.\s*untrash\b"),
    re.compile(r"messages\(\)\s*\.\s*delete\b"),
    re.compile(r"messages\(\)\s*\.\s*batchModify\b"),
    re.compile(r"messages\(\)\s*\.\s*batchDelete\b"),
    re.compile(r"threads\(\)\s*\.\s*(trash|delete|modify)\b"),
)


def _python_files() -> list[pathlib.Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _scan(patterns) -> list[str]:
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(SRC)}:{line} -> {match.group(0)}")
    return offenders


def test_nothing_touches_ryans_real_mail():
    """Drafts are Zarvis's. Messages are Ryan's.

    `gmail.modify` erases that line at the scope level, so it is drawn here
    instead. Zarvis may create, update and delete its own drafts, and may label
    them. It may never trash, delete or bulk-modify a message.
    """
    offenders = _scan(FORBIDDEN_MAIL)
    assert not offenders, (
        "Zarvis manages its own drafts and must never touch Ryan's real mail. "
        "Found:\n  " + "\n  ".join(offenders)
    )


def test_no_module_can_send_mail():
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_SEND:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(SRC)}:{line} -> {match.group(0)}")

    assert not offenders, (
        "Zarvis must never be able to send email. Found:\n  "
        + "\n  ".join(offenders)
        + "\n\nIf sending is now intended, delete this test in the same commit "
        "so the change is visible in review."
    )


def test_agent_scopes_are_read_only():
    """The deciding process holds no write scope, even though that alone is not
    sufficient. Defence in depth: the writer is a separate process."""
    from zarvis.config import Config

    scopes = Config.google_scopes.fget(object.__new__(Config))  # type: ignore[attr-defined]
    for scope in scopes:
        assert scope.endswith(".readonly"), f"non-readonly scope on the agent: {scope}"
