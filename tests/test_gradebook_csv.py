"""Canvas gradebook columns stay intact except the one score being updated."""

from pathlib import Path

from canvas_grader.gradebook_csv import apply_scores, load_gradebook, write_gradebook

_CSV = """Student,ID,SIS User ID,SIS Login ID,Section,HW1 (10),Current Score
Points Possible,,,,,10,
"Ada, Lovelace",111,s1,ada,A,,
"王小明",222,s2,grace,A,7,90
"""


def test_only_assignment_columns_are_offered(tmp_path: Path) -> None:
    path = _write(tmp_path)
    gradebook = load_gradebook(path)
    headers = [header for _index, header, _points in gradebook.assignment_columns()]
    assert headers == ["HW1 (10)"]
    column = gradebook.column_index("HW1 (10)")
    assert gradebook.points_possible(column) == 10


def test_update_changes_one_cell_and_reports_unknown_ids(tmp_path: Path) -> None:
    path = _write(tmp_path)
    gradebook = load_gradebook(path)
    missing = apply_scores(gradebook, "HW1 (10)", {"111": "8", "999": "1"})
    assert missing == ["999"]

    points = gradebook.rows[0]
    ada = gradebook.rows[1]
    wang = gradebook.rows[2]
    assert points[0] == "Points Possible"
    assert points[5] == "10"
    assert ada[0] == "Ada, Lovelace"
    assert ada[5] == "8"
    assert ada[6] == ""
    assert wang[0] == "王小明"
    assert wang[5] == "7"
    assert wang[6] == "90"

    write_gradebook(gradebook, tmp_path / "out.csv")
    reloaded = load_gradebook(tmp_path / "out.csv")
    assert reloaded.rows[2][0] == "王小明"
    assert reloaded.rows[2][5] == "7"
    assert reloaded.rows[1][5] == "8"


def test_name_match_ignores_punctuation(tmp_path: Path) -> None:
    gradebook = load_gradebook(_write(tmp_path))
    assert gradebook.find_student_row(None, "Ada Lovelace") == 1
    assert gradebook.find_student_row("222", "someone else") == 2
    assert gradebook.find_student_row("999", "nobody") is None
    assert gradebook.find_student_row("999", "Ada Lovelace") is None
    assert gradebook.find_student_row("111.0", "someone else") == 1


def _write(tmp_path: Path) -> Path:
    path = tmp_path / "grades.csv"
    path.write_text(_CSV, encoding="utf-8")
    return path
