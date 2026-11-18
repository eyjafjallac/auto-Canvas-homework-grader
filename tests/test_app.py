"""The pages render, and a zip plus gradebook becomes a student list."""

import io
import sqlite3
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from canvas_grader.main import app
from canvas_grader.store import get_settings, init_db


def test_grading_batch_links_to_finished_students(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    from canvas_grader.store import add_student, create_batch, update_batch

    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id,
            status="grading",
            assignment_column="HW5",
            job_message="Grading Ada: Problem 1 (1/2)",
            job_current=1,
            job_total=2,
        )
        add_student(
            batch.id,
            canvas_user_id="161437",
            display_name="Ada",
            filename_name="ada",
            parsed=True,
            matched=True,
            gradebook_row=1,
            source_files=[],
        )
        home = client.get("/")
        progress = client.get(f"/batches/{batch.id}")
        review = client.get(f"/batches/{batch.id}/review")
    assert home.status_code == 200
    assert "Review finished" in home.text
    assert progress.status_code == 200
    assert "Review finished students" in progress.text
    assert review.status_code == 200
    assert "Ada" in review.text


def test_mark_graded_students_reviewed_leaves_the_ungraded(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    from canvas_grader.store import (
        add_student,
        create_batch,
        get_student,
        update_batch,
        update_student,
    )

    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id, status="review", assignment_column="HW5", points_possible=3
        )
        graded = add_student(
            batch.id,
            canvas_user_id="1",
            display_name="Ada",
            filename_name="ada",
            parsed=True,
            matched=True,
            gradebook_row=1,
            source_files=[],
        )
        waiting = add_student(
            batch.id,
            canvas_user_id="2",
            display_name="Grace",
            filename_name="grace",
            parsed=True,
            matched=True,
            gradebook_row=2,
            source_files=[],
        )
        update_student(graded, total_score=2, status="graded", scores_json="[]")
        page = client.get(f"/batches/{batch.id}/review")
        response = client.post(
            f"/batches/{batch.id}/mark-reviewed",
            data={"viewed": str(graded)},
            follow_redirects=False,
        )
        ada = get_student(graded)
        grace = get_student(waiting)
        cleared = client.post(
            f"/batches/{batch.id}/mark-reviewed", follow_redirects=False
        )
        ada_after = get_student(graded)
    assert page.status_code == 200
    assert f'name="viewed" value="{graded}"' in page.text
    assert "Save viewed" in page.text
    assert response.status_code == 303
    assert ada.reviewed is True
    assert ada.status == "reviewed"
    assert ada.total_score == 2
    assert grace.reviewed is False
    assert grace.total_score is None
    assert cleared.status_code == 303
    assert ada_after.reviewed is False
    assert ada_after.status == "graded"


def test_review_highlights_scores_below_the_maximum(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    from canvas_grader.store import (
        add_student,
        create_batch,
        update_batch,
        update_student,
    )

    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id,
            status="review",
            assignment_column="HW5",
            points_possible=3,
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Problem 1: answer",
                            "max_points": 2,
                            "description": "final",
                        },
                        {
                            "name": "Problem 2: work",
                            "max_points": 1,
                            "description": "algebra",
                        },
                    ]
                }
            ),
        )
        student_id = add_student(
            batch.id,
            canvas_user_id="1",
            display_name="Ada",
            filename_name="ada",
            parsed=True,
            matched=True,
            gradebook_row=1,
            source_files=[],
        )
        update_student(
            student_id,
            status="graded",
            total_score=2,
            scores_json=json.dumps(
                [
                    {
                        "name": "Problem 1: answer",
                        "max_points": 2,
                        "points": 1,
                        "reason": "missed a term",
                    },
                    {
                        "name": "Problem 2: work",
                        "max_points": 1,
                        "points": 1,
                        "reason": "",
                    },
                ]
            ),
        )
        review = client.get(f"/batches/{batch.id}/review")
        student = client.get(f"/batches/{batch.id}/students/{student_id}")
    assert review.status_code == 200
    assert 'class="short"' in review.text
    assert student.status_code == 200
    assert student.text.count('class="score-row short"') == 1


