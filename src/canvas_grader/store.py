"""SQLite storage for settings, batches, and per-student grading state."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from canvas_grader.config import (
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_ONLINE_BASE_URL,
    DEFAULT_ONLINE_MODEL,
    data_dir,
)

_lock = threading.Lock()

_BATCH_FIELDS = {
    "assignment_column",
    "points_possible",
    "model_answer_text",
    "rubric_provider",
    "grade_provider",
    "status",
    "error",
    "job_current",
    "job_total",
    "job_message",
    "rubric_json",
}

_STUDENT_FIELDS = {
    "display_name",
    "is_pilot",
    "pilot_label",
    "matched",
    "gradebook_row",
    "transcript",
    "differences",
    "illegible",
    "scores_json",
    "comment",
    "total_score",
    "status",
    "error",
    "truncated",
    "reviewed",
    "text_body",
    "page_count",
}


@dataclass
class Settings:
    online_base_url: str
    online_model: str
    online_api_key: str
    local_base_url: str
    local_model: str
    local_api_key: str


@dataclass
class Batch:
    id: int
    created_at: str
    assignment_column: str
    points_possible: float | None
    model_answer_text: str
    rubric_provider: str
    grade_provider: str
    status: str
    error: str
    job_current: int
    job_total: int
    job_message: str
    rubric_json: str

    def criteria(self) -> list[dict[str, Any]]:
        if not self.rubric_json:
            return []
        payload = json.loads(self.rubric_json)
        criteria = payload.get("criteria", [])
        return list(criteria)


@dataclass
class Student:
    id: int
    batch_id: int
    canvas_user_id: str | None
    display_name: str
    filename_name: str
    parsed: bool
    is_pilot: bool
    pilot_label: str
    matched: bool
    gradebook_row: int | None
    source_files: list[str]
    transcript: str
    differences: str
    illegible: bool
    scores_json: str
    comment: str
    total_score: float | None
    status: str
    error: str
    truncated: bool
    reviewed: bool
    text_body: str
    page_count: int

    def scores(self) -> list[dict[str, Any]]:
        if not self.scores_json:
            return []
        payload = json.loads(self.scores_json)
        return list(payload)


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                deepseek_api_key TEXT NOT NULL DEFAULT '',
                local_base_url TEXT NOT NULL DEFAULT '',
                local_model TEXT NOT NULL DEFAULT '',
                local_api_key TEXT NOT NULL DEFAULT '',
                online_base_url TEXT NOT NULL DEFAULT '',
                online_model TEXT NOT NULL DEFAULT '',
                online_api_key TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS batches (
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
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                canvas_user_id TEXT,
                display_name TEXT NOT NULL,
                filename_name TEXT NOT NULL DEFAULT '',
                parsed INTEGER NOT NULL DEFAULT 0,
                is_pilot INTEGER NOT NULL DEFAULT 0,
                pilot_label TEXT NOT NULL DEFAULT '',
                matched INTEGER NOT NULL DEFAULT 0,
                gradebook_row INTEGER,
                source_files TEXT NOT NULL DEFAULT '[]',
                transcript TEXT NOT NULL DEFAULT '',
                differences TEXT NOT NULL DEFAULT '',
                illegible INTEGER NOT NULL DEFAULT 0,
                scores_json TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                total_score REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                truncated INTEGER NOT NULL DEFAULT 0,
                reviewed INTEGER NOT NULL DEFAULT 0,
                text_body TEXT NOT NULL DEFAULT '',
                page_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        _migrate_settings(conn)
        row = conn.execute("SELECT id FROM settings WHERE id = 1").fetchone()
        if row is None:
            online_key = os.environ.get("ONLINE_API_KEY") or os.environ.get(
                "DEEPSEEK_API_KEY", ""
            )
            online_base = os.environ.get("ONLINE_BASE_URL") or os.environ.get(
                "DEEPSEEK_BASE_URL", DEFAULT_ONLINE_BASE_URL
            )
            online_model = os.environ.get("ONLINE_MODEL") or os.environ.get(
                "DEEPSEEK_MODEL", DEFAULT_ONLINE_MODEL
            )
            conn.execute(
                """
                INSERT INTO settings (
                    id, deepseek_api_key, online_api_key, online_base_url, online_model,
                    local_base_url, local_model, local_api_key
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    online_key,
                    online_key,
                    online_base,
                    online_model,
                    os.environ.get("LOCAL_BASE_URL", DEFAULT_LOCAL_BASE_URL),
                    os.environ.get("LOCAL_MODEL", ""),
                    os.environ.get("LOCAL_API_KEY", ""),
                ),
            )


def get_settings() -> Settings:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return Settings(
        online_base_url=row["online_base_url"] or DEFAULT_ONLINE_BASE_URL,
        online_model=row["online_model"] or DEFAULT_ONLINE_MODEL,
        online_api_key=row["online_api_key"] or row["deepseek_api_key"],
        local_base_url=row["local_base_url"] or DEFAULT_LOCAL_BASE_URL,
        local_model=row["local_model"],
        local_api_key=row["local_api_key"],
    )


