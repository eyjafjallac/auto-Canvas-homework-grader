"""Rubric rows for one problem are graded together, then the next problem starts."""

from canvas_grader.grading import problem_groups
from canvas_grader.rubric import rubric_markdown


def test_problem_groups_stop_between_problems() -> None:
    criteria = [
        {"name": "Problem 1: final answer", "max_points": 0.75},
        {"name": "Problem 1: algebra", "max_points": 0.25},
        {"name": "Problem 2: efficiency", "max_points": 1},
        {"name": "A standalone item", "max_points": 1},
    ]
    groups = problem_groups(criteria)
    assert [label for label, _items in groups] == ["Problem 1", "Problem 2", "A standalone item"]
    assert [item["name"] for item in groups[0][1]] == [
        "Problem 1: final answer",
        "Problem 1: algebra",
    ]


def test_rubric_markdown_lists_each_criterion() -> None:
    text = rubric_markdown(
        "HW5",
        3,
        [{"name": "Problem 1: answer", "max_points": 2, "description": "Full credit for the final formula."}],
    )
    assert text.startswith("# HW5\n")
    assert "Points possible: 3" in text
    assert "## Problem 1: answer" in text
    assert "Max: 2" in text
    assert "Full credit for the final formula." in text
