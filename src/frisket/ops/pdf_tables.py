"""Pure PDF table normalization shared by typed actions and the adapter."""

from __future__ import annotations

import json
from typing import Any

from frisket.ops.integrations import natural_pdf


def _pdf_table_error(
    code: str, message: str, *, details: dict[str, Any] | None = None
) -> ValueError:
    suffix = ""
    if details:
        suffix = " " + json.dumps(details, sort_keys=True, separators=(",", ":"))
    return ValueError(f"{code}: {message}{suffix}")


def _pdf_table_records(
    tables: list[natural_pdf.PdfTable],
    *,
    source_row: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    expected_columns: list[str] | None = None
    for table in tables:
        if not table.rows:
            continue
        columns = table.header
        if expected_columns is None:
            expected_columns = columns
        elif columns != expected_columns:
            raise _pdf_table_error(
                "pdf_table_shape_mismatch",
                "media.extract_pdf_tables extracted table shapes do not match",
                details={
                    "expected_columns": expected_columns,
                    "actual_columns": columns,
                    "row_id": source_row["row_id"],
                    "table_index": table.table_index,
                },
            )
        for row_index, (row, raw_row) in enumerate(
            zip(table.rows, table.raw_cells, strict=True), start=1
        ):
            records.append(
                {
                    "source_row_id": source_row["row_id"],
                    "source_filename": source_row["filename"],
                    "source_blob_hash": source_row["blob_hash"],
                    "page_start": table.page_start,
                    "page_end": table.page_end,
                    "table_index": table.table_index,
                    "table_row_index": row_index,
                    "raw_cells_json": raw_row,
                    **dict(zip(columns, row, strict=True)),
                }
            )
    return records, expected_columns or []


def _pdf_table_extract_options(spec: dict[str, Any]) -> dict[str, Any]:
    """Build the ``extract_table`` options dict natural_pdf.py forwards to
    ``extract_table(**kwargs)``. ``table_mode`` rides natural_pdf's own
    vocabulary directly ("stream"/"lattice" become its ``method`` argument;
    "auto"/unset omits ``method`` so natural_pdf picks) as a ``method``
    default under the free-form ``extract_table.options`` escape hatch — an
    explicit ``method`` the caller already set there wins."""
    options = dict(spec.get("extract_table") or {})
    table_mode = spec.get("table_mode") or "auto"
    if table_mode != "auto":
        inner = dict(options.get("options") or {})
        inner.setdefault("method", table_mode)
        options["options"] = inner
    return options


_MEDIA_PDF_TABLE_METADATA_COLUMN_TYPES: dict[str, str] = {
    "source_row_id": "integer",
    "source_filename": "text",
    "source_blob_hash": "text",
    "page_start": "integer",
    "page_end": "integer",
    "table_index": "integer",
    "table_row_index": "integer",
    "raw_cells_json": "json",
}


def _media_extract_pdf_tables_item_schema(table_columns: list[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, column_type in _MEDIA_PDF_TABLE_METADATA_COLUMN_TYPES.items():
        if column_type == "integer":
            properties[name] = {"type": ["integer", "null"]}
        elif column_type == "json":
            properties[name] = {}
        else:
            properties[name] = {"type": ["string", "null"]}
    for name in table_columns:
        properties[name] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