def test_student_page_switches_original_pdf_and_split_pages(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    import pymupdf

    from canvas_grader.store import add_student, batch_dir, create_batch, update_batch

    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id, status="review", assignment_column="HW5", points_possible=1
        )
        pdf_path = batch_dir(batch.id) / "extracted" / "ada.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "Original page")
        document.save(pdf_path)
        document.close()
        student_id = add_student(
            batch.id,
            canvas_user_id="1",
            display_name="Ada",
            filename_name="ada",
            parsed=True,
            matched=True,
            gradebook_row=1,
            source_files=[str(pdf_path.resolve())],
        )
        outside = tmp_path / "secret.pdf"
        outside.write_bytes(b"%PDF-1.4 secret")
        page = client.get(f"/batches/{batch.id}/students/{student_id}")
        original = client.get(f"/batches/{batch.id}/students/{student_id}/sources/0")
        missing = client.get(f"/batches/{batch.id}/students/{student_id}/sources/4")
        connection = sqlite3.connect(tmp_path / "grader.db")
        connection.execute(
            "UPDATE students SET source_files = ? WHERE id = ?",
            (json.dumps([str(outside.resolve())]), student_id),
        )
        connection.commit()
        connection.close()
        blocked = client.get(f"/batches/{batch.id}/students/{student_id}/sources/0")
    assert page.status_code == 200
    assert "Original PDF" in page.text
    assert "Split pages" in page.text
    assert "review-dock" in page.text
    assert 'data-work-pane="original"' in page.text
    assert 'data-work-pane="chunks"' in page.text
    assert original.status_code == 200
    assert original.headers["content-type"].startswith("application/pdf")
    assert "inline" in original.headers["content-disposition"]
    assert missing.status_code == 404
    assert blocked.status_code == 404


