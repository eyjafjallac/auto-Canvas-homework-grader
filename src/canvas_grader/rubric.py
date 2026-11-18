"""Read the chosen anonymized submissions and draft a rubric from them."""

from __future__ import annotations

import json
from pathlib import Path

from canvas_grader.llm import Endpoint, LLMError, Part, chat_json

_JSON_SYSTEM = "You reply with one JSON object and no markdown fence."


def read_submission(
    endpoint: Endpoint,
    *,
    label: str,
    student_images: list[Path],
    student_text: str,
    model_answer_text: str,
    answer_images: list[Path],
    guidance: str = "",
) -> dict:
    """Transcribe one submission and note how it differs from the model answer.

    The prompt uses only the letter label. Names and Canvas ids stay out of the request.
    """
    user_text = f"""You are helping a teaching assistant understand one homework submission before writing a rubric.
The student is labeled only as {label}. Do not guess a name.

Model answer:
{_clip(model_answer_text, 12000) or "(no written model answer; use the model-answer images if present)"}

{_baseline_block(guidance, "Baseline from the teaching assistant. Use it only when you describe differences. The transcript must stay a faithful reading of the student's pages and must not be rewritten to match this baseline.")}The images are ordered like this:
1. Model-answer page halves, if any.
2. A line that says the student pages start.
3. Student page halves, upper half then lower half for each original page.

Student typed text, if any:
{_clip(student_text, 8000) or "(none)"}

Transcribe the student's work faithfully in the language they used. If a part cannot be read, say so in the transcript.
Describe how this attempt differs from the model answer: what is missing, wrong, or unusually good.

Return JSON only:
{{
  "transcript": "",
  "differences": "",
  "illegible": false
}}
"""
    parts = [Part(text=user_text)]
    if answer_images:
        parts.append(Part(text="Model-answer pages follow."))
        parts.extend(Part(image=path) for path in answer_images)
    if student_images:
        parts.append(
            Part(
                text=f"Student {label} pages follow. Each original page is an upper half, then a lower half."
            )
        )
        parts.extend(Part(image=path) for path in student_images)
    payload = chat_json(endpoint, system=_JSON_SYSTEM, parts=parts)
    return {
        "transcript": str(payload.get("transcript", "")).strip(),
        "differences": str(payload.get("differences", "")).strip(),
        "illegible": bool(payload.get("illegible", False)),
    }


def transcribe_model_answer(endpoint: Endpoint, images: list[Path]) -> str:
    """Read the model-answer pages once so later students do not resend them."""
    if not images:
        return ""
    user_text = """Transcribe this model answer completely, in the language it is written in.
Keep equations, units, and the order of the work. Each original page is an upper half, then a lower half.
Return JSON only:
{"transcript": ""}
"""
    parts = [Part(text=user_text), Part(text="Model-answer pages follow.")]
    parts.extend(Part(image=path) for path in images)
    payload = chat_json(endpoint, system=_JSON_SYSTEM, parts=parts)
    return str(payload.get("transcript", "")).strip()


def draft_rubric(
    endpoint: Endpoint,
    *,
    points_possible: float,
    model_answer_text: str,
    papers: list[dict],
    guidance: str = "",
) -> list[dict]:
    """Turn the reads into criteria whose points sum to the assignment total."""
    points_text = _points(points_possible)
    user_text = f"""Draft a grading rubric a teaching assistant can apply consistently.
Points possible: {points_text}
The max_points of the criteria must sum exactly to {points_text}. Use at most 8 criteria.

{_baseline_block(guidance, "Baseline from the teaching assistant. Treat this as the standard for full credit, partial credit, and what to ignore. Use the submissions for cases this baseline does not mention.")}Model answer:
{_clip(model_answer_text, 12000) or "(see the differences below)"}

Anonymized submissions:
{_paper_blocks(papers)}

Base the criteria on differences you actually saw, including partial credit.
Write names and descriptions in the language of the model answer. If the model answer has no clear language, use Traditional Chinese.

Return JSON only:
{{
  "criteria": [
    {{"name": "", "max_points": 0, "description": ""}}
  ]
}}
"""
    payload = chat_json(endpoint, system=_JSON_SYSTEM, user_text=user_text, images=None)
    criteria = _normalize_criteria(payload.get("criteria", []), points_possible)
    if not criteria:
        raise LLMError("The model did not return rubric criteria.")
    return criteria


