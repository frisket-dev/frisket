"""Download response helpers."""

from __future__ import annotations

import re
from pathlib import Path


def download_filename(name: str, suffix: str) -> str:
    """Conservative attachment filename safe for Content-Disposition."""
    if re.search(r"[^A-Za-z0-9._-]+", suffix):
        raise ValueError("download filename suffix must be ASCII-safe")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip(".-")
    return f"{stem or 'frisket'}{suffix}"


def unlink_best_effort(path: str | Path) -> None:
    """Best-effort cleanup of a temporary download artifact."""
    Path(path).unlink(missing_ok=True)
