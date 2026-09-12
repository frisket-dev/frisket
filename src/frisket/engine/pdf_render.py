"""Shared fenced launch interface for PDFium page rasterization."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from collections.abc import Callable, Sequence

from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.runtime.launch import worker_argv


class PdfRenderError(RuntimeError):
    pass


class PdfRenderCancelled(PdfRenderError):
    pass


@dataclass(frozen=True)
class PdfRenderResult:
    page_count: int
    pages: tuple[tuple[int, Path], ...]


def _validate(
    source: Path,
    scratch: Path,
    dpi: int,
    pages: Sequence[int] | None,
    page_limit: int | None,
) -> tuple[Path, Path, list[int] | None, int | None]:
    if type(dpi) is not int or not 50 <= dpi <= 600:
        raise ValueError("PDF dpi must be between 50 and 600")
    selected = None if pages is None else list(pages)
    if selected is not None and (
        not selected
        or any(type(page) is not int or page < 1 for page in selected)
        or len(selected) != len(set(selected))
    ):
        raise ValueError("PDF pages must be distinct positive integers")
    if page_limit is not None and (type(page_limit) is not int or page_limit < 0):
        raise ValueError("PDF page limit must be a nonnegative integer")
    if selected is not None and page_limit is not None:
        raise ValueError("PDF pages and page limit are mutually exclusive")
    return source.resolve(), scratch.resolve(), selected, page_limit


async def render_pdf_pages(
    source: Path,
    scratch: Path,
    *,
    dpi: int,
    pages: Sequence[int] | None = None,
    page_limit: int | None = None,
    timeout_seconds: int = 30,
    should_cancel: Callable[[], bool] | None = None,
) -> PdfRenderResult:
    """Render selected 1-based pages in one owned, fenced PDFium child."""
    source, scratch, selected, page_limit = _validate(
        source, scratch, dpi, pages, page_limit
    )
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise ValueError("PDF render timeout must be a positive integer")
    payload = json.dumps(
        {
            "source": str(source),
            "output": str(scratch),
            "dpi": dpi,
            "pages": selected,
            "page_limit": page_limit,
        },
        separators=(",", ":"),
    ).encode()
    result = await run_sandboxed(
        worker_argv("pdf-render"),
        policy=SandboxPolicy(
            cpu_seconds=timeout_seconds,
            wall_seconds=timeout_seconds,
            memory_mb=2048,
            confine=fence.Confinement(
                op="PDFium page rasterization",
                read=(str(source),),
                write=(str(scratch),),
            ),
        ),
        stdin_data=payload,
        scratch_dir=scratch,
        should_cancel=should_cancel,
    )
    if result.cancelled:
        raise PdfRenderCancelled("PDF rendering was cancelled")
    if not result.ok:
        raise PdfRenderError("PDF rendering failed")
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError
        page_count = response.get("page_count")
        rendered = response.get("pages")
        if (
            type(page_count) is not int
            or page_count < 0
            or not isinstance(rendered, list)
            or any(type(page) is not int or page < 1 for page in rendered)
            or len(rendered) != len(set(rendered))
            or any(page > page_count for page in rendered)
            or (selected is not None and rendered != selected)
            or (
                selected is None
                and page_limit is not None
                and len(rendered) > page_limit
            )
        ):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PdfRenderError("PDF rendering failed") from exc
    paths = tuple((page, scratch / f"page-{page}.png") for page in rendered)
    return PdfRenderResult(page_count=page_count, pages=paths)


__all__ = [
    "PdfRenderCancelled",
    "PdfRenderError",
    "PdfRenderResult",
    "render_pdf_pages",
]