def test_home_renders(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Homework grader" in response.text


def test_home_asks_for_a_connection_until_one_is_saved(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    with TestClient(app) as client:
        home = client.get("/")
        settings = client.get("/settings")
        saved = client.post(
            "/settings",
            data={
                "online_base_url": "https://api.deepseek.com",
                "online_model": "deepseek-flash",
                "online_api_key": "sk-test",
                "local_base_url": "http://127.0.0.1:1234/v1",
                "local_model": "",
                "local_api_key": "",
            },
            follow_redirects=True,
        )
        after = client.get("/")
    assert home.status_code == 200
    assert "Choose a model first" in home.text
    assert "paste an online API key" in home.text
    assert settings.status_code == 200
    assert "Fill in one connection before the first batch." in settings.text
    assert saved.status_code == 200
    assert "Choose a model first" not in after.text


def test_upload_lists_students(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("DoeJohn_111_67890_hw.pdf", b"not a real pdf")
    gradebook = (
        "Student,ID,SIS User ID,SIS Login ID,Section,HW1 (10)\n"
        "Points Possible,,,,,10\n"
        '"Doe, John",111,s1,doe,A,\n'
    )
    with TestClient(app) as client:
        response = client.post(
            "/batches",
            data={"model_answer_text": "The answer is 4."},
            files={
                "submissions": ("subs.zip", archive.getvalue(), "application/zip"),
                "gradebook": ("grades.csv", gradebook.encode(), "text/csv"),
            },
        )
    assert response.status_code == 200
    assert "Doe, John" in response.text
    assert "HW1 (10)" in response.text
    assert 'name="rubric_count"' in response.text


def test_empty_local_model_disables_the_local_choice(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    from canvas_grader.store import Settings, create_batch, save_settings, update_batch

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("DoeJohn_111_67890_hw.pdf", b"not a real pdf")
    gradebook = (
        "Student,ID,SIS User ID,SIS Login ID,Section,HW1 (10)\n"
        "Points Possible,,,,,10\n"
        '"Doe, John",111,s1,doe,A,\n'
    )
    with TestClient(app) as client:
        setup = client.post(
            "/batches",
            data={"model_answer_text": "The answer is 4."},
            files={
                "submissions": ("subs.zip", archive.getvalue(), "application/zip"),
                "gradebook": ("grades.csv", gradebook.encode(), "text/csv"),
            },
        )
        batch = create_batch("")
        update_batch(
            batch.id,
            status="rubric_ready",
            assignment_column="HW1",
            points_possible=10,
            grade_provider="local",
        )
        grade = client.get(f"/batches/{batch.id}")
        current = get_settings()
        save_settings(
            Settings(
                online_base_url=current.online_base_url,
                online_model=current.online_model,
                online_api_key=current.online_api_key,
                local_base_url=current.local_base_url,
                local_model="qwen-vl",
                local_api_key=current.local_api_key,
            )
        )
        ready = client.get(f"/batches/{batch.id}")
    assert setup.status_code == 200
    assert 'name="rubric_provider" value="local" disabled' in setup.text
    assert 'name="rubric_provider" value="online" checked' in setup.text
    assert "No local model is saved" in setup.text
    assert grade.status_code == 200
    assert 'name="grade_provider" value="local" disabled' in grade.text
    assert 'name="grade_provider" value="online" checked' in grade.text
    assert 'name="grade_provider" value="local" checked' in ready.text
    assert 'name="grade_provider" value="local" disabled' not in ready.text


def test_settings_page_shows_online_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/settings")
    assert response.status_code == 200
    assert "https://api.deepseek.com" in response.text
    assert "deepseek-flash" in response.text
    assert 'name="online_model"' in response.text


def test_old_settings_row_keeps_the_saved_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    database = tmp_path / "grader.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            deepseek_api_key TEXT NOT NULL DEFAULT '',
            local_base_url TEXT NOT NULL DEFAULT '',
            local_model TEXT NOT NULL DEFAULT '',
            local_api_key TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "INSERT INTO settings (id, deepseek_api_key, local_base_url) VALUES (1, 'secret-key', 'http://127.0.0.1:1234/v1')"
    )
    connection.commit()
    connection.close()
    init_db()
    settings = get_settings()
    assert settings.online_api_key == "secret-key"
    assert settings.online_base_url == "https://api.deepseek.com"
    assert settings.online_model == "deepseek-flash"


def test_old_batch_row_gains_baseline_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    database = tmp_path / "grader.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE batches (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            assignment_column TEXT NOT NULL DEFAULT '',
            points_possible REAL,
            model_answer_text TEXT NOT NULL DEFAULT '',
            rubric_provider TEXT NOT NULL DEFAULT 'online',
            grade_provider TEXT NOT NULL DEFAULT 'local',
            status TEXT NOT NULL DEFAULT 'setup',
            error TEXT NOT NULL DEFAULT '',
            job_current INTEGER NOT NULL DEFAULT 0,
            job_total INTEGER NOT NULL DEFAULT 0,
            job_message TEXT NOT NULL DEFAULT '',
            rubric_json TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "INSERT INTO batches (created_at, assignment_column) VALUES ('2020-01-01T00:00:00', 'HW1')"
    )
    connection.commit()
    connection.close()
    init_db()
    from canvas_grader.store import get_batch

    batch = get_batch(1)
    assert batch.assignment_column == "HW1"
    assert batch.rubric_guidance == ""
    assert batch.rubric_revision == ""


def test_baseline_stays_when_the_sample_form_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    from canvas_grader.store import add_student, batch_dir, create_batch

    with TestClient(app) as client:
        batch = create_batch("The answer is 4.")
        (batch_dir(batch.id) / "gradebook.csv").write_text(
            "Student,ID,SIS User ID,SIS Login ID,Section,HW1 (10)\n"
            "Points Possible,,,,,10\n"
            '"Doe, John",111,s1,doe,A,\n',
            encoding="utf-8",
        )
        add_student(
            batch.id,
            canvas_user_id="111",
            display_name="Doe, John",
            filename_name="DoeJohn",
            parsed=True,
            matched=True,
            gradebook_row=1,
            source_files=[],
        )
        response = client.post(
            f"/batches/{batch.id}/read",
            data={
                "rubric_guidance": "Units matter.",
                "points_possible": "nope",
                "assignment_column": "HW1 (10)",
                "rubric_count": "1",
                "rubric_provider": "online",
            },
        )
    assert response.status_code == 200
    assert "Points possible must be a number." in response.text
    assert 'name="rubric_guidance"' in response.text
    assert "Units matter." in response.text


def test_rubric_page_can_ask_for_another_draft(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    from canvas_grader.store import create_batch, update_batch

    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id,
            status="rubric_ready",
            assignment_column="HW1",
            points_possible=10,
            rubric_guidance="Units are required.",
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Answer",
                            "max_points": 10,
                            "description": "Final number.",
                        }
                    ]
                }
            ),
            job_message="The rubric draft is ready. Confirm it before grading the class",
        )
        page = client.get(f"/batches/{batch.id}")
    assert page.status_code == 200
    assert "Units are required." in page.text
    assert 'name="rubric_revision"' in page.text
    assert "Ask the model to remake this rubric" in page.text
    assert f'formaction="/batches/{batch.id}/revise-rubric"' in page.text
    assert "The rubric draft is ready" in page.text