def revise_rubric(
    endpoint: Endpoint,
    *,
    points_possible: float,
    model_answer_text: str,
    papers: list[dict],
    current_criteria: list[dict],
    revision_notes: str,
    guidance: str = "",
) -> list[dict]:
    """Rewrite the current draft. The notes say what the teaching assistant wants changed."""
    notes = revision_notes.strip()
    if not notes:
        raise ValueError("Write what should change before asking for another draft.")
    if not current_criteria:
        raise ValueError("There is no rubric to revise.")
    points_text = _points(points_possible)
    user_text = f"""Revise the grading rubric below. Start from this draft. Do not discard a criterion unless the notes say to remove or replace it, or it conflicts with the baseline.
Points possible: {points_text}
The max_points of the criteria must sum exactly to {points_text}. Use at most 8 criteria.

Current rubric:
{_criteria_lines(current_criteria)}

What the teaching assistant wants changed:
{_clip(notes, 6000)}

{_baseline_block(guidance, "Baseline from the teaching assistant. Treat this as the standard for full credit, partial credit, and what to ignore.")}Model answer:
{_clip(model_answer_text, 12000) or "(see the differences below)"}

Anonymized submissions:
{_paper_blocks(papers)}

Keep the point of each criterion that the notes do not mention.
Write names and descriptions in the language of the current rubric. If the notes ask for another language, follow the notes.

Return JSON only:
{{
  "criteria": [
    {{"name": "", "max_points": 0, "description": ""}}
  ]
}}
"""
    payload = chat_json(endpoint, system=_JSON_SYSTEM, user_text=user_text, images=None)
    criteria = _normalize_criteria(payload.get("criteria", []), points_possible)
    if not criteria:
        raise LLMError("The model did not return rubric criteria.")
    return criteria


def criteria_total(criteria: list[dict]) -> float:
    return round(sum(float(item["max_points"]) for item in criteria), 2)


def criteria_match_total(criteria: list[dict], points_possible: float) -> bool:
    return abs(criteria_total(criteria) - round(float(points_possible), 2)) < 0.02


def rubric_markdown(title: str, points_possible: float, criteria: list[dict]) -> str:
    """The saved rubric as a Markdown file. Sample papers are not included."""
    lines = [
        f"# {title.strip() or 'Rubric'}",
        "",
        f"Points possible: {_points(points_possible)}",
        "",
    ]
    for item in criteria:
        lines.append(f"## {item['name']}")
        lines.append(f"Max: {_points(item['max_points'])}")
        lines.append("")
        description = str(item.get("description", "")).strip()
        if description:
            lines.append(description)
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def rubric_discussion_text(
    criteria: list[dict],
    papers: list[dict],
    points_possible: float,
) -> str:
    """A name-free summary the TA can paste into a chat while settling the rubric."""
    lines = [f"Assignment total {_points(points_possible)}", "", "Rubric draft"]
    for item in criteria:
        lines.append(
            f"- {item['name']} ({_points(item['max_points'])}): {item['description']}"
        )
    lines.append("")
    lines.append("Differences across the sample (no names)")
    for paper in papers:
        lines.append(f"Student {paper['label']}")
        lines.append(paper.get("differences") or "(no differences)")
        lines.append("")
    return "\n".join(lines).strip()


def dump_criteria(criteria: list[dict]) -> str:
    return json.dumps({"criteria": criteria}, ensure_ascii=False)


def _normalize_criteria(raw: object, points_possible: float) -> list[dict]:
    if not isinstance(raw, list):
        return []
    criteria: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if not name:
            continue
        try:
            max_points = round(float(item.get("max_points", 0)), 2)
        except TypeError, ValueError:
            max_points = 0
        max_points = max(max_points, 0)
        criteria.append(
            {"name": name, "max_points": max_points, "description": description}
        )
    if criteria and not criteria_match_total(criteria, points_possible):
        criteria = _scale_to_total(criteria, points_possible)
    return criteria


def _scale_to_total(criteria: list[dict], points_possible: float) -> list[dict]:
    current = sum(float(item["max_points"]) for item in criteria)
    if current <= 0:
        share = round(float(points_possible) / len(criteria), 2)
        scaled = [{**item, "max_points": share} for item in criteria]
    else:
        factor = float(points_possible) / current
        scaled = [
            {**item, "max_points": round(float(item["max_points"]) * factor, 2)}
            for item in criteria
        ]
    drift = round(
        float(points_possible) - sum(float(item["max_points"]) for item in scaled), 2
    )
    scaled[-1]["max_points"] = round(float(scaled[-1]["max_points"]) + drift, 2)
    return scaled


def _paper_blocks(papers: list[dict]) -> str:
    blocks = []
    for paper in papers:
        blocks.append(
            "\n".join(
                [
                    f"Student {paper['label']}",
                    "Transcript:",
                    _clip(paper.get("transcript", ""), 8000),
                    "Differences from the model answer:",
                    _clip(paper.get("differences", ""), 4000),
                ]
            )
        )
    return "\n".join(blocks)


def _criteria_lines(criteria: list[dict]) -> str:
    lines = []
    for item in criteria:
        lines.append(
            f"- {item['name']} ({_points(item['max_points'])}): {item.get('description', '')}"
        )
    return "\n".join(lines)


def _baseline_block(guidance: str, lead: str) -> str:
    text = _clip(guidance, 6000)
    if not text:
        return ""
    return f"{lead}\n{text}\n\n"


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