def save_settings(settings: Settings) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE settings
            SET deepseek_api_key = ?, online_api_key = ?, online_base_url = ?, online_model = ?,
                local_base_url = ?, local_model = ?, local_api_key = ?
            WHERE id = 1
            """,
            (
                settings.online_api_key,
                settings.online_api_key,
                settings.online_base_url,
                settings.online_model,
                settings.local_base_url,
                settings.local_model,
                settings.local_api_key,
            ),
        )


def _migrate_settings(conn: sqlite3.Connection) -> None:
    """Add the online endpoint columns without dropping a key saved earlier."""
    names = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "online_base_url" not in names:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN online_base_url TEXT NOT NULL DEFAULT ''"
        )
    if "online_model" not in names:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN online_model TEXT NOT NULL DEFAULT ''"
        )
    if "online_api_key" not in names:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN online_api_key TEXT NOT NULL DEFAULT ''"
        )
        if "deepseek_api_key" in names:
            conn.execute(
                "UPDATE settings SET online_api_key = deepseek_api_key WHERE online_api_key = ''"
            )


def create_batch(model_answer_text: str) -> Batch:
    created_at = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batches (created_at, model_answer_text, status)
            VALUES (?, ?, 'setup')
            """,
            (created_at, model_answer_text),
        )
        batch_id = _inserted_id(cursor)
    batch_dir(batch_id).mkdir(parents=True, exist_ok=True)
    return get_batch(batch_id)


def update_batch(batch_id: int, **fields: Any) -> None:
    assignments = _assignments(fields, _BATCH_FIELDS)
    if not assignments:
        return
    columns = ", ".join(f"{name} = ?" for name in assignments)
    with _connect() as conn:
        conn.execute(
            f"UPDATE batches SET {columns} WHERE id = ?",
            (*assignments.values(), batch_id),
        )


def get_batch(batch_id: int) -> Batch:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise KeyError(batch_id)
    return _batch_from_row(row)


def list_batches() -> list[Batch]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
    return [_batch_from_row(row) for row in rows]


def add_student(
    batch_id: int,
    *,
    canvas_user_id: str | None,
    display_name: str,
    filename_name: str,
    parsed: bool,
    matched: bool,
    gradebook_row: int | None,
    source_files: list[str],
) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO students (
                batch_id, canvas_user_id, display_name, filename_name, parsed,
                matched, gradebook_row, source_files
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                canvas_user_id,
                display_name,
                filename_name,
                int(parsed),
                int(matched),
                gradebook_row,
                json.dumps(source_files),
            ),
        )
        return _inserted_id(cursor)


def list_students(batch_id: int) -> list[Student]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM students WHERE batch_id = ? ORDER BY display_name COLLATE NOCASE, id",
            (batch_id,),
        ).fetchall()
    return [_student_from_row(row) for row in rows]


def get_student(student_id: int) -> Student:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student_id,)
        ).fetchone()
    if row is None:
        raise KeyError(student_id)
    return _student_from_row(row)


def update_student(student_id: int, **fields: Any) -> None:
    assignments = _assignments(fields, _STUDENT_FIELDS)
    if not assignments:
        return
    columns = ", ".join(f"{name} = ?" for name in assignments)
    with _connect() as conn:
        conn.execute(
            f"UPDATE students SET {columns} WHERE id = ?",
            (*assignments.values(), student_id),
        )


def delete_batch(batch_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM students WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    directory = batch_dir(batch_id)
    if directory.exists():
        shutil.rmtree(directory)


def batch_dir(batch_id: int) -> Path:
    return data_dir() / "batches" / str(batch_id)


def pages_dir(batch_id: int, student_id: int) -> Path:
    return batch_dir(batch_id) / "pages" / str(student_id)


def answer_dir(batch_id: int) -> Path:
    return batch_dir(batch_id) / "answer"


def extracted_dir(batch_id: int) -> Path:
    return batch_dir(batch_id) / "extracted"


@contextmanager
def _connect() -> Generator[sqlite3.Connection]:
    with _lock:
        connection = sqlite3.connect(data_dir() / "grader.db", timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        finally:
            connection.close()


def _inserted_id(cursor: sqlite3.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("SQLite did not return a row id")
    return row_id


def _assignments(fields: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
    converted: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, bool):
            converted[key] = int(value)
        else:
            converted[key] = value
    return converted


def _batch_from_row(row: sqlite3.Row) -> Batch:
    return Batch(
        id=row["id"],
        created_at=row["created_at"],
        assignment_column=row["assignment_column"],
        points_possible=row["points_possible"],
        model_answer_text=row["model_answer_text"],
        rubric_provider=row["rubric_provider"],
        grade_provider=row["grade_provider"],
        status=row["status"],
        error=row["error"],
        job_current=row["job_current"],
        job_total=row["job_total"],
        job_message=row["job_message"],
        rubric_json=row["rubric_json"],
    )


def _student_from_row(row: sqlite3.Row) -> Student:
    return Student(
        id=row["id"],
        batch_id=row["batch_id"],
        canvas_user_id=row["canvas_user_id"],
        display_name=row["display_name"],
        filename_name=row["filename_name"],
        parsed=bool(row["parsed"]),
        is_pilot=bool(row["is_pilot"]),
        pilot_label=row["pilot_label"],
        matched=bool(row["matched"]),
        gradebook_row=row["gradebook_row"],
        source_files=list(json.loads(row["source_files"])),
        transcript=row["transcript"],
        differences=row["differences"],
        illegible=bool(row["illegible"]),
        scores_json=row["scores_json"],
        comment=row["comment"],
        total_score=row["total_score"],
        status=row["status"],
        error=row["error"],
        truncated=bool(row["truncated"]),
        reviewed=bool(row["reviewed"]),
        text_body=row["text_body"],
        page_count=row["page_count"],
    )
