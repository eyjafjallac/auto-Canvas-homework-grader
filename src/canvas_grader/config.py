"""Paths and the default model endpoints."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ONLINE_BASE_URL = "https://api.deepseek.com"
DEFAULT_ONLINE_MODEL = "deepseek-flash"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:1234/v1"

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def load_dotenv() -> None:
    """Load `.env` from the working directory without overriding real env vars."""
    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def data_dir() -> Path:
    override = os.environ.get("CANVAS_GRADER_DATA", "").strip()
    root = Path(override) if override else Path.cwd() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root
