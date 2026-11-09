"""Read a Canvas gradebook export and write scores back into one column."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

# Canvas ignores these headers on import and rejects some of them as new columns.
_EXACT_RESERVED = {
    "student",
    "id",
    "sis user id",
    "sis login id",
    "section",
    "integration id",
    "root account",
    "points possible",
}
_CONTAINS_RESERVED = (
    "current score",
    "current points",
    "current grade",
    "final score",
    "final points",
    "final grade",
    "override score",
    "override grade",
    "override status",
)


@dataclass
class Gradebook:
    headers: list[str]
    rows: list[list[str]]
    points_row_index: int | None
    student_column: int
    id_column: int

    def assignment_columns(self) -> list[tuple[int, str, str | None]]:
        """Return ``(index, header, points possible)`` for columns a TA can grade."""
        found: list[tuple[int, str, str | None]] = []
        for index, header in enumerate(self.headers):
            if not _is_assignment_header(header):
                continue
            points = self.points_possible(index)
            found.append((index, header, None if points is None else _format_points(points)))
        return found

    def points_possible(self, column: int) -> float | None:
        if self.points_row_index is None:
            return None
        row = self.rows[self.points_row_index]
        if column >= len(row):
            return None
        return _parse_points(row[column])

    def column_index(self, header: str) -> int:
        try:
            return self.headers.index(header)
        except ValueError as exc:
            raise ValueError(f"Gradebook has no column named {header!r}.") from exc

    def find_student_row(self, user_id: str | None, name: str) -> int | None:
        """Match a parsed file id only against the ID column.

        The zip name is lowercase and is not a reliable key when two students
        share a surname. Name matching is only for a file that had no id.
        """
        if user_id:
            wanted = _normalize_id(user_id)
            if not wanted:
                return None
            for index, row in enumerate(self.rows):
                if index == self.points_row_index:
                    continue
                if _normalize_id(_cell(row, self.id_column)) == wanted:
                    return index
            return None
        target = _normalize_name(name)
        if not target:
            return None
        hits = [
            index
            for index, row in enumerate(self.rows)
            if index != self.points_row_index and _normalize_name(_cell(row, self.student_column)) == target
        ]
        if len(hits) == 1:
            return hits[0]
        return None

    def student_name(self, row_index: int) -> str:
        return _cell(self.rows[row_index], self.student_column)

    def student_id(self, row_index: int) -> str:
        return _cell(self.rows[row_index], self.id_column)


def load_gradebook(path: Path) -> Gradebook:
    text = _decode(path.read_bytes())
    reader = csv.reader(io.StringIO(text))
    table = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not table:
        raise ValueError("The gradebook CSV is empty.")
    headers = [cell.strip() for cell in table[0]]
    rows = [list(row) for row in table[1:]]
    student_column = _find_header(headers, "student")
    id_column = _find_header(headers, "id")
    if student_column is None or id_column is None:
        raise ValueError("The gradebook CSV needs Student and ID columns.")
    points_row_index = _find_points_row(rows)
    return Gradebook(
        headers=headers,
        rows=rows,
        points_row_index=points_row_index,
        student_column=student_column,
        id_column=id_column,
    )


def apply_scores(gradebook: Gradebook, column: str, scores_by_user_id: dict[str, str]) -> list[str]:
    """Write scores for known student IDs. Other cells stay as they were.

    Returns user ids that were not present in the gradebook.
    """
    column_index = gradebook.column_index(column)
    found: set[str] = set()
    for index, row in enumerate(gradebook.rows):
        if index == gradebook.points_row_index:
            continue
        user_id = _cell(row, gradebook.id_column)
        if user_id not in scores_by_user_id:
            continue
        while len(row) <= column_index:
            row.append("")
        row[column_index] = scores_by_user_id[user_id]
        found.add(user_id)
    return [user_id for user_id in scores_by_user_id if user_id not in found]


def gradebook_csv_text(gradebook: Gradebook) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(gradebook.headers)
    writer.writerows(gradebook.rows)
    return buffer.getvalue()


def write_gradebook(gradebook: Gradebook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(gradebook.headers)
        writer.writerows(gradebook.rows)


def format_points(value: float) -> str:
    return _format_points(value)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _find_header(headers: list[str], expected: str) -> int | None:
    for index, header in enumerate(headers):
        if header.strip().casefold() == expected:
            return index
    return None


def _find_points_row(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows[:2]):
        if any(cell.strip().casefold() == "points possible" for cell in row):
            return index
    return None


def _is_assignment_header(header: str) -> bool:
    name = header.strip().casefold()
    if not name or name in _EXACT_RESERVED:
        return False
    return not any(token in name for token in _CONTAINS_RESERVED)


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return row[index].strip()


def _normalize_name(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _normalize_id(value: str) -> str:
    text = value.strip()
    if len(text) > 2 and text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _parse_points(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_points(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")
