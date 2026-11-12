"""Temporary students for checking review status, overflow, and PDF views.

Run from the project directory:

    uv run python -m canvas_grader.preview

Re-running replaces the previous temporary batch. Delete it from the review
page when you are finished.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from canvas_grader.config import load_dotenv
from canvas_grader.render import MAX_SOURCE_PAGES, prepare_submission
from canvas_grader.store import (
    add_student,
    batch_dir,
    create_batch,
    delete_batch,
    init_db,
    list_batches,
    pages_dir,
    update_batch,
    update_student,
)

PREVIEW_ASSIGNMENT = "TEMP status check"

_LONG_TOKEN = "OverflowCheckToken" * 8

_CRITERIA = [
    {
        "name": "Problem 1: setup",
        "max_points": 2,
        "description": "Writes the given information and the unknown.",
    },
    {
        "name": "Problem 1: work",
        "max_points": 3,
        "description": "Shows the algebra. A missing sign loses a point.",
    },
    {
        "name": "Problem 2: diagram",
        "max_points": 2,
        "description": "Labels the forces and the direction of motion.",
    },
    {
        "name": "Problem 2: result",
        "max_points": 2,
        "description": "Numeric answer with units.",
    },
    {
        "name": "Clarity",
        "max_points": 1,
        "description": (
            "Work can be followed without guessing. "
            "Long token for the edge of the card: " + _LONG_TOKEN
        ),
    },
]


def seed_status_preview() -> int:
    """Replace the temporary batch and return its id."""
    for batch in list_batches():
        if batch.assignment_column == PREVIEW_ASSIGNMENT:
            delete_batch(batch.id)
    batch = create_batch("Temporary preview. These scores are not a real class.")
    update_batch(
        batch.id,
        assignment_column=PREVIEW_ASSIGNMENT,
        points_possible=10,
        status="review",
        rubric_json=json.dumps({"criteria": _CRITERIA}),
        job_message=(
            "Temporary students for checking status, text that runs to the edge, "
            "and the original PDF versus the split pages. Delete this batch when done."
        ),
    )
    pdf_path = batch_dir(batch.id) / "extracted" / "preview.pdf"
    _write_preview_pdf(pdf_path)
    source = [str(pdf_path.resolve())]
    samples = [
        _Sample(
            name="Ada Lovelace",
            filename="ada",
            user_id="1001",
            matched=True,
            status="graded",
            points=[2, 2, 2, 1, 1],
            reason="Missed a unit on problem 2.",
            comment="Check the units on the last line.",
        ),
        _Sample(
            name="Grace Hopper",
            filename="grace",
            user_id="1002",
            matched=True,
            status="reviewed",
            reviewed=True,
            points=[2, 3, 2, 2, 1],
            reason="",
            comment="Complete.",
        ),
        _Sample(
            name="Alan Turing",
            filename="alan",
            user_id="1003",
            matched=True,
            status="error",
            error=(
                "The model returned a score that could not be read. Token: "
                + _LONG_TOKEN
            ),
        ),
        _Sample(
            name="Katherine Johnson",
            filename="katherine",
            user_id="1004",
            matched=True,
            status="pending",
        ),
        _Sample(
            name="Maryam Mirzakhani",
            filename="maryam",
            user_id="1005",
            matched=True,
            status="graded",
            illegible=True,
            truncated=True,
            points=[1, 1, 1, 1, 0],
            reason="Several lines are hard to read, and later pages were not sent.",
        ),
        _Sample(
            name="ChristopherAlexanderTheodoreLongnameWithoutBreaks",
            filename="christopher",
            user_id=None,
            matched=False,
            status="graded",
            points=[2, 3, 1, 2, 1],
            reason="Not in the gradebook, so this row stays out of the CSV.",
            text_body=("Typed answer: the total is 4.\n" + _LONG_TOKEN),
            transcript="The student wrote 4. " + _LONG_TOKEN,
        ),
    ]
    for sample in samples:
        student_id = add_student(
            batch.id,
            canvas_user_id=sample.user_id,
            display_name=sample.name,
            filename_name=sample.filename,
            parsed=sample.user_id is not None,
            matched=sample.matched,
            gradebook_row=1 if sample.matched else None,
            source_files=source,
        )
        prepared = prepare_submission([pdf_path], pages_dir(batch.id, student_id))
        (pages_dir(batch.id, student_id) / ".ready").write_text("ok", encoding="utf-8")
        page_count = (
            MAX_SOURCE_PAGES if sample.truncated else prepared.source_page_count
        )
        scores = _scores(sample.points, sample.reason)
        total = round(sum(sample.points), 2) if sample.points is not None else None
        update_student(
            student_id,
            status=sample.status,
            reviewed=sample.reviewed,
            illegible=sample.illegible,
            truncated=sample.truncated,
            error=sample.error,
            comment=sample.comment,
            transcript=sample.transcript,
            text_body=sample.text_body,
            scores_json=json.dumps(scores) if scores is not None else "",
            total_score=total,
            page_count=page_count,
        )
    return batch.id


class _Sample:
    def __init__(
        self,
        *,
        name: str,
        filename: str,
        user_id: str | None,
        matched: bool,
        status: str,
        reviewed: bool = False,
        illegible: bool = False,
        truncated: bool = False,
        points: list[float] | None = None,
        reason: str = "",
        comment: str = "",
        transcript: str = "",
        text_body: str = "",
        error: str = "",
    ) -> None:
        self.name = name
        self.filename = filename
        self.user_id = user_id
        self.matched = matched
        self.status = status
        self.reviewed = reviewed
        self.illegible = illegible
        self.truncated = truncated
        self.points = points
        self.reason = reason
        self.comment = comment
        self.transcript = transcript
        self.text_body = text_body
        self.error = error


def _scores(
    points: list[float] | None, reason: str
) -> list[dict[str, str | float]] | None:
    if points is None:
        return None
    rows: list[dict[str, str | float]] = []
    for criterion, value in zip(_CRITERIA, points, strict=True):
        rows.append(
            {
                "name": str(criterion["name"]),
                "max_points": float(criterion["max_points"]),
                "points": value,
                "reason": reason,
            }
        )
    return rows


def _write_preview_pdf(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    try:
        for number in range(1, 4):
            page = document.new_page(width=595, height=842)
            page.insert_text(
                (72, 80),
                f"TEMP preview — original page {number}",
                fontsize=18,
            )
            page.insert_text(
                (72, 140),
                "This is the original PDF. The other tab cuts each page",
                fontsize=13,
            )
            page.insert_text(
                (72, 164),
                "into an upper half and a lower half.",
                fontsize=13,
            )
            page.insert_text(
                (72, 430),
                f"Middle of page {number}. The split overlaps this line.",
                fontsize=14,
            )
            page.insert_text(
                (72, 780),
                f"Bottom of original page {number}.",
                fontsize=14,
            )
        document.save(target)
    finally:
        document.close()


def main() -> None:
    load_dotenv()
    init_db()
    batch_id = seed_status_preview()
    print(f"Temporary batch {batch_id}")
    print(f"Open http://127.0.0.1:8765/batches/{batch_id}/review")


if __name__ == "__main__":
    main()
