"""Turn submission files into JPEG page halves a vision model can read."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pymupdf
from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PDF_EXTENSIONS = {".pdf"}
TEXT_EXTENSIONS = {".html", ".htm", ".txt"}
MAX_SOURCE_PAGES = 12
RENDER_DPI = 140
JPEG_QUALITY = 82
# Long edge of each page half. A local 32k vision context cannot hold a full
# answer plus several large scans, so pages are kept near this size.
MAX_EDGE = 1100
OVERLAP_RATIO = 0.12


@dataclass
class PreparedPages:
    image_paths: list[Path] = field(default_factory=list)
    text: str = ""
    truncated: bool = False
    unsupported: list[str] = field(default_factory=list)
    source_page_count: int = 0


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped)


def prepare_submission(source_files: list[Path], dest: Path) -> PreparedPages:
    """Render up to eight source pages, each as an upper and a lower JPEG."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jpg"):
        old.unlink()
    prepared = PreparedPages()
    images: list[Image.Image] = []
    texts: list[str] = []
    for path in source_files:
        suffix = path.suffix.casefold()
        if suffix in TEXT_EXTENSIONS:
            texts.append(_read_text(path))
            continue
        if suffix not in PDF_EXTENSIONS and suffix not in IMAGE_EXTENSIONS:
            prepared.unsupported.append(path.name)
            continue
        incoming = _load_source_images(path)
        remaining = MAX_SOURCE_PAGES - len(images)
        if remaining <= 0:
            prepared.truncated = True
            break
        if len(incoming) > remaining:
            images.extend(incoming[:remaining])
            prepared.truncated = True
            break
        images.extend(incoming)
    prepared.source_page_count = len(images)
    halves: list[Image.Image] = []
    for image in images:
        halves.extend(_split_halves(image))
    for index, image in enumerate(halves):
        target = dest / f"{index:03d}.jpg"
        _save_jpeg(image, target)
        prepared.image_paths.append(target)
    prepared.text = "\n\n".join(part for part in texts if part.strip())
    return prepared


def pages_exceed_limit(directory: Path) -> bool:
    """True when a cached JPEG is larger than the current page-half limit."""
    if not directory.is_dir():
        return False
    for path in directory.glob("*.jpg"):
        with Image.open(path) as image:
            if max(image.size) > MAX_EDGE:
                return True
    return False


def prepare_answer_files(source_files: list[Path], dest: Path) -> list[Path]:
    """Render a model-answer PDF or image without the eight-page submission cap."""
    if not source_files:
        return []
    prepared = prepare_submission(source_files, dest)
    if not prepared.truncated:
        return prepared.image_paths
    # The model answer is the instructor's own file, so keep every page.
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jpg"):
        old.unlink()
    images: list[Image.Image] = []
    for path in source_files:
        suffix = path.suffix.casefold()
        if suffix in PDF_EXTENSIONS or suffix in IMAGE_EXTENSIONS:
            images.extend(_load_source_images(path))
    saved: list[Path] = []
    index = 0
    for image in images:
        for half in _split_halves(image):
            target = dest / f"{index:03d}.jpg"
            _save_jpeg(half, target)
            saved.append(target)
            index += 1
    return saved


def _load_source_images(path: Path) -> list[Image.Image]:
    suffix = path.suffix.casefold()
    if suffix in PDF_EXTENSIONS:
        document = pymupdf.open(path)
        try:
            return [_pixmap_image(page) for page in document]
        finally:
            document.close()
    with Image.open(path) as image:
        frame = _to_rgb(image)
        frame.load()
        return [frame.copy()]


def _pixmap_image(page: pymupdf.Page) -> Image.Image:
    pixmap = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _split_halves(image: Image.Image) -> list[Image.Image]:
    rgb = _to_rgb(image)
    width, height = rgb.size
    if height < 400:
        return [rgb]
    overlap = int(height * OVERLAP_RATIO)
    midpoint = height // 2
    top = rgb.crop((0, 0, width, min(height, midpoint + overlap)))
    bottom = rgb.crop((0, max(0, midpoint - overlap), width, height))
    return [top, bottom]


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        return background
    return image.convert("RGB")


def _save_jpeg(image: Image.Image, path: Path) -> None:
    fitted = _limit_edge(_to_rgb(image))
    fitted.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)


def _limit_edge(image: Image.Image) -> Image.Image:
    width, height = image.size
    edge = max(width, height)
    if edge <= MAX_EDGE:
        return image
    scale = MAX_EDGE / edge
    resized = image.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.LANCZOS,
    )
    return resized


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if path.suffix.casefold() in {".html", ".htm"}:
        parser = _TextExtractor()
        parser.feed(text)
        return "\n".join(parser.parts)
    return text.strip()


def tiny_test_jpeg() -> bytes:
    """A generated picture used only to see whether a local server accepts images."""
    image = Image.new("RGB", (64, 64), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()
