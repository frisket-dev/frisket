"""Resolve owned PDF cells to original PDF bytes for model-row actions."""

from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from frisket.actions.types import RowError
from frisket.ai.llm.document_input import (
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    inspect_pdf_bytes,
)
from frisket.ai.llm.types import LLMError
from frisket.engine.pdf_page_source import PdfPageSourceError, materialize_pdf_page
from frisket.engine.store.blob_backend import BlobNotFoundError


def pdf_part(project: Any, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
        raise RowError("pdf_input_invalid", "Choose an owned PDF file as model input.")
    mime = value.get("mime")
    filename = (
        str(value.get("filename") or "document.pdf")
        .replace("\\", "/")
        .rsplit("/", 1)[-1]
    )
    if mime != "application/pdf" and not filename.lower().endswith(".pdf"):
        raise RowError(
            "pdf_input_invalid", "This file is not a PDF. Run To markdown or OCR first."
        )
    page = value.get("page")
    if page is not None and (type(page) is not int or page < 1):
        raise RowError(
            "pdf_input_invalid", "PDF page selector must be a positive integer."
        )
    try:
        with (
            project.materialize_blob(value["blob"]) as raw_path,
            TemporaryDirectory(prefix="frisket-model-pdf-") as scratch,
        ):
            path = Path(raw_path)
            if page is not None:
                path = materialize_pdf_page(
                    path, page=page, destination=Path(scratch) / "selected-page.pdf"
                )
            if path.stat().st_size > MAX_PDF_BYTES:
                raise RowError(
                    "pdf_input_too_large",
                    "Direct PDF input is limited to 20 MiB per row.",
                )
            data = path.read_bytes()
            count, text_chars = inspect_pdf_bytes(
                data, max_bytes=MAX_PDF_BYTES, max_pages=MAX_PDF_PAGES
            )
    except RowError:
        raise
    except LLMError as exc:
        raise RowError(exc.provider_code or "pdf_input_invalid", str(exc)) from exc
    except (BlobNotFoundError, OSError, ValueError, PdfPageSourceError) as exc:
        raise RowError(
            "pdf_input_invalid",
            "The PDF could not be read. Check the file and selected page.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - hostile document parser boundary
        raise RowError(
            "pdf_input_invalid",
            "The PDF could not be read. Check the file and selected page.",
        ) from exc
    return {
        "__pdf_b64__": base64.b64encode(data).decode("ascii"),
        "mime": "application/pdf",
        "filename": (
            f"{Path(filename).stem}-page-{page}.pdf"
            if page is not None
            else Path(filename).name
        ),
        "page_count": count,
        "text_char_count": text_chars,
    }
