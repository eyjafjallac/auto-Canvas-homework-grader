"""Score one submission against a locked rubric."""

from __future__ import annotations

import json
from pathlib import Path

from canvas_grader.llm import Endpoint, Part, chat_json

_JSON_SYSTEM = "You reply with one JSON object and no markdown fence."


def problem_groups(criteria: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group rubric rows that belong to the same problem.

    ``Problem 1: final answer`` and ``Problem 1: algebra`` stay together.
    The grader scores one group, then stops and starts a new request for the next.
    """
    groups: list[tuple[str, list[dict]]] = []
    positions: dict[str, int] = {}
    for item in criteria:
        label = _problem_label(str(item.get("name", "")))
        position = positions.get(label)
        if position is None:
            positions[label] = len(groups)
            groups.append((label, [item]))
        else:
            groups[position][1].append(item)
    return groups


def _problem_label(name: str) -> str:
    head, separator, _rest = name.partition(":")
    label = head.strip() if separator else name.strip()
    return label or "Problem"


def grade_submission(
    endpoint: Endpoint,
    *,
    criteria: list[dict],
    student_images: list[Path],
    student_text: str,
    model_answer_text: str,
    answer_images: list[Path],
) -> dict:
    """Return transcript, per-criterion scores, a student-facing comment, and an illegible flag.

    The total is the sum of the criterion scores. The model is not asked for a separate total.
    """
    rubric_lines = []
    for item in criteria:
        rubric_lines.append(
            f"- {item['name']} (max { _points(item['max_points']) }): {item.get('description', '')}"
        )
    user_text = f"""Grade only this one problem, then stop. Do not score any other problem.
Score every criterion below with a number from 0 through its max. Do not invent an overall total.
Use the criterion names exactly as written.
The transcript must cover only this problem, including work that continues on a later page.

Rubric:
{chr(10).join(rubric_lines)}

Model answer:
{_clip(model_answer_text, 12000) or "(no written model answer; use the model-answer images if present)"}

Images, in order: model-answer page halves if any, then the student page halves (upper half before lower half).
Student typed text, if any:
{_clip(student_text, 8000) or "(none)"}

Write the comment as feedback to the student, in the language of the submission.
If handwriting cannot be read, set illegible to true and say so in the transcript.

Return JSON only:
{{
  "transcript": "",
  "criteria": [{{"name": "", "points": 0, "reason": ""}}],
  "comment": "",
  "illegible": false
}}
"""
    parts = [Part(text=user_text)]
    if answer_images:
        parts.append(Part(text="Model-answer pages follow."))
        parts.extend(Part(image=path) for path in answer_images)
    if student_images:
        parts.append(Part(text="Student pages follow. Each original page is an upper half, then a lower half."))
        parts.extend(Part(image=path) for path in student_images)
    payload = chat_json(endpoint, system=_JSON_SYSTEM, parts=parts)
    scores = align_scores(criteria, payload.get("criteria", []))
    return {
        "transcript": str(payload.get("transcript", "")).strip(),
        "scores": scores,
        "comment": str(payload.get("comment", "")).strip(),
        "illegible": bool(payload.get("illegible", False)),
        "total": round(sum(float(item["points"]) for item in scores), 2),
    }


def align_scores(criteria: list[dict], returned: object) -> list[dict]:
    """Match model scores to rubric rows, then clamp each score to its maximum."""
    rows = [item for item in returned if isinstance(item, dict)] if isinstance(returned, list) else []
    used: set[int] = set()
    aligned: list[dict] = []
    for criterion in criteria:
        key = str(criterion["name"]).strip().casefold()
        source: dict | None = None
        for row_index, row in enumerate(rows):
            if row_index in used:
                continue
            if str(row.get("name", "")).strip().casefold() == key:
                source = row
                used.add(row_index)
                break
        if source is None:
            for row_index, row in enumerate(rows):
                if row_index in used:
                    continue
                source = row
                used.add(row_index)
                break
        points = 0.0
        reason = ""
        if source is None:
            reason = "The model did not score this criterion."
        else:
            try:
                points = float(source.get("points", 0))
            except (TypeError, ValueError):
                points = 0.0
            reason = str(source.get("reason", "")).strip()
        maximum = float(criterion["max_points"])
        points = min(maximum, max(0.0, points))
        aligned.append(
            {
                "name": criterion["name"],
                "max_points": maximum,
                "points": round(points, 2),
                "reason": reason,
            }
        )
    return aligned


def dump_scores(scores: list[dict]) -> str:
    return json.dumps(scores, ensure_ascii=False)


def feedback_markdown(student_name: str, points_possible: float, total: float | None, scores: list[dict], comment: str, transcript: str) -> str:
    lines = [f"# {student_name}", ""]
    if total is None:
        lines.append("Total: not graded")
    else:
        lines.append(f"Total: {_points(total)} / {_points(points_possible)}")
    lines.append("")
    for item in scores:
        lines.append(f"## {item['name']}")
        lines.append(f"Score: {_points(item['points'])} / {_points(item['max_points'])}")
        if item.get("reason"):
            lines.append(item["reason"])
        lines.append("")
    lines.append("## Comment for the student")
    lines.append(comment or "(no comment)")
    lines.append("")
    lines.append("## Transcript")
    lines.append(transcript or "(no transcript)")
    lines.append("")
    return "\n".join(lines)


def _clip(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def _points(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")
