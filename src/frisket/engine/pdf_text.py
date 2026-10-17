"""Fenced native-PDF positioned text reader; no OCR or persistent raster copies."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from frisket.actions.document_extraction_types import PositionedPage, PositionedToken
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.runtime.launch import worker_argv


class PdfTextError(RuntimeError):
    pass


class PdfTextCancelled(PdfTextError):
    pass


@dataclass(frozen=True)
class PdfTextBatch:
    pages: list[PositionedPage]
    page_count: int
    next_page: int | None


@dataclass(frozen=True)
class PdfTextSample:
    text: str
    page: int
    page_count: int
    truncated: bool


def positioned_tokens_text(tokens: Iterable[PositionedToken]) -> str:
    """Assemble positioned tokens into text while retaining inferred lines."""

    parts: list[str] = []
    previous: PositionedToken | None = None
    for token in tokens:
        if not token.text.strip():
            continue
        if previous is not None:
            same_line = (
                abs(token.box.y0 - previous.box.y0)
                < max(
                    token.box.y1 - token.box.y0,
                    previous.box.y1 - previous.box.y0,
                )
                * 0.6
            )
            parts.append(" " if same_line else "\n")
        parts.append(token.text)
        previous = token
    return "".join(parts).strip()


async def extract_pdf_text(
    source: Path,
    *,
    timeout_seconds: int = 60,
    should_cancel: Callable[[], bool] | None = None,
    max_pages: int = 2000,
    max_chars: int = 1_000_000,
) -> list[PositionedPage]:
    """Read every page, preserving blanks; reject limits rather than truncate."""
    for name, value in (
        ("timeout_seconds", timeout_seconds),
        ("max_pages", max_pages),
        ("max_chars", max_chars),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    response = await _read_pdf_text(
        source,
        {"max_pages": max_pages, "max_chars": max_chars},
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
    )
    return _positioned_pages(
        response, start_page=1, max_pages=max_pages, max_chars=max_chars
    )


def _positioned_pages(response, *, start_page, max_pages, max_chars):
    try:
        pages = TypeAdapter(list[PositionedPage]).validate_python(response["pages"])
        if len(pages) > max_pages or [p.page for p in pages] != list(
            range(start_page, start_page + len(pages))
        ):
            raise ValueError
        if sum(len(token.text) for page in pages for token in page.tokens) > max_chars:
            raise ValueError
        return pages
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise PdfTextError("PDF text extraction failed") from exc


async def _read_pdf_text(source, request, *, timeout_seconds, should_cancel):
    source = source.resolve()
    result = await run_sandboxed(
        worker_argv("pdf-text"),
        policy=SandboxPolicy(
            cpu_seconds=timeout_seconds,
            wall_seconds=timeout_seconds,
            memory_mb=2048,
            confine=fence.Confinement(op="PDFium positioned text", read=(str(source),)),
        ),
        stdin_data=json.dumps({"source": str(source), **request}).encode(),
        should_cancel=should_cancel,
    )
    if result.cancelled:
        raise PdfTextCancelled("PDF text extraction was cancelled")
    if not result.ok:
        raise PdfTextError("PDF text extraction failed")
    try:
        response = json.loads(result.stdout)
        if (
            isinstance(response, dict)
            and response.get("error") == "document_limit_exceeded"
        ):
            raise PdfTextError(
                "PDF exceeds the positioned-text page or character limit"
            )
        if (
            isinstance(response, dict)
            and response.get("error") == "page_limit_exceeded"
        ):
            raise PdfTextError("PDF page exceeds the positioned-text character limit")
        if isinstance(response, dict) and response.get("error") == "page_out_of_range":
            raise PdfTextError("PDF page is outside the document")
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError
        return response
    except (TypeError, ValueError) as exc:
        raise PdfTextError("PDF text extraction failed") from exc


async def extract_pdf_text_batch(
    source: Path,
    *,
    start_page: int = 1,
    max_pages: int = 64,
    max_chars: int = 1_000_000,
    timeout_seconds: int = 60,
    should_cancel: Callable[[], bool] | None = None,
) -> PdfTextBatch:
    """Read complete physical pages within one bounded worker response."""
    for name, value in (
        ("start_page", start_page),
        ("max_pages", max_pages),
        ("max_chars", max_chars),
        ("timeout_seconds", timeout_seconds),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    response = await _read_pdf_text(
        source,
        {
            "mode": "batch",
            "start_page": start_page,
            "max_pages": max_pages,
            "max_chars": max_chars,
        },
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
    )
    pages = _positioned_pages(
        response, start_page=start_page, max_pages=max_pages, max_chars=max_chars
    )
    page_count = response.get("page_count")
    next_page = response.get("next_page")
    if (
        type(page_count) is not int
        or page_count < start_page
        or not pages
        or pages[-1].page > page_count
        or (next_page is not None and type(next_page) is not int)
        or next_page != (pages[-1].page + 1 if pages[-1].page < page_count else None)
    ):
        raise PdfTextError("PDF text extraction failed")
    return PdfTextBatch(pages, page_count, next_page)


async def sample_pdf_text(
    source: Path,
    *,
    page: int,
    max_chars: int = 12_000,
    timeout_seconds: int = 60,
    should_cancel: Callable[[], bool] | None = None,
) -> PdfTextSample:
    """Inspect one bounded plain-text prefix, without positioned glyph boxes."""
    for name, value in (
        ("page", page),
        ("max_chars", max_chars),
        ("timeout_seconds", timeout_seconds),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    response = await _read_pdf_text(
        source,
        {"mode": "sample", "page": page, "max_chars": max_chars},
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
    )
    try:
        text, page_count, truncated = (
            response["text"],
            response["page_count"],
            response["truncated"],
        )
        if (
            not isinstance(text, str)
            or len(text) > max_chars
            or type(page_count) is not int
            or page_count < page
            or response.get("page") != page
            or type(truncated) is not bool
        ):
            raise ValueError
        return PdfTextSample(text, page, page_count, truncated)
    except (KeyError, TypeError, ValueError) as exc:
        raise PdfTextError("PDF text extraction failed") from exc
