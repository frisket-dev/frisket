"""One-shot fenced PDFium raster worker.

The request is a small JSON object on stdin.  PDFium and its Pillow adapter
are imported only after the runtime bootstrap has installed the converter
fence, so malformed PDFs never load in a server process.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


def _reply(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":"), allow_nan=False))


def _request() -> tuple[Path, Path, int, list[int] | None, int | None]:
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
    ):
        raise ValueError("invalid_request")
    return Path(source), Path(output), dpi, pages, page_limit


def _close(value: Any) -> None:
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()


def main() -> None:
    try:
        source, output, dpi, pages, page_limit = _request()
    except ValueError as exc:
        _reply({"ok": False, "error": str(exc)})
        return
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(source))
    except Exception:
        _reply({"ok": False, "error": "invalid_pdf"})
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
            page = document[number - 1]
            # Preserve the full MediaBox raster used by imports and OCR text
            # coordinates; PDFium otherwise clips to the document CropBox.
            page.set_cropbox(*page.get_mediabox())
            bitmap = None
            image = None
            try:
                bitmap = page.render(scale=dpi / 72)
                image = bitmap.to_pil()
                image.save(output / f"page-{number}.png", format="PNG")
                rendered.append(number)
            finally:
                _close(image)
                _close(bitmap)
                _close(page)
        _reply({"ok": True, "page_count": page_count, "pages": rendered})
    except Exception:
        _reply({"ok": False, "error": "render_failed"})
    finally:
        _close(document)


if __name__ == "__main__":
    main()
