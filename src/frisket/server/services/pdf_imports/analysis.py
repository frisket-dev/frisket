"""Bounded representative native-text analysis for one staged PDF."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from frisket.engine.pdf_text import (
    PdfTextError,
    extract_pdf_text_batch,
    positioned_tokens_text,
    sample_pdf_text,
)
from frisket.engine.pdf_render import render_pdf_pages


MAX_SAMPLE_PAGES = 6
MAX_SAMPLE_CHARS = 12_000
MAX_PAGE_POSITIONED_WORDS = 5_000
MAX_ERROR_CHARS = 500


def representative_pages(page_count: int) -> list[int]:
    count = min(MAX_SAMPLE_PAGES, page_count)
    if count == 1:
        return [1]
    return sorted(
        {1 + round(index * (page_count - 1) / (count - 1)) for index in range(count)}
    )


def analyze_pdf(
    path: Path,
    *,
    quality_assessor: Callable[..., Sequence[Any]] | None = None,
) -> dict[str, Any]:
    try:
        pages: dict[int, Any] = {}
        page_text: dict[int, str] = {}
        try:
            first = asyncio.run(
                extract_pdf_text_batch(
                    path,
                    start_page=1,
                    max_pages=1,
                    max_chars=MAX_SAMPLE_CHARS,
                    timeout_seconds=30,
                )
            )
            page_count = first.page_count
            pages[1] = first.pages[0]
            page_text[1] = positioned_tokens_text(first.pages[0].tokens)
        except PdfTextError:
            first_sample = asyncio.run(
                sample_pdf_text(
                    path,
                    page=1,
                    max_chars=MAX_SAMPLE_CHARS,
                    timeout_seconds=30,
                )
            )
            page_count = first_sample.page_count
            page_text[1] = first_sample.text
        sample_pages = representative_pages(page_count)
        remaining = max(0, MAX_SAMPLE_CHARS - len(page_text[1]))
        for page_number in sample_pages:
            if page_number == 1 or remaining <= 0:
                continue
            try:
                batch = asyncio.run(
                    extract_pdf_text_batch(
                        path,
                        start_page=page_number,
                        max_pages=1,
                        max_chars=remaining,
                        timeout_seconds=30,
                    )
                )
                pages[page_number] = batch.pages[0]
                page_text[page_number] = positioned_tokens_text(
                    batch.pages[0].tokens
                )
            except PdfTextError:
                sample = asyncio.run(
                    sample_pdf_text(
                        path,
                        page=page_number,
                        max_chars=remaining,
                        timeout_seconds=30,
                    )
                )
                page_text[page_number] = sample.text
            remaining -= len(page_text[page_number])
        loaded_pages = [page for page in sample_pages if page in pages]
        text = "\n".join(
            page_text[page] for page in sample_pages if page in page_text
        )[:MAX_SAMPLE_CHARS]
        category = "missing" if not text.strip() else "text"
        if text.strip() and _quality_flags(
            path, text, loaded_pages, pages, quality_assessor
        ):
            category = "suspicious"
        return {
            "analysis_state": "done",
            "page_count": page_count,
            "category": category,
            "sample_pages": sample_pages,
            "error": None,
        }
    except Exception as exc:
        return {
            "analysis_state": "error",
            "page_count": None,
            "category": "unreadable",
            "sample_pages": [],
            "error": (str(exc) or "PDF could not be read")[:MAX_ERROR_CHARS],
        }


def _quality_flags(
    path: Path,
    text: str,
    sample_pages: Sequence[int],
    pages: dict[int, Any],
    assessor: Callable[..., Sequence[Any]] | None,
) -> bool:
    if assessor is None:
        try:
            from frisket.documents.text_quality import assess_text_quality

            assessor = assess_text_quality
        except ImportError:
            return False
    flags = list(assessor(text))
    # Page signals are calibrated only for word boxes paired with a thumbnail
    # from the same physical page. Other granularities deliberately abstain.
    eligible = [
        page
        for page in sample_pages
        if len(pages[page].tokens) <= MAX_PAGE_POSITIONED_WORDS
        and all(token.granularity == "word" for token in pages[page].tokens)
    ]
    if eligible:
        try:
            import cv2

            with tempfile.TemporaryDirectory(prefix="frisket-pdf-quality-") as raw:
                rendered = asyncio.run(
                    render_pdf_pages(
                        path,
                        Path(raw),
                        pages=eligible,
                        dpi=96,
                        max_edge=1400,
                        timeout_seconds=max(30, 5 * len(eligible)),
                    )
                )
                for page_number, image_path in rendered.pages:
                    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                    if image is None:
                        continue
                    page = pages[page_number]
                    flags.extend(
                        assessor(
                            positioned_tokens_text(page.tokens),
                            grayscale_thumbnail=image,
                            positioned_tokens=page.tokens,
                        )
                    )
        except Exception:
            # Rendering/page-signal availability must not turn readable native
            # text into an unreadable file. Text-only flags remain valid.
            pass
    names = {str(getattr(flag, "name", flag)).upper() for flag in flags}
    return bool(names - {"EMPTY"})


__all__ = [
    "MAX_PAGE_POSITIONED_WORDS",
    "MAX_SAMPLE_CHARS",
    "analyze_pdf",
    "representative_pages",
]
