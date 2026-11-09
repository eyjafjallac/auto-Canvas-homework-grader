"""Local web app for drafting a rubric and grading handwritten submissions."""

from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from canvas_grader.canvas_zip import extract_submission_zip
from canvas_grader.config import (
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_ONLINE_BASE_URL,
    DEFAULT_ONLINE_MODEL,
    STATIC_DIR,
    TEMPLATES_DIR,
    load_dotenv,
)
from canvas_grader.gradebook_csv import (
    apply_scores,
    format_points,
    gradebook_csv_text,
    load_gradebook,
)
from canvas_grader.grading import dump_scores, feedback_markdown
from canvas_grader.jobs import (
    ensure_student_pages,
    is_running,
    start_grading,
    start_reading,
)
from canvas_grader.llm import LLMError, endpoint_for, test_endpoint
from canvas_grader.render import (
    IMAGE_EXTENSIONS,
    MAX_SOURCE_PAGES,
    PDF_EXTENSIONS,
    TEXT_EXTENSIONS,
    prepare_answer_files,
)
from canvas_grader.rubric import (
    criteria_match_total,
    criteria_total,
    dump_criteria,
    rubric_discussion_text,
    rubric_markdown,
)
from canvas_grader.store import (
    Batch,
    Settings,
    Student,
    add_student,
    answer_dir,
    batch_dir,
    create_batch,
    delete_batch,
    extracted_dir,
    get_batch,
    get_settings,
    get_student,
    init_db,
    list_batches,
    list_students,
    pages_dir,
    save_settings,
    update_batch,
    update_student,
)

