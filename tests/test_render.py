"""PDF pages are split into two JPEG halves."""

from pathlib import Path

import pymupdf

from PIL import Image

from canvas_grader.render import MAX_EDGE, prepare_submission


def test_pdf_page_becomes_two_halves(tmp_path: Path) -> None:
    source = tmp_path / "hw.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=800)
    page.insert_text((72, 72), "hello")
    document.save(source)
    document.close()

    prepared = prepare_submission([source], tmp_path / "pages")
    assert prepared.source_page_count == 1
    assert len(prepared.image_paths) == 2
    assert not prepared.truncated
    assert all(path.suffix == ".jpg" and path.stat().st_size > 0 for path in prepared.image_paths)


def test_nine_page_submission_is_kept(tmp_path: Path) -> None:
    source = tmp_path / "long.pdf"
    document = pymupdf.open()
    for _ in range(9):
        document.new_page(width=200, height=200)
    document.save(source)
    document.close()
    prepared = prepare_submission([source], tmp_path / "pages")
    assert prepared.source_page_count == 9
    assert not prepared.truncated


def test_large_page_half_stays_within_the_edge_limit(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    Image.new("RGB", (2400, 3200), "white").save(source)
    prepared = prepare_submission([source], tmp_path / "pages")
    assert prepared.image_paths
    for path in prepared.image_paths:
        with Image.open(path) as image:
            assert max(image.size) <= MAX_EDGE
