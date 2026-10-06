"""Materialize a single-page PDF from a page-scoped source reference."""

from __future__ import annotations

from pathlib import Path


class PdfPageSourceError(ValueError):
    """The selected page cannot be materialized from the supplied PDF."""


def materialize_pdf_page(
    source: Path,
    *,
    page: int,
    destination: Path,
) -> Path:
    """Write one 1-based PDF page to ``destination`` without changing ``source``."""
    if type(page) is not int or page < 1:
        raise PdfPageSourceError("PDF page selector must be a positive integer")
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(source)
        if page > len(reader.pages):
            raise PdfPageSourceError(
                f"PDF page selector {page} exceeds the {len(reader.pages)} page document"
            )
        writer = PdfWriter()
        writer.add_page(reader.pages[page - 1])
        with destination.open("xb") as output:
            writer.write(output)
        writer.close()
    except PdfPageSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - hostile PDF parsing boundary
        destination.unlink(missing_ok=True)
        raise PdfPageSourceError("selected PDF page could not be materialized") from exc
    return destination
