"""Optional Natural PDF adapter boundary for durable PDF table extraction."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


class NaturalPdfError(RuntimeError):
    """Base error raised by the Natural PDF adapter boundary."""


class NaturalPdfUnavailable(NaturalPdfError):
    """Natural PDF (the optional `pdf` extra) is not installed."""


class NaturalPdfUnsupportedMode(NaturalPdfError):
    """The requested extraction mode is outside the admitted backend slice."""


class NaturalPdfExtractionError(NaturalPdfError):
    """Natural PDF failed to extract tables from a source document."""


@dataclass(frozen=True)
class PdfTableExtractRequest:
    path: Path
    filename: str
    source_row_id: int
    source_blob_hash: str | None
    mode: Literal["extract_table"]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PdfTable:
    page_start: int | None
    page_end: int | None
    table_index: int
    header: list[str]
    rows: list[list[str | None]]
    raw_cells: list[list[Any | None]]
    warnings: list[str] = field(default_factory=list)


_RESERVED_OUTPUT_COLUMNS = frozenset(
    {
        "source_row_id",
        "source_filename",
        "source_blob_hash",
        "page_start",
        "page_end",
        "table_index",
        "table_row_index",
        "raw_cells_json",
    }
)


def extract_pdf_tables(request: PdfTableExtractRequest) -> list[PdfTable]:
    """Extract one (possibly multi-page) table using Natural PDF.

    The executor and tests depend only on the dataclass boundary above. The
    production adapter imports Natural PDF lazily so projects can open and other
    actions can run without the package installed.

    The real natural-pdf API stitches a table that spans pages via
    ``pdf.pages.to_flow().extract_table()``, which returns a single
    ``TableResult`` (a ``collections.abc.Sequence`` of row lists with a
    ``.headers`` property). We coerce that one result into one ``PdfTable``.
    """

    if request.mode != "extract_table":
        raise NaturalPdfUnsupportedMode(
            "media.extract_pdf_tables currently supports only mode='extract_table'"
        )
    try:
        natural_pdf = importlib.import_module("natural_pdf")
    except ImportError as exc:  # pragma: no cover - exercised through monkeypatch
        raise NaturalPdfUnavailable("Natural PDF is not installed") from exc

    # natural-pdf>=0.6.5 is pinned (the `pdf` extra), so PDF / pages.to_flow /
    # extract_table are guaranteed once the import succeeds; call them directly
    # and let any real API failure surface as NaturalPdfExtractionError.
    try:
        document = natural_pdf.PDF(str(request.path))
        result = document.pages.to_flow().extract_table(
            **_extract_table_kwargs(request.options)
        )
    except Exception as exc:  # noqa: BLE001 - provider failures cross this boundary
        raise NaturalPdfExtractionError(str(exc)) from exc
    return _to_pdf_tables(result)


def _extract_table_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    table_options = options.get("options") if isinstance(options, dict) else None
    return dict(table_options) if isinstance(table_options, dict) else {}


def _to_pdf_tables(result: Any) -> list[PdfTable]:
    """Coerce a single Natural PDF ``TableResult`` into one ``PdfTable``."""

    if result is None:
        return []
    rows = [list(row) for row in result]
    if not rows:
        return []
    # TableResult.headers falls back to the first row when no header is detected.
    # TODO: header-vs-body is heuristic here (drop row 0 when it equals the
    # detected header). When the import UI lands, surface a "has headers" y/n
    # checkbox so the user can override this guess instead of relying on it.
    raw_header = [
        str(value) if value is not None else "" for value in (result.headers or rows[0])
    ]
    first_row = [str(value) if value is not None else "" for value in rows[0]]
    raw_body = rows[1:] if raw_header == first_row else rows
    body = [
        [str(value) if value is not None else None for value in row] for row in raw_body
    ]
    return [
        PdfTable(
            page_start=None,
            page_end=None,
            table_index=0,
            header=_normalize_table_header(raw_header),
            rows=body,
            raw_cells=raw_body,
        )
    ]


def _normalize_table_header(values: list[str]) -> list[str]:
    names: list[str] = []
    used = set(_RESERVED_OUTPUT_COLUMNS)
    for index, value in enumerate(values, start=1):
        base = value.strip() or f"column_{index}"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        names.append(name)
        used.add(name)
    return names
