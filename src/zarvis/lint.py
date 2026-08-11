"""Post-generation checks. Deterministic, no model involved.

Two separate jobs that must never be confused with each other:

  `sweep()`  mechanical edits that are always safe (em dashes, signoff period).
             These are applied silently.
  `check()`  assertion linting. Blocks a draft that states a specific fact about
             the recipient which does not appear in the evidence bundle.

The distinction matters because of how corrections get routed later: a tone edit
is a style note, a fabricated fact is a defect. Collapsing them would teach the
decision graph the wrong lesson from the same correction.

WHY NUMBERS ARE NOT ALL TREATED ALIKE
-------------------------------------
The obvious linter — "every number in the draft must appear in the evidence" —
blocks its own best drafts. "Got 15 minutes this week?" is a proposal, not a
claim about anyone, and 15 is nowhere in the evidence because it could not be.

What actually causes damage is a fabricated *specific about them*: a connection
count, a dollar figure, a percentage, a date they did something. Those are the
sentences that end a relationship when they turn out to be invented, and they
are recognisable by shape. So the rule is scoped to claim-shaped numbers and
leaves conversational ones alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EM_DASHES = ("—", "–")

BANNED_WORDS = (
    "keen", "dive in", "delve", "supercharge", "synergy", "shaking up",
    "jazzed", "knack", "innovation", "reckon", "pumped", "spot on",
)

FLUFF_CLOSERS = (
    "looking forward to hearing your thoughts",
    "excited to hear what you think",
    "let's chat soon",
    "looking forward to hearing from you",
    "can't wait to hear back",
)

# Claim-shaped: money, percentages, counts of 100+, and dates. Deliberately not
# "any digit" — see the module docstring.
MONEY = re.compile(r"\$[\d,]+(?:\.\d+)?")
PERCENT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
BIG_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{3,}\b")
DATE_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
DATE_WORDS = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
URL = re.compile(r"https?://[^\s<>\"')]+")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Durations, which the plain number rules deliberately miss.
#
# gpt-5 offered Okafor "a 20-minute walkthrough" and Tomas "set up in 10
# minutes on a quick call". Neither number came from anywhere. Both are small
# integers, so the count rule ignores them by design, and both are exactly the
# kind of quiet commitment that produces an awkward moment on the call when the
# thing takes forty minutes.
#
# The distinction that matters is ASKING versus PROMISING. "Got 15 minutes
# Thursday?" requests THEIR time and commits Ryan to nothing. "I'll give you a
# 20-minute walkthrough" and "I'll have you set up in 10 minutes" are claims
# about what will happen. Only the second kind is a problem, so only the second
# kind is matched.
UNIT = r"(?:minute|min|hour|hr)s?"
PROMISED_DURATION = (
    # "a 20-minute walkthrough", "a 30 min demo"
    re.compile(rf"\b\d+[-\s]?{UNIT}\s+\w*\s*"
               r"(?:walkthrough|demo|call|session|meeting|onboarding|training|chat|setup)\b",
               re.IGNORECASE),
    # "in 10 minutes", "takes about 5 minutes", "done in under an hour"
    re.compile(rf"\b(?:in|takes?|take|within|under)\s+"
               rf"(?:about|around|only|just|less than|under)?\s*\d+[-\s]?{UNIT}\b",
               re.IGNORECASE),
)
# Asking for their time. Never a claim, always allowed.
ASKING_FOR_TIME = re.compile(
    rf"\b(?:got|have|spare|free for|do you have)\s+(?:a\s+)?\d+[-\s]?{UNIT}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    detail: str
    blocking: bool


def sweep(text: str) -> str:
    """Mechanical fixes that are always correct.

    Rule 0 is enforced here rather than trusted to the prompt, exactly as the
    production bot does it: the instruction goes in the prompt AND the string
    replacement runs afterwards, because models regress and a regex costs
    nothing.
    """
    for dash in EM_DASHES:
        # A dash between words becomes a comma. A dash used as an aside opener
        # at a clause boundary reads better as a period, but telling those apart
        # needs parsing, so the safe universal substitution is a comma.
        text = text.replace(f" {dash} ", ", ").replace(dash, ", ")

    # Rule 4: no trailing period on the signoff.
    text = re.sub(
        r"\n(Best|Cheers|Thanks|Talk soon),\s*\n\s*([A-Z][a-z]+)\.\s*$",
        r"\n\1,\n\2",
        text.rstrip() + "",
    )
    # Collapse any run of blank lines the substitutions may have left.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _claims(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for label, pattern in (
        ("money", MONEY),
        ("percentage", PERCENT),
        ("count", BIG_NUMBER),
        ("date", DATE_ISO),
        ("date", DATE_WORDS),
        ("url", URL),
        ("email", EMAIL),
    ):
        for match in pattern.findall(text):
            out.append((label, match if isinstance(match, str) else match[0]))
    return out


def check(body: str, *, evidence_text: str, recipient_first_name: str | None = None) -> list[Finding]:
    """Findings against a draft. Blocking ones must stop it being queued."""
    findings: list[Finding] = []
    haystack = evidence_text.lower()

    for label, value in _claims(body):
        needle = value.lower().strip().rstrip(".,;:!?")
        if needle and needle not in haystack:
            findings.append(
                Finding(
                    rule=f"uncited-{label}",
                    detail=(
                        f"{value!r} is stated as fact but does not appear in the "
                        f"evidence bundle"
                    ),
                    blocking=True,
                )
            )

    for pattern in PROMISED_DURATION:
        for match in pattern.finditer(body):
            phrase = match.group(0)
            # Skip anything that is really a request for their time.
            if ASKING_FOR_TIME.search(phrase):
                continue
            if phrase.lower() in haystack:
                continue
            findings.append(
                Finding(
                    rule="uncited-duration",
                    detail=(
                        f"{phrase!r} promises how long something takes, and that "
                        f"is not in the evidence. Ask for their time instead."
                    ),
                    blocking=True,
                )
            )

    lowered = body.lower()
    for word in BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            findings.append(
                Finding("banned-vocabulary", f"{word!r} is on the banned list", False)
            )

    for closer in FLUFF_CLOSERS:
        if closer in lowered:
            findings.append(
                Finding("fluff-closer", f"{closer!r} adds nothing, cut it", False)
            )

    for dash in EM_DASHES:
        if dash in body:
            findings.append(
                Finding("em-dash", "em or en dash survived the sweep", True)
            )
            break

    # Greeting a real person by the wrong name is worse than any of the above,
    # and it is the failure a template system makes most often.
    if recipient_first_name:
        greeting = re.match(r"\s*(?:hey|hi|hello)\s+([A-Za-z]+)", body, re.IGNORECASE)
        if greeting and greeting.group(1).lower() != recipient_first_name.lower():
            findings.append(
                Finding(
                    "wrong-name",
                    f"greets {greeting.group(1)!r} but the recipient is "
                    f"{recipient_first_name!r}",
                    True,
                )
            )

    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.blocking]
