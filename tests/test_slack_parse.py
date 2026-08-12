"""One Slack message about two people must file two separate facts.

WHY
---
Ryan corrected a report that had covered two people, in a single message:

    "I sent the draft to marek. I didn't bin it. I also sent the draft to Sunniva"

`parse()` returned one object with one `person`, so the whole text, including
the sentence about Sunniva, was written into MAREK's evidence bundle. Sunniva is
irrelevant to Marek, and the drafting model cannot tell: it reads the bundle as
facts about the person it is writing to and uses them with confidence.

These tests cover the RESHAPING contract rather than the model's judgment. What
the model chooses to split is prompt work and cannot be asserted offline; that
`parse` always hands back a list, and that a stray single-object response is
carried rather than dropped, is code and is testable.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from zarvis.llm import Completion
from zarvis.slack import parse


def _completion(payload) -> Completion:
    return Completion(
        text=json.dumps(payload), model_id="test", input_tokens=0, output_tokens=0,
    )


def _parse(payload) -> list[dict]:
    with patch("zarvis.slack.complete", return_value=_completion(payload)):
        return parse("whatever")


def test_two_people_stay_two_items():
    items = _parse({"items": [
        {"intent": "note", "person": "marek", "content": "Ryan sent the draft."},
        {"intent": "note", "person": "Sunniva", "content": "Ryan sent the draft."},
    ]})
    assert len(items) == 2
    assert [i["person"] for i in items] == ["marek", "Sunniva"]
    # The whole point: neither item carries the other's name in its content.
    assert "Sunniva" not in items[0]["content"]


def test_single_person_still_returns_a_list():
    items = _parse({"items": [
        {"intent": "note", "person": "Dana", "content": "Moving offices."},
    ]})
    assert isinstance(items, list) and len(items) == 1
    assert items[0]["person"] == "Dana"


def test_legacy_single_object_is_carried_not_dropped():
    """The model sometimes reverts to the old shape. Reshape, never discard.

    Dropping it would lose a fact Ryan believes he recorded, which is the one
    outcome worse than filing it imperfectly.
    """
    items = _parse({"intent": "note", "person": "Dana", "content": "Moving offices."})
    assert len(items) == 1
    assert items[0]["person"] == "Dana"


def test_bare_list_is_accepted():
    items = _parse([{"intent": "note", "person": "Dana", "content": "x"}])
    assert len(items) == 1


def test_empty_items_falls_back_rather_than_returning_nothing():
    """An empty list would silently swallow the message."""
    items = _parse({"items": [], "intent": "unclear", "person": None, "content": "x"})
    assert len(items) == 1
