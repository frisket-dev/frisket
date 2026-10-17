"""One-shot fenced PDFium raster worker.

The request is a small JSON object on stdin.  PDFium and its Pillow adapter
are imported only after the runtime bootstrap has installed the converter
fence, so malformed PDFs never load in a server process.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
import sys
from typing import Any


def is_environment_failure(error: BaseException) -> bool:
    """Keep disk/resource failures distinct from one unreadable page."""
    return isinstance(error, MemoryError) or (
        isinstance(error, OSError)
        and error.errno
        in {
            errno.ENOSPC,
            getattr(errno, "EDQUOT", -1),
            errno.ENOMEM,
            errno.EMFILE,
            errno.ENFILE,
            errno.EROFS,
            errno.EIO,
        }
    )


def _reply(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":"), allow_nan=False))


def _request() -> tuple[Path, Path, int, list[int] | None, int | None, int | None]:
    try:
        value = json.load(sys.stdin)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_request") from exc
    if not isinstance(value, dict) or set(value) != {
        "source",
        "output",
        "dpi",
        "pages",
        "page_limit",
        "max_edge",
    }:
        raise ValueError("invalid_request")
    source, output, dpi, pages, page_limit = (
        value["source"],
        value["output"],
        value["dpi"],
        value["pages"],
        value["page_limit"],
    )
    if (
        not isinstance(source, str)
        or not isinstance(output, str)
        or not Path(source).is_absolute()
        or not Path(output).is_absolute()
        or type(dpi) is not int
        or not 50 <= dpi <= 600
        or (pages is not None and not isinstance(pages, list))
        or (
            isinstance(pages, list)
            and (not pages or any(type(page) is not int or page < 1 for page in pages))
        )
        or (isinstance(pages, list) and len(pages) != len(set(pages)))
        or (page_limit is not None and (type(page_limit) is not int or page_limit < 0))
        or (pages is not None and page_limit is not None)
        or (
            value["max_edge"] is not None
            and (type(value["max_edge"]) is not int or value["max_edge"] < 1)
        )
    ):
        raise ValueError("invalid_request")
    return Path(source), Path(output), dpi, pages, page_limit, value["max_edge"]


def _close(value: Any) -> None:
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()


def render_page(
    document: Any,
    number: int,
    output: Path,
    *,
    dpi: int,
    max_edge: int | None = None,
) -> tuple[int, int]:
    """Render one physical page and release its bitmap before returning."""
    page = document[number - 1]
    bitmap = image = None
    try:
        # OCR coordinates refer to the full MediaBox, including cropped pages.
        page.set_cropbox(*page.get_mediabox())
        scale = dpi / 72
        if max_edge is not None:
            scale = min(scale, max_edge / max(page.get_size()))
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        image.save(output, format="PNG")
        return image.size
    finally:
        _close(image)
        _close(bitmap)
        _close(page)


def main() -> None:
    try:
        source, output, dpi, pages, page_limit, max_edge = _request()
    except ValueError as exc:
        _reply({"ok": False, "error": str(exc)})
        return
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(source))
    except Exception as exc:
        _reply(
            {
                "ok": False,
                "error": "environment_failed"
                if is_environment_failure(exc)
                else "invalid_pdf",
            }
        )
        return
    try:
        page_count = len(document)
        if pages is None:
            limit = page_count if page_limit is None else min(page_count, page_limit)
            pages = list(range(1, limit + 1))
        if any(page > page_count for page in pages):
            _reply(
                {"ok": False, "error": "page_out_of_range", "page_count": page_count}
            )
            return
        rendered: list[int] = []
        for number in pages:
            render_page(
                document,
                number,
                output / f"page-{number}.png",
                dpi=dpi,
                max_edge=max_edge,
            )
            rendered.append(number)
        _reply({"ok": True, "page_count": page_count, "pages": rendered})
    except Exception as exc:
        _reply(
            {
                "ok": False,
                "error": "environment_failed"
                if is_environment_failure(exc)
                else "render_failed",
            }
        )
    finally:
        _close(document)


if __name__ == "__main__":
    main()
