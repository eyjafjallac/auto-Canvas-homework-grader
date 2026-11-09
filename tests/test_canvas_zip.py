"""Filename parsing for Canvas submission downloads."""

import zipfile
from pathlib import Path

from canvas_grader.canvas_zip import extract_submission_zip, parse_submission_name


def test_parse_late_token() -> None:
    parsed = parse_submission_name("DoeJohn_LATE_12345_67890_hw.pdf")
    assert parsed.parsed
    assert parsed.late
    assert parsed.user_id == "12345"
    assert parsed.file_id == "67890"
    assert parsed.student_name == "DoeJohn"


def test_parse_underscored_name() -> None:
    parsed = parse_submission_name("doe_john_12345_67890_hw.pdf")
    assert parsed.user_id == "12345"
    assert parsed.file_id == "67890"
    assert parsed.student_name == "doe john"
    assert not parsed.late


def test_parse_prefers_the_long_id_pair() -> None:
    parsed = parse_submission_name("Assignment_1_12345678_99999_sol.pdf")
    assert parsed.user_id == "12345678"
    assert parsed.file_id == "99999"


def test_parse_single_user_id() -> None:
    parsed = parse_submission_name("smith_12345678_essay.pdf")
    assert parsed.parsed
    assert parsed.user_id == "12345678"
    assert parsed.file_id is None
    assert parsed.student_name == "smith"


def test_parse_lowercase_name_id_and_sid() -> None:
    parsed = parse_submission_name("baraka_161437_1123456.pdf")
    assert parsed.parsed
    assert parsed.user_id == "161437"
    assert parsed.file_id == "1123456"
    assert parsed.student_name == "baraka"


def test_parse_lowercase_name_with_underscores() -> None:
    parsed = parse_submission_name("submissions/chan_chi_167088_10001.pdf")
    assert parsed.user_id == "167088"
    assert parsed.file_id == "10001"
    assert parsed.student_name == "chan chi"


def test_parse_leaves_unknown_names_unparsed() -> None:
    parsed = parse_submission_name("notes.pdf")
    assert not parsed.parsed
    assert parsed.user_id is None


def test_zip_groups_files_and_skips_macosx(tmp_path: Path) -> None:
    archive_path = tmp_path / "submissions.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("DoeJohn_LATE_12345_67890_hw.pdf", b"one")
        archive.writestr("DoeJohn_12345_67891_hw2.pdf", b"two")
        archive.writestr("notes.pdf", b"loose")
        archive.writestr("__MACOSX/._junk", b"skip")
    groups = extract_submission_zip(archive_path, tmp_path / "out")
    by_key = {group.key: group for group in groups}
    assert set(by_key) == {"12345", "unparsed:notes.pdf"}
    student = by_key["12345"]
    assert student.user_id == "12345"
    assert [path.name for path in student.files] == [
        "DoeJohn_LATE_12345_67890_hw.pdf",
        "DoeJohn_12345_67891_hw2.pdf",
    ]
    assert student.file_ids == ["67890", "67891"]
    assert by_key["unparsed:notes.pdf"].parsed is False
