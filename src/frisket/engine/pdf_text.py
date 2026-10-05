"""Fenced native-PDF positioned text reader; no OCR or persistent raster copies."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from frisket.actions.document_extraction_types import PositionedPage
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.runtime.launch import worker_argv


class PdfTextError(RuntimeError):
    pass


class PdfTextCancelled(PdfTextError):
    pass


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
    source = source.resolve()
    result = await run_sandboxed(
        worker_argv("pdf-text"),
        policy=SandboxPolicy(
            cpu_seconds=timeout_seconds,
            wall_seconds=timeout_seconds,
            memory_mb=2048,
            confine=fence.Confinement(op="PDFium positioned text", read=(str(source),)),
        ),
        stdin_data=json.dumps(
            {"source": str(source), "max_pages": max_pages, "max_chars": max_chars}
        ).encode(),
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
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError
        pages = TypeAdapter(list[PositionedPage]).validate_python(response["pages"])
        if len(pages) > max_pages or [p.page for p in pages] != list(
            range(1, len(pages) + 1)
        ):
            raise ValueError
        if sum(len(token.text) for page in pages for token in page.tokens) > max_chars:
            raise ValueError
        return pages
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise PdfTextError("PDF text extraction failed") from exc