def test_remake_uses_the_draft_on_the_page(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    from canvas_grader.store import (
        Settings,
        create_batch,
        get_batch,
        get_settings,
        save_settings,
        update_batch,
    )

    started: list[int] = []
    monkeypatch.setattr(
        "canvas_grader.main.start_revising", lambda batch_id: started.append(batch_id)
    )
    with TestClient(app) as client:
        current = get_settings()
        save_settings(
            Settings(
                online_base_url=current.online_base_url,
                online_model=current.online_model,
                online_api_key="test-key",
                local_base_url=current.local_base_url,
                local_model=current.local_model,
                local_api_key=current.local_api_key,
            )
        )
        batch = create_batch("4 N")
        update_batch(
            batch.id,
            status="rubric_ready",
            assignment_column="HW1",
            points_possible=10,
            rubric_provider="online",
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Answer",
                            "max_points": 10,
                            "description": "Final number.",
                        }
                    ]
                }
            ),
        )
        response = client.post(
            f"/batches/{batch.id}/revise-rubric",
            data={
                "name": "Method",
                "max_points": "6",
                "description": "Shows the steps.",
                "grade_provider": "online",
                "action": "revise",
                "rubric_guidance": "Units are required.",
                "rubric_revision": "Give the method its own points.",
            },
            follow_redirects=False,
        )
        saved = get_batch(batch.id)
    assert response.status_code == 303
    assert saved.status == "revising"
    assert saved.criteria()[0]["name"] == "Method"
    assert saved.criteria()[0]["max_points"] == 6
    assert saved.rubric_guidance == "Units are required."
    assert saved.rubric_revision == "Give the method its own points."
    assert started == [batch.id]


def test_remake_without_a_note_keeps_the_current_rubric(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json
    from urllib.parse import unquote_plus

    from canvas_grader.store import create_batch, get_batch, update_batch

    started: list[int] = []
    monkeypatch.setattr(
        "canvas_grader.main.start_revising", lambda batch_id: started.append(batch_id)
    )
    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id,
            status="rubric_ready",
            assignment_column="HW1",
            points_possible=10,
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Answer",
                            "max_points": 10,
                            "description": "Final number.",
                        }
                    ]
                }
            ),
        )
        response = client.post(
            f"/batches/{batch.id}/revise-rubric",
            data={
                "name": "Method",
                "max_points": "10",
                "description": "Shows the steps.",
                "grade_provider": "online",
                "action": "revise",
                "rubric_guidance": "Units are required.",
                "rubric_revision": "   ",
            },
            follow_redirects=False,
        )
        saved = get_batch(batch.id)
    assert response.status_code == 303
    assert "Write what should change" in unquote_plus(response.headers["location"])
    assert saved.status == "rubric_ready"
    assert saved.criteria()[0]["name"] == "Answer"
    assert saved.rubric_guidance == "Units are required."
    assert started == []


def test_remake_keeps_edits_when_the_model_is_not_configured(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    from canvas_grader.store import (
        Settings,
        create_batch,
        get_batch,
        get_settings,
        save_settings,
        update_batch,
    )

    def fail(batch_id: int) -> None:
        raise AssertionError(f"rewrite started for {batch_id}")

    monkeypatch.setattr("canvas_grader.main.start_revising", fail)
    with TestClient(app) as client:
        current = get_settings()
        save_settings(
            Settings(
                online_base_url=current.online_base_url,
                online_model=current.online_model,
                online_api_key="",
                local_base_url=current.local_base_url,
                local_model=current.local_model,
                local_api_key=current.local_api_key,
            )
        )
        batch = create_batch("")
        update_batch(
            batch.id,
            status="rubric_ready",
            assignment_column="HW1",
            points_possible=10,
            rubric_provider="online",
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Answer",
                            "max_points": 10,
                            "description": "Final number.",
                        }
                    ]
                }
            ),
        )
        page = client.post(
            f"/batches/{batch.id}/revise-rubric",
            data={
                "name": "Final answer",
                "max_points": "10",
                "description": "Final number with units.",
                "grade_provider": "online",
                "action": "revise",
                "rubric_guidance": "Units are required.",
                "rubric_revision": "Split the final number from the units.",
            },
        )
        saved = get_batch(batch.id)
    assert page.status_code == 200
    assert "No online API key yet" in page.text
    assert saved.status == "rubric_ready"
    assert saved.criteria()[0]["name"] == "Final answer"
    assert saved.rubric_revision == "Split the final number from the units."
    assert 'value="Final answer"' in page.text


