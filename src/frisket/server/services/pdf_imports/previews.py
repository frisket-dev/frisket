"""Bounded native-page, thumbnail and OCR-preview preparation helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from frisket.engine.pdf_render import render_pdf_pages
from frisket.engine.pdf_text import (
    PdfTextError,
    extract_pdf_text_batch,
    positioned_tokens_text,
    sample_pdf_text,
)

from .analysis import MAX_SAMPLE_CHARS


class ThumbnailLease:
    def __init__(self, path: Path) -> None:
        self.path = path

    def close(self) -> None:
        self.path.unlink(missing_ok=True)


def preview_settings(action_params: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    settings = {
        **action_params,
        "dpi": 200,
        "language": None,
        "keep_text_position": True,
    }
    digest = hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return settings, digest


def read_native_page(path: Path, *, page: int) -> tuple[str, bool]:
    try:
        batch = asyncio.run(
            extract_pdf_text_batch(
                path,
                start_page=page,
                max_pages=1,
                max_chars=MAX_SAMPLE_CHARS,
                timeout_seconds=30,
            )
        )
        return positioned_tokens_text(batch.pages[0].tokens), False
    except PdfTextError:
        try:
            sample = asyncio.run(
                sample_pdf_text(
                    path,
                    page=page,
                    max_chars=MAX_SAMPLE_CHARS,
                    timeout_seconds=30,
                )
            )
            return sample.text, sample.truncated
        except PdfTextError:
            return "", False


def thumbnail(
    directory: Path,
    *,
    source: Path,
    occurrence_id: str,
    page: int,
) -> ThumbnailLease:
    cache_key = hashlib.sha256(occurrence_id.encode()).hexdigest()
    cached = directory / "cache" / f"{cache_key}-{page}.png"
    if not cached.is_file():
        rendered = asyncio.run(
            render_pdf_pages(
                source,
                directory / "cache",
                pages=[page],
                dpi=96,
                max_edge=1200,
                timeout_seconds=30,
            )
        )
        os.replace(rendered.pages[0][1], cached)
    descriptor, raw = tempfile.mkstemp(
        prefix="frisket-pdf-import-thumb-", suffix=".png"
    )
    os.close(descriptor)
    target = Path(raw)
    shutil.copyfile(cached, target)
    return ThumbnailLease(target)


def prepare_ocr(
    preview_runs: Any,
    *,
    project_id: str,
    source: Path,
    item: Mapping[str, Any],
    occurrence_id: str,
    page: int,
    action_params: Mapping[str, Any],
    request_context: Any,
    on_page: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    settings, settings_hash = preview_settings(action_params)
    plan, _source, router, composition, execution_context = (
        preview_runs.prepare_ocr_scratch_path(
            project_id,
            source,
            media_digest=str(item["sha256"]),
            media_size=int(item["size"]),
            payload={
                **settings,
                "pages": [page],
                "filename": item["filename"],
                "mime": "application/pdf",
            },
            on_page=on_page,
            max_pages=1,
            request_context=request_context,
        )
    )
    return {
        "plan": plan,
        "router": router,
        "composition": composition,
        "execution_context": execution_context,
        "settings_hash": settings_hash,
    }


__all__ = [
    "ThumbnailLease",
    "prepare_ocr",
    "preview_settings",
    "read_native_page",
    "thumbnail",
]
