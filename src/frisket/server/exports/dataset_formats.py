"""JSONL and Parquet renderers over the shared dataset export plan.

Both consume typed plan row batches (after media-policy projection) so they
never stringify JSON-ish values the way CSV does. pyarrow is imported lazily so
a missing optional dependency fails with a clear typed error instead of an
import-time crash.
"""

from __future__ import annotations

import io
import json
from typing import Any

from frisket.server.exports.plan import SheetExportPlan, iter_export_row_batches
from frisket.engine.store import Project

PARQUET_WRITER_UNAVAILABLE = "parquet_writer_unavailable"
PARQUET_RENDER_FAILED = "parquet_render_failed"


class DatasetFormatError(Exception):
    """Typed renderer failure mapped onto the action error contract."""

    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def render_jsonl(project: Project, plan: SheetExportPlan) -> tuple[list[int], bytes]:
    """Render one typed JSON object per row (newline-delimited)."""
    names = list(plan.column_names)
    row_ids: list[int] = []
    buffer = io.StringIO()
    for batch in iter_export_row_batches(project, plan):
        row_ids.extend(batch.row_ids)
        for row in batch.rows:
            obj = dict(zip(names, row))
            buffer.write(json.dumps(obj, ensure_ascii=False, sort_keys=True))
            buffer.write("\n")
    return row_ids, buffer.getvalue().encode("utf-8")


def render_parquet(project: Project, plan: SheetExportPlan) -> tuple[list[int], bytes]:
    """Render a Parquet table with stable schema metadata via pyarrow."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DatasetFormatError(
            PARQUET_WRITER_UNAVAILABLE,
            "Parquet export requires pyarrow, which is not installed",
            details={"error": str(exc)},
        ) from exc

    names = list(plan.column_names)
    col_types = [column.type for column in plan.columns]
    columns: list[list[Any]] = [[] for _ in names]
    row_ids: list[int] = []
    for batch in iter_export_row_batches(project, plan):
        row_ids.extend(batch.row_ids)
        for row in batch.rows:
            for index, value in enumerate(row):
                columns[index].append(_parquet_scalar(value, col_types[index]))

    metadata = {
        b"frisket.export_plan.schema_version": plan.schema_version.encode("utf-8"),
        b"frisket.sheet_id": str(plan.sheet_id).encode("utf-8"),
        b"frisket.sheet_name": plan.sheet_name.encode("utf-8"),
        b"frisket.row_count": str(len(row_ids)).encode("utf-8"),
    }
    if plan.query_hash:
        metadata[b"frisket.query_hash"] = plan.query_hash.encode("utf-8")
    try:
        arrays = [pa.array(column) for column in columns]
        table = pa.table(dict(zip(names, arrays))) if names else pa.table({})
        table = table.replace_schema_metadata(metadata)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink)
    except Exception as exc:  # noqa: BLE001 - surface a clear typed error
        # A column with genuinely heterogeneous scalar types (e.g. a stray
        # non-numeric edit in a numeric column) defeats Arrow type inference.
        raise DatasetFormatError(
            PARQUET_RENDER_FAILED,
            "Parquet export could not encode the sheet values; a column has "
            "mixed or unsupported types for a typed columnar artifact",
            details={"error": str(exc)[:300]},
        ) from exc
    return row_ids, sink.getvalue().to_pybytes()


def _parquet_scalar(value: Any, col_type: str) -> Any:
    """Coerce a typed cell to a Parquet-friendly scalar.

    ``json`` columns are intentionally heterogeneous, so their values (and any
    dict/list value elsewhere, e.g. a media envelope that escaped projection)
    are serialized to JSON text to keep the Parquet column a stable string type.
    Native scalars and None pass through for typed columns.
    """
    if value is None:
        return None
    if col_type == "json" or isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