def test_cancel_revise_returns_to_the_rubric(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    from canvas_grader.store import create_batch, get_batch, update_batch

    with TestClient(app) as client:
        batch = create_batch("")
        update_batch(
            batch.id,
            status="revising",
            assignment_column="HW1",
            points_possible=10,
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Answer",
                            "max_points": 10,
                            "description": "Final number.",
                        }
                    ]
                }
            ),
        )
        progress = client.get(f"/batches/{batch.id}")
        home = client.get("/")
        response = client.post(
            f"/batches/{batch.id}/cancel-revise", follow_redirects=False
        )
        saved = get_batch(batch.id)
    assert progress.status_code == 200
    assert "Try the rewrite again" in progress.text
    assert "Back to the rubric" in progress.text
    assert "Rewriting rubric" in home.text
    assert response.status_code == 303
    assert saved.status == "rubric_ready"


def test_rewrite_keeps_the_previous_rubric_when_the_model_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    import json

    from canvas_grader.jobs import _revise
    from canvas_grader.llm import Endpoint, LLMError
    from canvas_grader.store import (
        add_student,
        create_batch,
        get_batch,
        update_batch,
        update_student,
    )

    monkeypatch.setattr(
        "canvas_grader.jobs.endpoint_for",
        lambda settings, provider: Endpoint(
            "Online model", "http://127.0.0.1:9", "key", "model"
        ),
    )
    seen: dict[str, object] = {}

    def fail(endpoint, **kwargs):
        seen.update(kwargs)
        raise LLMError("offline")

    monkeypatch.setattr("canvas_grader.jobs.revise_rubric", fail)
    with TestClient(app) as client:
        batch = create_batch("4 N")
        update_batch(
            batch.id,
            status="revising",
            assignment_column="HW1",
            points_possible=10,
            rubric_provider="online",
            rubric_guidance="Units are required.",
            rubric_revision="Give units their own criterion.",
            rubric_json=json.dumps(
                {
                    "criteria": [
                        {
                            "name": "Answer",
                            "max_points": 10,
                            "description": "Final number.",
                        }
                    ]
                }
            ),
        )
        student_id = add_student(
            batch.id,
            canvas_user_id="1",
            display_name="Ada",
            filename_name="ada",
            parsed=True,
            matched=True,
            gradebook_row=1,
            source_files=[],
        )
        update_student(
            student_id,
            is_pilot=True,
            pilot_label="A",
            transcript="4",
            differences="no units",
            status="read",
        )
        _revise(batch.id)
        failed = get_batch(batch.id)

        def succeed(endpoint, **kwargs):
            return [
                {
                    "name": "Units",
                    "max_points": 10,
                    "description": "The number has units.",
                }
            ]

        monkeypatch.setattr("canvas_grader.jobs.revise_rubric", succeed)
        update_batch(batch.id, status="revising", error="")
        _revise(batch.id)
        done = get_batch(batch.id)
        page = client.get(f"/batches/{batch.id}")
    assert failed.status == "rubric_ready"
    assert failed.criteria()[0]["name"] == "Answer"
    assert "offline" in failed.error
    assert seen["revision_notes"] == "Give units their own criterion."
    assert seen["guidance"] == "Units are required."
    assert seen["current_criteria"][0]["name"] == "Answer"
    assert seen["papers"][0]["label"] == "A"
    assert done.status == "rubric_ready"
    assert done.error == ""
    assert done.criteria()[0]["name"] == "Units"
    assert "started from the previous one" in page.text
