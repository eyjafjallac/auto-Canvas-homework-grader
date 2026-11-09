"""Parse Canvas submission zip filenames and group files by student."""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# name_id_1{sid}.pdf — the name is lowercase letters, and the id matches the gradebook ID column.
_LOWER_NAME_ID_SID = re.compile(
    r"^(?P<name>[a-z]+(?:_[a-z]+)*)_(?P<user_id>\d+)_1(?P<sid>\d+)$"
)


@dataclass(frozen=True)
class ParsedName:
    student_name: str
    user_id: str | None
    file_id: str | None
    late: bool
    parsed: bool
    filename: str


@dataclass
class StudentGroup:
    key: str
    user_id: str | None
    display_name: str
    parsed: bool
    files: list[Path] = field(default_factory=list)
    file_ids: list[str] = field(default_factory=list)


def parse_submission_name(archive_name: str) -> ParsedName:
    """Pull the Canvas user id out of a downloaded submission filename.

    This course's zip uses ``name_id_1{sid}.pdf``. The name is lowercase
    letters. The id is what the gradebook ID column uses. ``1{sid}`` is only
    the file token.

    Other Canvas downloads look like ``LastFirst_userId_fileId_original.ext``,
    with an optional ``LATE`` token before the user id. Names that do not
    match stay unparsed so they can be matched by hand later.
    """
    filename = PurePosixPath(archive_name.replace("\\", "/")).name
    stem = Path(filename).stem
    lower = _LOWER_NAME_ID_SID.match(stem)
    if lower is not None:
        return ParsedName(
            student_name=_clean_name(lower.group("name").split("_")),
            user_id=lower.group("user_id"),
            file_id=f"1{lower.group('sid')}",
            late=False,
            parsed=True,
            filename=filename,
        )
    parts = stem.split("_") if stem else []
    pair = _best_consecutive_ids(parts)
    if pair is not None:
        user_index, file_index = pair
        name_parts = parts[:user_index]
        late = bool(name_parts) and name_parts[-1].upper() == "LATE"
        if late:
            name_parts = name_parts[:-1]
        return ParsedName(
            student_name=_clean_name(name_parts) or stem or filename,
            user_id=parts[user_index],
            file_id=parts[file_index],
            late=late,
            parsed=True,
            filename=filename,
        )

    single = _best_single_id(parts)
    if single is None:
        return ParsedName(
            student_name=stem or filename,
            user_id=None,
            file_id=None,
            late=False,
            parsed=False,
            filename=filename,
        )
    name_parts = parts[:single]
    late = bool(name_parts) and name_parts[-1].upper() == "LATE"
    if late:
        name_parts = name_parts[:-1]
    return ParsedName(
        student_name=_clean_name(name_parts) or stem or filename,
        user_id=parts[single],
        file_id=None,
        late=late,
        parsed=True,
        filename=filename,
    )


def extract_submission_zip(zip_path: Path, dest: Path) -> list[StudentGroup]:
    """Extract a Canvas zip and group files that belong to the same student."""
    dest.mkdir(parents=True, exist_ok=True)
    groups: dict[str, StudentGroup] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or _skip_member(info.filename):
                continue
            target = _safe_member_path(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            parsed = parse_submission_name(info.filename)
            key = (
                parsed.user_id
                if parsed.parsed and parsed.user_id
                else f"unparsed:{parsed.filename}"
            )
            group = groups.get(key)
            if group is None:
                group = StudentGroup(
                    key=key,
                    user_id=parsed.user_id if parsed.parsed else None,
                    display_name=parsed.student_name,
                    parsed=parsed.parsed and parsed.user_id is not None,
                )
                groups[key] = group
            group.files.append(target)
            group.file_ids.append(parsed.file_id or "")
    ordered = list(groups.values())
    for group in ordered:
        paired = sorted(
            zip(group.file_ids, group.files, strict=True), key=_file_sort_key
        )
        group.file_ids = [item[0] for item in paired]
        group.files = [item[1] for item in paired]
    ordered.sort(key=lambda group: group.display_name.casefold())
    return ordered


def _best_consecutive_ids(parts: list[str]) -> tuple[int, int] | None:
    candidates: list[tuple[tuple[bool, int, int], int, int]] = []
    for index in range(len(parts) - 1):
        left, right = parts[index], parts[index + 1]
        if not left.isdigit() or not right.isdigit():
            continue
        score = (
            len(left) >= 4 and len(right) >= 4,
            len(left) + len(right),
            -index,
        )
        candidates.append((score, index, index + 1))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def _best_single_id(parts: list[str]) -> int | None:
    numeric = [index for index, part in enumerate(parts) if part.isdigit()]
    long_ids = [index for index in numeric if len(parts[index]) >= 4]
    pool = long_ids or numeric
    if not pool:
        return None
    return max(pool, key=lambda index: (len(parts[index]), -index))


def _clean_name(parts: list[str]) -> str:
    return " ".join(part for part in parts if part).strip()


def _file_sort_key(item: tuple[str, Path]) -> tuple[int, int | str, str]:
    file_id, path = item
    if file_id.isdigit():
        return (0, int(file_id), path.name.casefold())
    return (1, file_id, path.name.casefold())


def _skip_member(name: str) -> bool:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts:
        return True
    if any(part == "__MACOSX" or part.startswith(".") for part in parts):
        return True
    return Path(parts[-1]).name.startswith(".")


def _safe_member_path(dest: Path, name: str) -> Path:
    relative = PurePosixPath(name.replace("\\", "/"))
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ValueError(f"Unsafe path in zip: {name}")
    target = (dest / Path(*relative.parts)).resolve()
    root = dest.resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Unsafe path in zip: {name}")
    return target
