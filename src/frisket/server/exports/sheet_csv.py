"""Shared visible-sheet CSV export renderer."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from frisket.server.exports.plan import (
    SheetExportPlan,
    iter_export_row_batches,
    plan_receipt_payload,
)
from frisket.engine.store import Project


# Compatibility name for callers outside this streaming path.  Local/Solo CSV
# exports intentionally have no total-row ceiling; hosted composition injects
# one through SheetExportLimits.
MAX_FILTERED_SHEET_CSV_EXPORT_ROWS: int | None = None


@dataclass(frozen=True)
class SheetExportLimits:
    """Deployment-owned export policy.  ``None`` means no total cap."""

    max_rows: int | None = None


# Leading characters that spreadsheet software may interpret as a formula when a
# downloaded CSV is opened.
FORMULA_TRIGGER_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
FORMULA_POLICIES = ("escape", "raw")


class SheetCsvNotFound(LookupError):
    """Raised when a visible sheet cannot be exported."""


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _escape_csv_formula(text: str) -> str:
    if text and text[0] in FORMULA_TRIGGER_PREFIXES:
        return "'" + text
    return text


def csv_display(value: Any, *, formula_policy: str = "escape") -> str:
    """Render a typed cell value as a CSV display string.

    ``formula_policy='escape'`` (default) prefixes string cells whose text would
    be treated as a formula with a leading apostrophe; ``'raw'`` preserves bytes.
    Only string values are escaped — a native numeric/boolean cell (e.g. the
    integer ``-1``) is data, not a formula, so it is never apostrophe-prefixed.
    """
    text = csv_cell(value)
    if formula_policy == "escape" and isinstance(value, str):
        return _escape_csv_formula(text)
    return text


def iter_export_csv_rows(
    project: Project,
    plan: SheetExportPlan,
    *,
    formula_policy: str = "escape",
    batch_size: int = 1000,
) -> Iterator[list[str]]:
    """Stream CSV rows (header first) from a shared export plan."""
    yield [
        csv_display(name, formula_policy=formula_policy) for name in plan.column_names
    ]
    for batch in iter_export_row_batches(project, plan, batch_size=batch_size):
        for row in batch.rows:
            yield [csv_display(value, formula_policy=formula_policy) for value in row]


def render_export_csv(
    project: Project,
    plan: SheetExportPlan,
    *,
    formula_policy: str = "escape",
    batch_size: int = 1000,
) -> tuple[dict[str, Any], str]:
    """Render the full CSV for a plan and return (receipt payload, text)."""
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        [csv_display(name, formula_policy=formula_policy) for name in plan.column_names]
    )
    exported_row_ids: list[int] = []
    for batch in iter_export_row_batches(project, plan, batch_size=batch_size):
        exported_row_ids.extend(batch.row_ids)
        writer.writerows(
            [csv_display(value, formula_policy=formula_policy) for value in row]
            for row in batch.rows
        )
    return plan_receipt_payload(plan, exported_row_ids), out.getvalue()


def iter_export_csv_bytes(
    project: Project,
    plan: SheetExportPlan,
    *,
    formula_policy: str = "escape",
    batch_size: int = 1000,
    max_chunk_bytes: int = 64 * 1024,
    bom: bool = True,
    on_batch: Callable[[list[int]], None] | None = None,
) -> Iterator[bytes]:
    """Incrementally encode a CSV without collecting the rendered corpus.

    Encoded records larger than the preferred chunk size are byte-sliced. CSV
    consumers see the exact same byte stream after concatenation while network
    and disk consumers never receive an oversized buffer.
    """
    pending = bytearray(b"\xef\xbb\xbf" if bom else b"")

    def encoded(row: list[str]) -> bytes:
        out = io.StringIO(newline="")
        csv.writer(out).writerow(row)
        return out.getvalue().encode("utf-8")

    rows: Iterator[list[str]]
    header = [
        csv_display(name, formula_policy=formula_policy) for name in plan.column_names
    ]

    def data_rows() -> Iterator[list[str]]:
        yield header
        for batch in iter_export_row_batches(project, plan, batch_size=batch_size):
            if on_batch is not None:
                on_batch(batch.row_ids)
            for row in batch.rows:
                yield [
                    csv_display(value, formula_policy=formula_policy) for value in row
                ]

    rows = data_rows()
    for row in rows:
        item = encoded(row)
        if pending and len(pending) + len(item) > max_chunk_bytes:
            yield bytes(pending)
            pending.clear()
        if len(item) > max_chunk_bytes:
            for start in range(0, len(item), max_chunk_bytes):
                yield item[start : start + max_chunk_bytes]
        else:
            pending.extend(item)
    if pending:
        yield bytes(pending)


def sheet_csv_metadata(project: Project, sheet_id: int) -> dict[str, Any]:
    sheet = project.db.execute(
        "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise SheetCsvNotFound(f"no visible sheet {sheet_id}")
    columns = [
        {
            "column_id": int(column["id"]),
            "name": str(column["name"]),
            "type": str(column["type"]),
            "position": int(column["position"]),
            "current_run_id": (
                int(column["current_run_id"])
                if column["current_run_id"] is not None
                else None
            ),
        }
        for column in project.columns(sheet_id)
    ]
    return {
        "sheet_id": int(sheet["id"]),
        "sheet_name": str(sheet["name"]),
        "columns": columns,
        "column_count": len(columns),
    }


def iter_sheet_csv_rows(
    project: Project,
    sheet_id: int,
    *,
    batch_size: int = 1000,
    metadata: dict[str, Any] | None = None,
    row_ids: list[int] | None = None,
) -> Iterator[list[str]]:
    payload = (
        metadata if metadata is not None else sheet_csv_metadata(project, sheet_id)
    )
    columns = list(payload["columns"])
    yield [column["name"] for column in columns]
    for _, rows in _iter_sheet_csv_data_batches(
        project, sheet_id, columns, batch_size=batch_size, row_ids=row_ids
    ):
        yield from rows


def render_sheet_csv(
    project: Project,
    sheet_id: int,
    *,
    batch_size: int = 1000,
    metadata: dict[str, Any] | None = None,
    row_ids: list[int] | None = None,
) -> tuple[dict[str, Any], str]:
    payload = dict(
        metadata if metadata is not None else sheet_csv_metadata(project, sheet_id)
    )
    columns = list(payload["columns"])
    exported_row_ids: list[int] = []
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow([column["name"] for column in columns])
    for batch_row_ids, rows in _iter_sheet_csv_data_batches(
        project, sheet_id, columns, batch_size=batch_size, row_ids=row_ids
    ):
        exported_row_ids.extend(batch_row_ids)
        writer.writerows(rows)
    payload["row_ids"] = exported_row_ids
    payload["row_count"] = len(exported_row_ids)
    return payload, out.getvalue()


def _iter_sheet_csv_data_batches(
    project: Project,
    sheet_id: int,
    columns: list[dict[str, Any]],
    *,
    batch_size: int,
    row_ids: list[int] | None,
) -> Iterator[tuple[list[int], list[list[str]]]]:
    if row_ids is not None:
        for start in range(0, len(row_ids), batch_size):
            batch_row_ids = [
                int(row_id) for row_id in row_ids[start : start + batch_size]
            ]
            yield (
                batch_row_ids,
                _sheet_csv_rows_for_row_ids(project, sheet_id, columns, batch_row_ids),
            )
        return

    last_position: int | None = None
    last_id = 0
    while True:
        where = "sheet_id=? AND hidden=0"
        params: list[Any] = [sheet_id]
        if last_position is not None:
            where += " AND (position > ? OR (position=? AND id > ?))"
            params.extend([last_position, last_position, last_id])
        params.append(batch_size)
        batch = project.db.execute(
            f"SELECT id, position FROM rows WHERE {where} "
            "ORDER BY position ASC, id ASC LIMIT ?",
            params,
        ).fetchall()
        if not batch:
            break
        row_ids = [int(row["id"]) for row in batch]
        yield row_ids, _sheet_csv_rows_for_row_ids(project, sheet_id, columns, row_ids)
        last_position = int(batch[-1]["position"])
        last_id = int(batch[-1]["id"])


def _sheet_csv_rows_for_row_ids(
    project: Project,
    sheet_id: int,
    columns: list[dict[str, Any]],
    row_ids: list[int],
) -> list[list[str]]:
    values_by_col = {
        int(column["column_id"]): project.get_values(
            sheet_id,
            int(column["column_id"]),
            row_ids=row_ids,
        )
        for column in columns
    }
    return [
        [
            csv_cell(values_by_col[int(column["column_id"])].get(row_id))
            for column in columns
        ]
        for row_id in row_ids
    ]