_STATUS_LABELS = {
    "setup": "Choose students",
    "reading": "Reading",
    "rubric_ready": "Confirm rubric",
    "grading": "Grading",
    "review": "Needs review",
    "error": "Error",
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    load_dotenv()
    init_db()
    yield


app = FastAPI(title="Homework grader", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["points"] = lambda value: (
    "—" if value is None else format_points(float(value))
)
template_globals = cast(dict[str, Any], templates.env.globals)
template_globals["status_label"] = lambda status: _STATUS_LABELS.get(status, status)
template_globals["page_limit"] = MAX_SOURCE_PAGES


def main() -> None:
    load_dotenv()
    init_db()
    print("Open http://127.0.0.1:8765")
    uvicorn.run("canvas_grader.main:app", host="127.0.0.1", port=8765, reload=False)


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return _render(request, "home.html", batches=list_batches(), running=is_running())


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    return _render(
        request,
        "settings.html",
        settings=settings,
        has_online_key=bool(settings.online_api_key.strip()),
    )


@app.post("/settings")
def save_settings_route(
    request: Request,
    online_base_url: str = Form(""),
    online_model: str = Form(""),
    online_api_key: str = Form(""),
    clear_online_key: str = Form(""),
    local_base_url: str = Form(""),
    local_model: str = Form(""),
    local_api_key: str = Form(""),
) -> RedirectResponse:
    _store_settings(
        online_base_url,
        online_model,
        online_api_key,
        clear_online_key,
        local_base_url,
        local_model,
        local_api_key,
    )
    return _redirect("/settings", notice="Settings saved.")


@app.post("/settings/test")
def test_local_route(
    online_base_url: str = Form(""),
    online_model: str = Form(""),
    online_api_key: str = Form(""),
    clear_online_key: str = Form(""),
    local_base_url: str = Form(""),
    local_model: str = Form(""),
    local_api_key: str = Form(""),
) -> RedirectResponse:
    return _test_saved_connection(
        "local",
        online_base_url,
        online_model,
        online_api_key,
        clear_online_key,
        local_base_url,
        local_model,
        local_api_key,
    )


@app.post("/settings/test-online")
def test_online_route(
    online_base_url: str = Form(""),
    online_model: str = Form(""),
    online_api_key: str = Form(""),
    clear_online_key: str = Form(""),
    local_base_url: str = Form(""),
    local_model: str = Form(""),
    local_api_key: str = Form(""),
) -> RedirectResponse:
    return _test_saved_connection(
        "online",
        online_base_url,
        online_model,
        online_api_key,
        clear_online_key,
        local_base_url,
        local_model,
        local_api_key,
    )


@app.get("/batches/new", response_class=HTMLResponse)
def new_batch_page(request: Request) -> HTMLResponse:
    return _render(request, "batch_new.html")


@app.post("/batches")
async def create_batch_route(
    request: Request,
    submissions: UploadFile = File(...),
    gradebook: UploadFile = File(...),
    model_answer_text: str = Form(""),
    model_answer_files: list[UploadFile] | None = File(None),
) -> Response:
    if not submissions.filename:
        return _render(
            request,
            "batch_new.html",
            error="Upload the submission zip from Canvas.",
            status_code=400,
        )
    if not gradebook.filename:
        return _render(
            request,
            "batch_new.html",
            error="Upload the gradebook CSV.",
            status_code=400,
        )
    batch = create_batch(model_answer_text.strip())
    try:
        await _fill_batch(
            batch.id,
            submissions,
            gradebook,
            model_answer_files or [],
            model_answer_text,
        )
    except Exception as exc:
        delete_batch(batch.id)
        if not isinstance(exc, (ValueError, zipfile.BadZipFile, OSError)):
            exc = ValueError(f"Could not read the files: {exc}")
        return _render(request, "batch_new.html", error=str(exc), status_code=400)
    return _redirect(f"/batches/{batch.id}")


@app.get("/batches/{batch_id}", response_class=HTMLResponse, response_model=None)
def batch_page(request: Request, batch_id: int) -> HTMLResponse | RedirectResponse:
    batch = _batch_or_404(batch_id)
    students = list_students(batch_id)
    running = is_running(batch_id)
    reads_to_keep = any(student.is_pilot and student.transcript for student in students)
    grades_to_keep = any(
        student.status in {"graded", "reviewed"} for student in students
    )
    if batch.status in {"reading", "grading"} or (running and batch.status != "review"):
        return _render(request, "batch_progress.html", batch=batch, running=running)
    if batch.status == "error" and grades_to_keep:
        return _redirect(f"/batches/{batch_id}/review")
    if batch.status == "error" and reads_to_keep and not batch.rubric_json:
        return _render(request, "batch_progress.html", batch=batch, running=False)
    if batch.status == "rubric_ready" or (
        batch.status == "error" and batch.rubric_json
    ):
        return _render(request, "batch_rubric.html", **_rubric_context(batch, students))
    if batch.status == "review":
        return _redirect(f"/batches/{batch_id}/review")
    gradebook = load_gradebook(batch_dir(batch_id) / "gradebook.csv")
    return _render(
        request,
        "batch_setup.html",
        batch=batch,
        students=students,
        columns=gradebook.assignment_columns(),
        selected_ids=_selected_ids(students),
        class_size=len(students),
        rubric_count=len(_selected_ids(students)),
        running_any=is_running(),
    )


@app.post("/batches/{batch_id}/read")
def start_read_route(
    batch_id: int,
    student_id: list[str] = Form(default=[]),
    rubric_count: str = Form("5"),
    rubric_provider: str = Form("online"),
    assignment_column: str = Form(""),
    points_possible: str = Form(""),
) -> RedirectResponse:
    batch = _batch_or_404(batch_id)
    if is_running(batch_id):
        return _redirect(f"/batches/{batch_id}", error="This batch is already running.")
    students = list_students(batch_id)
    try:
        points = _parse_points(points_possible)
        _validate_column(batch_id, assignment_column)
        _validate_provider(rubric_provider)
        endpoint_for(get_settings(), rubric_provider)
        count = _parse_count(rubric_count, len(students))
        selected = _choose_pilots(students, set(student_id), count)
    except (LLMError, ValueError) as exc:
        return _redirect(f"/batches/{batch_id}", error=str(exc))
    _assign_pilots(batch_id, [student.id for student in selected])
    update_batch(
        batch_id,
        assignment_column=assignment_column,
        points_possible=points,
        rubric_provider=rubric_provider,
        status="reading",
        error="",
        rubric_json="",
        job_current=0,
        job_total=len(selected) + 1,
        job_message="Preparing to start",
    )
    try:
        start_reading(batch.id)
    except RuntimeError as exc:
        update_batch(batch_id, status="setup", job_message="")
        return _redirect(f"/batches/{batch_id}", error=str(exc))
    return _redirect(f"/batches/{batch_id}")


@app.post("/batches/{batch_id}/continue-read")
def continue_read_route(batch_id: int) -> RedirectResponse:
    batch = _batch_or_404(batch_id)
    try:
        endpoint_for(get_settings(), batch.rubric_provider)
        start_reading(batch_id)
    except (LLMError, RuntimeError) as exc:
        return _redirect(f"/batches/{batch_id}", error=str(exc))
    return _redirect(f"/batches/{batch_id}")


@app.post("/batches/{batch_id}/rubric")
def save_rubric_route(
    batch_id: int,
    name: list[str] = Form(default=[]),
    max_points: list[str] = Form(default=[]),
    description: list[str] = Form(default=[]),
    grade_provider: str = Form("local"),
    action: str = Form("grade"),
) -> RedirectResponse:
    batch = _batch_or_404(batch_id)
    if is_running(batch_id):
        return _redirect(f"/batches/{batch_id}", error="This batch is already running.")
    try:
        criteria = _criteria_from_form(
            name, max_points, description, float(batch.points_possible or 0)
        )
        _validate_provider(grade_provider)
        endpoint_for(get_settings(), grade_provider)
    except (LLMError, ValueError) as exc:
        return _redirect(f"/batches/{batch_id}", error=str(exc))
    update_batch(
        batch_id,
        rubric_json=dump_criteria(criteria),
        grade_provider=grade_provider,
        error="",
    )
    if action == "save":
        update_batch(
            batch_id,
            status="rubric_ready",
            job_message="Rubric updated. The class has not been graded yet.",
        )
        return _redirect(f"/batches/{batch_id}", notice="Rubric saved.")
    update_batch(
        batch_id,
        status="grading",
        job_current=0,
        job_total=0,
        job_message="Preparing to grade",
    )
    try:
        start_grading(batch_id, resume=False)
    except RuntimeError as exc:
        update_batch(batch_id, status="rubric_ready", job_message="")
        return _redirect(f"/batches/{batch_id}", error=str(exc))
    return _redirect(f"/batches/{batch_id}")


@app.post("/batches/{batch_id}/continue-grade")
def continue_grade_route(batch_id: int) -> RedirectResponse:
    batch = _batch_or_404(batch_id)
    try:
        endpoint_for(get_settings(), batch.grade_provider)
        update_batch(
            batch_id,
            status="grading",
            error="",
            job_message="Continuing unfinished students",
        )
        start_grading(batch_id, resume=True)
    except (LLMError, RuntimeError) as exc:
        return _redirect(f"/batches/{batch_id}/review", error=str(exc))
    return _redirect(f"/batches/{batch_id}")


@app.post("/batches/{batch_id}/reopen")
def reopen_rubric_route(batch_id: int) -> RedirectResponse:
    _batch_or_404(batch_id)
    if is_running(batch_id):
        return _redirect(
            f"/batches/{batch_id}/review", error="This batch is already running."
        )
    update_batch(
        batch_id,
        status="rubric_ready",
        job_message="You can edit the rubric. Confirming it regrades the class.",
    )
    return _redirect(f"/batches/{batch_id}")


@app.post("/batches/{batch_id}/mark-reviewed")
def mark_reviewed_route(
    batch_id: int, viewed: list[str] = Form(default=[])
) -> RedirectResponse:
    """Set the viewed mark from the checkboxes. Only graded students are included."""
    batch = _batch_or_404(batch_id)
    if batch.status not in {"review", "grading", "error"}:
        return _redirect(f"/batches/{batch_id}", error="Review is not open yet.")
    chosen = {item.strip() for item in viewed}
    marked = 0
    cleared = 0
    for student in list_students(batch_id):
        if student.total_score is None:
            continue
        want = str(student.id) in chosen
        if want and not student.reviewed:
            update_student(student.id, reviewed=True, status="reviewed")
            marked += 1
        elif not want and student.reviewed:
            update_student(student.id, reviewed=False, status="graded")
            cleared += 1
    if not marked and not cleared:
        return _redirect(f"/batches/{batch_id}/review", notice="No view marks changed.")
    return _redirect(
        f"/batches/{batch_id}/review",
        notice=f"Saved viewed marks. {marked} marked, {cleared} cleared.",
    )


@app.get("/batches/{batch_id}/review", response_class=HTMLResponse, response_model=None)
def review_page(request: Request, batch_id: int) -> HTMLResponse | RedirectResponse:
    batch = _batch_or_404(batch_id)
    if batch.status not in {"review", "grading", "error"}:
        return _redirect(f"/batches/{batch_id}")
    students = _review_sort(list_students(batch_id))
    unfinished = any(student.status in {"pending", "error"} for student in students)
    return _render(
        request,
        "batch_review.html",
        batch=batch,
        students=students,
        unfinished=unfinished,
        running=is_running(batch_id),
    )


@app.get("/batches/{batch_id}/students/{student_id}", response_class=HTMLResponse)
def student_page(request: Request, batch_id: int, student_id: int) -> HTMLResponse:
    batch = _batch_or_404(batch_id)
    student = _student_or_404(batch_id, student_id)
    try:
        ensure_student_pages(student)
    except Exception as exc:
        update_student(student.id, error=str(exc)[:500])
    student = get_student(student_id)
    ordered = _review_sort(list_students(batch_id))
    position = [item.id for item in ordered].index(student.id)
    return _render(
        request,
        "student.html",
        batch=batch,
        student=student,
        rows=_score_rows(batch, student),
        page_count=len(list(pages_dir(batch_id, student_id).glob("*.jpg"))),
        previous_id=ordered[position - 1].id if position else None,
        next_id=ordered[position + 1].id if position + 1 < len(ordered) else None,
        position=position + 1,
        student_count=len(ordered),
    )


@app.post("/batches/{batch_id}/students/{student_id}")
def save_student_route(
    batch_id: int,
    student_id: int,
    points: list[str] = Form(default=[]),
    reason: list[str] = Form(default=[]),
    comment: str = Form(""),
    transcript: str = Form(""),
    reviewed: str = Form(""),
    illegible: str = Form(""),
) -> RedirectResponse:
    batch = _batch_or_404(batch_id)
    student = _student_or_404(batch_id, student_id)
    criteria = batch.criteria()
    if not criteria:
        return _redirect(
            f"/batches/{batch_id}/students/{student_id}",
            error="There is no rubric yet.",
        )
    if len(points) != len(criteria) or len(reason) != len(criteria):
        return _redirect(
            f"/batches/{batch_id}/students/{student_id}",
            error="The score fields do not match the rubric. Reload the page.",
        )
    scores: list[dict[str, str | float]] = []
    for criterion, raw_points, raw_reason in zip(criteria, points, reason, strict=True):
        maximum = float(criterion["max_points"])
        scores.append(
            {
                "name": criterion["name"],
                "max_points": maximum,
                "points": _clamp_points(raw_points, maximum),
                "reason": raw_reason.strip(),
            }
        )
    total = round(sum(float(item["points"]) for item in scores), 2)
    is_reviewed = reviewed == "1"
    update_student(
        student_id,
        scores_json=dump_scores(scores),
        comment=comment.strip(),
        transcript=transcript,
        total_score=total,
        reviewed=is_reviewed,
        illegible=illegible == "1",
        status="reviewed" if is_reviewed else "graded",
        error="",
    )
    if is_reviewed:
        remaining = [
            item
            for item in _review_sort(list_students(batch_id))
            if item.id != student.id and not item.reviewed
        ]
        if remaining:
            return _redirect(
                f"/batches/{batch_id}/students/{remaining[0].id}", notice="Saved."
            )
    return _redirect(f"/batches/{batch_id}/students/{student_id}", notice="Saved.")


@app.get("/batches/{batch_id}/students/{student_id}/pages/{index}")
def student_page_image(batch_id: int, student_id: int, index: int) -> FileResponse:
    _student_or_404(batch_id, student_id)
    return _image_response(
        pages_dir(batch_id, student_id) / f"{index:03d}.jpg",
        pages_dir(batch_id, student_id),
    )


@app.get("/batches/{batch_id}/answer/{index}")
def answer_image(batch_id: int, index: int) -> FileResponse:
    _batch_or_404(batch_id)
    return _image_response(
        answer_dir(batch_id) / f"{index:03d}.jpg", answer_dir(batch_id)
    )


@app.get("/batches/{batch_id}/export", response_class=HTMLResponse, response_model=None)
def export_page(request: Request, batch_id: int) -> HTMLResponse | RedirectResponse:
    batch = _batch_or_404(batch_id)
    if batch.status != "review":
        return _redirect(
            f"/batches/{batch_id}",
            error="Export is available after the rubric is confirmed and the class is graded.",
        )
    included, skipped = _export_split(batch, "all")
    reviewed, _ = _export_split(batch, "reviewed")
    return _render(
        request,
        "export.html",
        batch=batch,
        included=included,
        reviewed_count=len(reviewed),
        skipped=skipped,
    )


@app.get("/batches/{batch_id}/rubric.md")
def export_rubric(batch_id: int) -> Response:
    batch = _batch_or_404(batch_id)
    criteria = batch.criteria()
    if not criteria:
        raise HTTPException(status_code=400, detail="There is no rubric yet.")
    text = rubric_markdown(
        batch.assignment_column, float(batch.points_possible or 0), criteria
    )
    return Response(
        content=text.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="rubric-{batch_id}.md"'},
    )


@app.get("/batches/{batch_id}/export.csv")
def export_csv(batch_id: int, scope: str = "all") -> Response:
    batch = _batch_or_404(batch_id)
    if batch.status != "review":
        raise HTTPException(status_code=400, detail="Grading is not ready to export.")
    if scope not in {"all", "reviewed"}:
        raise HTTPException(status_code=400, detail="Unknown export scope.")
    text = _export_csv_text(batch, scope)
    filename = f"grades-{batch_id}.csv"
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/batches/{batch_id}/feedback.zip")
def export_feedback(batch_id: int) -> Response:
    batch = _batch_or_404(batch_id)
    if batch.status != "review":
        raise HTTPException(status_code=400, detail="Grading is not ready to export.")
    payload = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(payload, "w") as archive:
        for student in list_students(batch_id):
            if (
                student.total_score is None
                and not student.comment
                and not student.transcript
            ):
                continue
            filename = _unique_markdown_name(student, used)
            archive.writestr(
                filename,
                feedback_markdown(
                    student.display_name,
                    float(batch.points_possible or 0),
                    student.total_score,
                    student.scores(),
                    student.comment,
                    student.transcript,
                ),
            )
    return Response(
        content=payload.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="feedback-{batch_id}.zip"'
        },
    )


@app.get("/batches/{batch_id}/status.json")
def batch_status(batch_id: int) -> dict:
    batch = _batch_or_404(batch_id)
    running = is_running(batch_id)
    return {
        "status": batch.status,
        "running": running,
        "current": batch.job_current,
        "total": batch.job_total,
        "message": batch.job_message,
        "error": batch.error,
        "done": not running and batch.status not in {"reading", "grading"},
    }


@app.post("/batches/{batch_id}/delete")
def delete_batch_route(batch_id: int) -> RedirectResponse:
    _batch_or_404(batch_id)
    if is_running(batch_id):
        return _redirect(
            f"/batches/{batch_id}", error="This batch is running and cannot be deleted."
        )
    delete_batch(batch_id)
    return _redirect("/", notice="Batch deleted.")


def _render(
    request: Request, name: str, status_code: int = 200, **extra: object
) -> HTMLResponse:
    context = {
        "notice": request.query_params.get("notice", ""),
        "error": request.query_params.get("error", ""),
        **extra,
    }
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _redirect(path: str, *, notice: str = "", error: str = "") -> RedirectResponse:
    query = {}
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    target = path if not query else path + "?" + urlencode(query)
    return RedirectResponse(target, status_code=303)


def _test_saved_connection(
    provider: str,
    online_base_url: str,
    online_model: str,
    online_api_key: str,
    clear_online_key: str,
    local_base_url: str,
    local_model: str,
    local_api_key: str,
) -> RedirectResponse:
    _store_settings(
        online_base_url,
        online_model,
        online_api_key,
        clear_online_key,
        local_base_url,
        local_model,
        local_api_key,
    )
    if is_running():
        return _redirect(
            "/settings",
            error="A batch is grading. Wait until it finishes before testing the connection.",
        )
    try:
        result = test_endpoint(endpoint_for(get_settings(), provider))
    except LLMError as exc:
        return _redirect("/settings", error=str(exc))
    if result.ok:
        return _redirect("/settings", notice=result.message)
    return _redirect("/settings", error=result.message)


def _store_settings(
    online_base_url: str,
    online_model: str,
    online_api_key: str,
    clear_online_key: str,
    local_base_url: str,
    local_model: str,
    local_api_key: str,
) -> None:
    current = get_settings()
    if online_api_key.strip():
        key = online_api_key.strip()
    elif clear_online_key == "1":
        key = ""
    else:
        key = current.online_api_key
    save_settings(
        Settings(
            online_base_url=online_base_url.strip() or DEFAULT_ONLINE_BASE_URL,
            online_model=online_model.strip() or DEFAULT_ONLINE_MODEL,
            online_api_key=key,
            local_base_url=local_base_url.strip() or DEFAULT_LOCAL_BASE_URL,
            local_model=local_model.strip(),
            local_api_key=local_api_key.strip(),
        )
    )


async def _fill_batch(
    batch_id: int,
    submissions: UploadFile,
    gradebook: UploadFile,
    answer_files: list[UploadFile],
    model_answer_text: str,
) -> None:
    root = batch_dir(batch_id)
    zip_path = root / "submissions.zip"
    gradebook_path = root / "gradebook.csv"
    await _write_upload(submissions, zip_path)
    await _write_upload(gradebook, gradebook_path)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("The upload is not a zip. Use Canvas Download Submissions.")
    try:
        loaded = load_gradebook(gradebook_path)
    except ValueError as exc:
        raise ValueError(f"Could not read the gradebook: {exc}") from exc
    if not loaded.assignment_columns():
        raise ValueError(
            "The gradebook has no assignment column to fill. Export a CSV that includes the assignment."
        )
    groups = extract_submission_zip(zip_path, extracted_dir(batch_id))
    if not groups:
        raise ValueError("The zip has no submission files.")
    texts = [model_answer_text.strip()]
    answer_sources: list[Path] = []
    upload_dir = root / "answer_upload"
    for index, upload in enumerate(answer_files):
        if not upload.filename:
            continue
        suffix = Path(upload.filename).suffix.casefold()
        target = upload_dir / f"{index:03d}{suffix}"
        await _write_upload(upload, target)
        if suffix in TEXT_EXTENSIONS:
            texts.append(target.read_text(encoding="utf-8", errors="replace"))
        elif suffix in IMAGE_EXTENSIONS or suffix in PDF_EXTENSIONS:
            answer_sources.append(target)
        else:
            raise ValueError(f"Unsupported answer file: {upload.filename}")
    answer = "\n\n".join(part for part in texts if part.strip())
    if not answer and not answer_sources:
        raise ValueError(
            "Paste a model answer, or upload a PDF or images of the answer."
        )
    prepare_answer_files(answer_sources, answer_dir(batch_id))
    update_batch(batch_id, model_answer_text=answer)
    for group in groups:
        row_index = loaded.find_student_row(group.user_id, group.display_name)
        if row_index is None:
            display_name = group.display_name
            user_id = group.user_id
            matched = False
        else:
            display_name = loaded.student_name(row_index) or group.display_name
            user_id = loaded.student_id(row_index) or group.user_id
            matched = True
        add_student(
            batch_id,
            canvas_user_id=user_id,
            display_name=display_name,
            filename_name=group.display_name,
            parsed=group.parsed,
            matched=matched,
            gradebook_row=row_index,
            source_files=[str(path.resolve()) for path in group.files],
        )


async def _write_upload(upload: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def _batch_or_404(batch_id: int) -> Batch:
    try:
        return get_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found") from exc


def _student_or_404(batch_id: int, student_id: int) -> Student:
    try:
        student = get_student(student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Student not found") from exc
    if student.batch_id != batch_id:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


def _image_response(path: Path, root: Path) -> FileResponse:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(resolved, media_type="image/jpeg")


def _validate_provider(provider: str) -> None:
    if provider not in {"online", "local"}:
        raise ValueError("Choose online or local.")


def _validate_column(batch_id: int, column: str) -> None:
    gradebook = load_gradebook(batch_dir(batch_id) / "gradebook.csv")
    headers = {header for _, header, _ in gradebook.assignment_columns()}
    if column not in headers:
        raise ValueError("Choose an assignment column from the gradebook.")


def _parse_points(value: str) -> float:
    try:
        points = float(value.strip())
    except ValueError as exc:
        raise ValueError("Points possible must be a number.") from exc
    if points <= 0:
        raise ValueError("Points possible must be greater than 0.")
    return round(points, 2)


def _parse_count(value: str, total: int) -> int:
    if total < 1:
        raise ValueError("This batch has no students.")
    try:
        count = int(value.strip())
    except ValueError as exc:
        raise ValueError(
            "The number of papers for the rubric must be a whole number."
        ) from exc
    if count < 1 or count > total:
        raise ValueError(f"Use between 1 and {total} papers for the rubric.")
    return count


def _choose_pilots(
    students: list[Student], selected_ids: set[str], required: int
) -> list[Student]:
    chosen = [student for student in students if str(student.id) in selected_ids]
    if len(chosen) != required:
        raise ValueError(f"Select {required} students. {len(chosen)} are checked.")
    return chosen


def _pilot_label(index: int) -> str:
    """A, B, … Z, AA. The label is the only student identifier sent to a model."""
    number = index + 1
    letters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _assign_pilots(batch_id: int, selected_ids: list[int]) -> None:
    selected = set(selected_ids)
    for student in list_students(batch_id):
        update_student(
            student.id,
            is_pilot=student.id in selected,
            pilot_label="",
            transcript="",
            differences="",
            illegible=False,
            status="pending",
            error="",
            scores_json="",
            comment="",
            total_score=None,
            reviewed=False,
        )
    for index, student_id in enumerate(selected_ids):
        update_student(student_id, pilot_label=_pilot_label(index))


def _selected_ids(students: list[Student]) -> set[int]:
    marked = {student.id for student in students if student.is_pilot}
    if marked:
        return marked
    parsed = [student for student in students if student.parsed]
    pool = parsed if len(parsed) >= 5 else students
    return {student.id for student in pool[:5]}


def _criteria_from_form(
    names: list[str],
    max_points: list[str],
    descriptions: list[str],
    points_possible: float,
) -> list[dict]:
    if not (len(names) == len(max_points) == len(descriptions)):
        raise ValueError("The rubric fields are incomplete.")
    criteria = []
    for name, raw_points, description in zip(
        names, max_points, descriptions, strict=True
    ):
        if not name.strip():
            continue
        try:
            points = round(float(raw_points.strip()), 2)
        except ValueError as exc:
            raise ValueError(
                f'Max points for "{name.strip()}" is not a number.'
            ) from exc
        if points < 0:
            raise ValueError(f'Max points for "{name.strip()}" cannot be negative.')
        criteria.append(
            {
                "name": name.strip(),
                "max_points": points,
                "description": description.strip(),
            }
        )
    if not criteria:
        raise ValueError("Add at least one rubric criterion.")
    if not criteria_match_total(criteria, points_possible):
        raise ValueError(
            f"Criterion points total {format_points(criteria_total(criteria))}, but they must equal the assignment total {format_points(points_possible)}."
        )
    return criteria


def _rubric_context(batch: Batch, students: list[Student]) -> dict:
    pilots = [student for student in students if student.is_pilot]
    pilots.sort(key=lambda student: student.pilot_label)
    for student in pilots:
        try:
            ensure_student_pages(student)
        except Exception as exc:
            update_student(student.id, error=str(exc)[:500])
    pilots = [get_student(student.id) for student in pilots]
    papers = [
        {
            "label": student.pilot_label,
            "transcript": student.transcript,
            "differences": student.differences,
        }
        for student in pilots
    ]
    answer_count = (
        len(list(answer_dir(batch.id).glob("*.jpg")))
        if answer_dir(batch.id).is_dir()
        else 0
    )
    pilot_pages = {
        student.id: len(list(pages_dir(batch.id, student.id).glob("*.jpg")))
        for student in pilots
    }
    return {
        "batch": batch,
        "pilots": pilots,
        "pilot_pages": pilot_pages,
        "criteria": batch.criteria(),
        "discussion": rubric_discussion_text(
            batch.criteria(), papers, float(batch.points_possible or 0)
        ),
        "answer_count": answer_count,
        "criteria_sum": criteria_total(batch.criteria()) if batch.criteria() else 0,
        "running_any": is_running(),
    }


def _score_rows(batch: Batch, student: Student) -> list[dict]:
    saved = {item["name"]: item for item in student.scores()}
    rows = []
    for criterion in batch.criteria():
        found = saved.get(criterion["name"], {})
        points = found.get("points", "") if student.scores_json else ""
        rows.append(
            {
                "name": criterion["name"],
                "max_points": criterion["max_points"],
                "description": criterion.get("description", ""),
                "points": points,
                "reason": found.get("reason", ""),
            }
        )
    return rows


def _clamp_points(raw: str, maximum: float) -> float:
    try:
        value = float(raw.strip())
    except ValueError:
        value = 0.0
    return round(min(maximum, max(0.0, value)), 2)


def _review_sort(students: list[Student]) -> list[Student]:
    def rank(student: Student) -> int:
        if student.status == "error":
            return 0
        if student.total_score is not None and not student.reviewed:
            return 1
        if not student.reviewed:
            return 2
        return 3

    return sorted(
        students,
        key=lambda student: (
            rank(student),
            student.display_name.casefold(),
            student.id,
        ),
    )


def _export_split(batch: Batch, scope: str) -> tuple[list[Student], list[Student]]:
    included: list[Student] = []
    skipped: list[Student] = []
    for student in list_students(batch.id):
        has_score = student.total_score is not None
        take = has_score and student.reviewed if scope == "reviewed" else has_score
        if not take:
            continue
        if student.matched and student.canvas_user_id:
            included.append(student)
        else:
            skipped.append(student)
    return included, skipped


def _export_csv_text(batch: Batch, scope: str) -> str:
    included, _skipped = _export_split(batch, scope)
    gradebook = load_gradebook(batch_dir(batch.id) / "gradebook.csv")
    updates = {
        student.canvas_user_id: format_points(student.total_score)
        for student in included
        if student.canvas_user_id and student.total_score is not None
    }
    apply_scores(gradebook, batch.assignment_column, updates)
    return gradebook_csv_text(gradebook)


def _unique_markdown_name(student: Student, used: set[str]) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in " _-" else "_"
        for character in student.display_name
    ).strip()
    if not cleaned:
        cleaned = f"student-{student.id}"
    candidate = cleaned
    if candidate in used:
        candidate = f"{cleaned}-{student.id}"
    used.add(candidate)
    return candidate + ".md"


if __name__ == "__main__":
    main()
