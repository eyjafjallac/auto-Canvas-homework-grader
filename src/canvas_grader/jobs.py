"""Background reading and grading. Only one batch runs at a time."""

from __future__ import annotations

import threading
from pathlib import Path

from canvas_grader.grading import dump_scores, grade_submission, problem_groups
from canvas_grader.llm import LLMError, endpoint_for
from canvas_grader.render import MAX_SOURCE_PAGES, PreparedPages, pages_exceed_limit, prepare_submission
from canvas_grader.rubric import draft_rubric, dump_criteria, read_submission, transcribe_model_answer
from canvas_grader.store import (
    answer_dir,
    get_batch,
    get_settings,
    get_student,
    list_students,
    pages_dir,
    update_batch,
    update_student,
)

_guard = threading.Lock()
_running: set[int] = set()


def is_running(batch_id: int | None = None) -> bool:
    with _guard:
        if batch_id is None:
            return bool(_running)
        return batch_id in _running


def start_reading(batch_id: int) -> None:
    endpoint_for(get_settings(), get_batch(batch_id).rubric_provider)
    _start(batch_id, lambda: _read(batch_id))


def start_grading(batch_id: int, *, resume: bool) -> None:
    endpoint_for(get_settings(), get_batch(batch_id).grade_provider)
    _start(batch_id, lambda: _grade(batch_id, resume))


def _start(batch_id: int, target) -> None:
    with _guard:
        if _running:
            raise RuntimeError("Another batch is already running. Wait for it to finish so two models are not loaded at once.")
        _running.add(batch_id)

    def wrap() -> None:
        try:
            target()
        except Exception as exc:
            update_batch(batch_id, status="error", error=_short(exc), job_message="Interrupted")
        finally:
            with _guard:
                _running.discard(batch_id)

    threading.Thread(target=wrap, name=f"grader-{batch_id}", daemon=True).start()


def _read(batch_id: int) -> None:
    batch = get_batch(batch_id)
    endpoint = endpoint_for(get_settings(), batch.rubric_provider)
    pilots = [student for student in list_students(batch_id) if student.is_pilot]
    pilots.sort(key=lambda student: student.pilot_label)
    answer_text, answer_images = _answer_for_students(batch_id, endpoint)
    update_batch(
        batch_id,
        status="reading",
        error="",
        job_current=0,
        job_total=len(pilots) + 1,
        job_message="Preparing to read 5 submissions",
    )
    papers: list[dict] = []
    for index, student in enumerate(pilots, start=1):
        update_batch(batch_id, job_current=index, job_message=f"Reading student {student.pilot_label} ({index}/{len(pilots)})")
        current = get_student(student.id)
        if current.status == "read" and current.transcript and not current.error:
            papers.append(_paper(current.pilot_label, current.transcript, current.differences))
            continue
        try:
            prepared = ensure_student_pages(current)
            if not prepared.image_paths and not prepared.text.strip():
                raise LLMError("This submission has no readable pages or text.")
            result = read_submission(
                endpoint,
                label=current.pilot_label,
                student_images=prepared.image_paths,
                student_text=prepared.text,
                model_answer_text=answer_text,
                answer_images=answer_images,
            )
            update_student(
                current.id,
                transcript=result["transcript"],
                differences=result["differences"],
                illegible=result["illegible"],
                status="read",
                error="",
            )
            papers.append(_paper(current.pilot_label, result["transcript"], result["differences"]))
        except Exception as exc:
            update_student(current.id, status="error", error=_short(exc))
    if not papers:
        update_batch(batch_id, status="error", error="None of the 5 submissions could be read, so there is no rubric yet.", job_message="Reading failed")
        return
    update_batch(batch_id, job_current=len(pilots) + 1, job_message="Drafting the rubric")
    try:
        criteria = draft_rubric(
            endpoint,
            points_possible=float(batch.points_possible or 0),
            model_answer_text=answer_text,
            papers=papers,
        )
    except Exception as exc:
        update_batch(batch_id, status="error", error=_short(exc), job_message="Could not draft the rubric")
        return
    update_batch(
        batch_id,
        rubric_json=dump_criteria(criteria),
        status="rubric_ready",
        error="",
        job_message="The rubric draft is ready. Confirm it before grading the class",
    )


