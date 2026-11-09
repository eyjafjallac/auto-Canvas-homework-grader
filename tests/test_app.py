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


def test_home_renders(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANVAS_GRADER_DATA", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Homework grader" in response.text


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
