"""Rubric rows for one problem are graded together, then the next problem starts."""

import pytest

from canvas_grader.grading import problem_groups
from canvas_grader.llm import Endpoint
from canvas_grader.rubric import (
    draft_rubric,
    read_submission,
    revise_rubric,
    rubric_markdown,
)

_ENDPOINT = Endpoint("Online model", "http://127.0.0.1:9", "key", "model")


def test_problem_groups_stop_between_problems() -> None:
    criteria = [
        {"name": "Problem 1: final answer", "max_points": 0.75},
        {"name": "Problem 1: algebra", "max_points": 0.25},
        {"name": "Problem 2: efficiency", "max_points": 1},
        {"name": "A standalone item", "max_points": 1},
    ]
    groups = problem_groups(criteria)
    assert [label for label, _items in groups] == [
        "Problem 1",
        "Problem 2",
        "A standalone item",
    ]
    assert [item["name"] for item in groups[0][1]] == [
        "Problem 1: final answer",
        "Problem 1: algebra",
    ]


def test_rubric_markdown_lists_each_criterion() -> None:
    text = rubric_markdown(
        "HW5",
        3,
        [
            {
                "name": "Problem 1: answer",
                "max_points": 2,
                "description": "Full credit for the final formula.",
            }
        ],
    )
    assert text.startswith("# HW5\n")
    assert "Points possible: 3" in text
    assert "## Problem 1: answer" in text
    assert "Max: 2" in text
    assert "Full credit for the final formula." in text


def test_draft_rubric_includes_the_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake(endpoint, *, system, user_text="", images=None, parts=None):
        captured["user_text"] = user_text
        return {
            "criteria": [
                {"name": "Units", "max_points": 4, "description": "The number has units."}
            ]
        }

    monkeypatch.setattr("canvas_grader.rubric.chat_json", fake)
    draft_rubric(
        _ENDPOINT,
        points_possible=4,
        model_answer_text="4 N",
        papers=[{"label": "A", "transcript": "4", "differences": "no units"}],
        guidance="Full credit requires units.",
    )
    assert "Baseline from the teaching assistant" in captured["user_text"]
    assert "Full credit requires units." in captured["user_text"]
    assert "Student A" in captured["user_text"]


def test_draft_rubric_omits_an_empty_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake(endpoint, *, system, user_text="", images=None, parts=None):
        captured["user_text"] = user_text
        return {
            "criteria": [
                {"name": "Answer", "max_points": 4, "description": "The final number."}
            ]
        }

    monkeypatch.setattr("canvas_grader.rubric.chat_json", fake)
    draft_rubric(
        _ENDPOINT,
        points_possible=4,
        model_answer_text="4 N",
        papers=[{"label": "A", "transcript": "4", "differences": "no units"}],
    )
    assert "Baseline from the teaching assistant" not in captured["user_text"]


def test_revise_rubric_starts_from_the_current_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake(endpoint, *, system, user_text="", images=None, parts=None):
        captured["user_text"] = user_text
        return {
            "criteria": [
                {"name": "Method", "max_points": 1, "description": "Setup."},
                {"name": "Units", "max_points": 1, "description": "Units on the answer."},
            ]
        }

    monkeypatch.setattr("canvas_grader.rubric.chat_json", fake)
    revised = revise_rubric(
        _ENDPOINT,
        points_possible=10,
        model_answer_text="4 N",
        papers=[{"label": "A", "transcript": "4", "differences": "no units"}],
        current_criteria=[
            {"name": "Answer", "max_points": 10, "description": "Final number."}
        ],
        revision_notes="Split units from the final number.",
        guidance="Units are required.",
    )
    assert "Current rubric:" in captured["user_text"]
    assert "Answer (10): Final number." in captured["user_text"]
    assert "Split units from the final number." in captured["user_text"]
    assert "Units are required." in captured["user_text"]
    assert [item["name"] for item in revised] == ["Method", "Units"]
    assert sum(item["max_points"] for item in revised) == 10


def test_revise_rubric_requires_a_note() -> None:
    with pytest.raises(ValueError, match="what should change"):
        revise_rubric(
            _ENDPOINT,
            points_possible=4,
            model_answer_text="4 N",
            papers=[],
            current_criteria=[
                {"name": "Answer", "max_points": 4, "description": "Final number."}
            ],
            revision_notes="  ",
        )


def test_read_submission_uses_the_baseline_for_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake(endpoint, *, system, user_text="", images=None, parts=None):
        captured["text"] = parts[0].text
        return {"transcript": "4", "differences": "no units", "illegible": False}

    monkeypatch.setattr("canvas_grader.rubric.chat_json", fake)
    read_submission(
        _ENDPOINT,
        label="A",
        student_images=[],
        student_text="4",
        model_answer_text="4 N",
        answer_images=[],
        guidance="Units are required.",
    )
    assert "must not be rewritten to match this baseline" in captured["text"]
    assert "Units are required." in captured["text"]


def test_read_submission_skips_an_empty_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake(endpoint, *, system, user_text="", images=None, parts=None):
        captured["text"] = parts[0].text
        return {"transcript": "4", "differences": "matches", "illegible": False}

    monkeypatch.setattr("canvas_grader.rubric.chat_json", fake)
    read_submission(
        _ENDPOINT,
        label="A",
        student_images=[],
        student_text="4",
        model_answer_text="4 N",
        answer_images=[],
    )
    assert "Baseline from the teaching assistant" not in captured["text"]