def _grade(batch_id: int, resume: bool) -> None:
    batch = get_batch(batch_id)
    criteria = batch.criteria()
    if not criteria:
        update_batch(batch_id, status="error", error="There is no rubric yet.", job_message="")
        return
    endpoint = endpoint_for(get_settings(), batch.grade_provider)
    students = list_students(batch_id)
    if not resume:
        for student in students:
            update_student(
                student.id,
                scores_json="",
                comment="",
                total_score=None,
                reviewed=False,
                status="pending",
                error="",
                transcript="",
                illegible=False,
            )
    students = list_students(batch_id)
    update_batch(
        batch_id,
        status="grading",
        error="",
        job_current=0,
        job_total=len(students),
        job_message="Preparing to grade",
    )
    answer_text, answer_images = _answer_for_students(batch_id, endpoint)
    failures = 0
    for index, student in enumerate(students, start=1):
        current = get_student(student.id)
        if resume and current.status in {"graded", "reviewed"}:
            update_batch(batch_id, job_current=index)
            continue
        update_batch(batch_id, job_current=index, job_message=f"Grading {current.display_name} ({index}/{len(students)})")
        try:
            prepared = ensure_student_pages(current)
            if not prepared.image_paths and not prepared.text.strip():
                raise LLMError("This submission has no readable pages or text.")
            scores: list[dict] = []
            transcripts: list[str] = []
            comments: list[str] = []
            illegible = False
            for label, items in problem_groups(criteria):
                update_batch(
                    batch_id,
                    job_current=index,
                    job_message=f"Grading {current.display_name}: {label} ({index}/{len(students)})",
                )
                result = grade_submission(
                    endpoint,
                    criteria=items,
                    student_images=prepared.image_paths,
                    student_text=prepared.text,
                    model_answer_text=answer_text,
                    answer_images=answer_images,
                )
                scores.extend(result["scores"])
                if result["transcript"]:
                    transcripts.append(f"{label}\n{result['transcript']}")
                if result["comment"]:
                    comments.append(result["comment"])
                illegible = illegible or result["illegible"]
            update_student(
                current.id,
                transcript="\n\n".join(transcripts),
                scores_json=dump_scores(scores),
                comment="\n\n".join(comments),
                total_score=round(sum(float(item["points"]) for item in scores), 2),
                illegible=illegible,
                status="graded",
                reviewed=False,
                error="",
            )
        except Exception as exc:
            failures += 1
            update_student(current.id, status="error", error=_short(exc))
    message = "The class is graded. Review before you export."
    if failures:
        message = f"Grading finished with {failures} failures. Check the errors, then continue."
    update_batch(batch_id, status="review", error="", job_message=message)


def ensure_student_pages(student):
    dest = pages_dir(student.batch_id, student.id)
    marker = dest / ".ready"
    cached_is_complete = not student.truncated or student.page_count >= MAX_SOURCE_PAGES
    if marker.is_file() and cached_is_complete and not pages_exceed_limit(dest):
        images = sorted(dest.glob("*.jpg"))
        return PreparedPages(
            image_paths=images,
            text=student.text_body,
            truncated=student.truncated,
            source_page_count=student.page_count,
        )
    if marker.is_file():
        marker.unlink()
    prepared = prepare_submission([Path(path) for path in student.source_files], dest)
    marker.write_text("ok", encoding="utf-8")
    update_student(
        student.id,
        text_body=prepared.text,
        truncated=prepared.truncated,
        page_count=prepared.source_page_count,
    )
    prepared.text = prepared.text
    return prepared


def _answer_for_students(batch_id: int, endpoint) -> tuple[str, list[Path]]:
    """Use answer text for every student. Read the answer images once if needed.

    Sending the answer pages again with each submission overflows a 32k context.
    """
    batch = get_batch(batch_id)
    text = batch.model_answer_text.strip()
    images = _answer_images(batch_id)
    if text or not images:
        return text, []
    update_batch(batch_id, job_message="Reading the model answer")
    text = transcribe_model_answer(endpoint, images)
    if not text:
        raise LLMError("The model returned an empty transcript for the model answer.")
    update_batch(batch_id, model_answer_text=text)
    return text, []


def _answer_images(batch_id: int) -> list[Path]:
    directory = answer_dir(batch_id)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jpg"))


def _paper(label: str, transcript: str, differences: str) -> dict:
    return {"label": label, "transcript": transcript, "differences": differences}


def _short(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]
