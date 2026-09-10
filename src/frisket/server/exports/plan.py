"""Shared dataset export plan.

CSV, Google Sheets, JSONL, and Parquet all consume the same resolved plan:
source rowset, projected columns, typed row batches, media policy, and the
metadata receipts/renderers need. The plan never stringifies values — value
display rules belong to the destination renderer (e.g. CSV ``display_scalars``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

from frisket.server.exports import rowset as _rowset
from frisket.server.exports.rowset import ExportError
from frisket.engine.store import Project

EXPORT_PLAN_SCHEMA_VERSION = "frisket.export_plan.v1"


def rowset_hash(
    *,
    row_ids: list[int],
    column_names: list[str],
    query_hash: str | None,
    schema_version: str,
) -> str:
    """Stable hash of an exported rowset for bounded large-export evidence.

    Derived from ordered row ids + ordered exported column names + query hash +
    plan schema version. This is NOT the rendered-content hash (the artifact
    sha256 already captures exact bytes).
    """
    canonical = json.dumps(
        {
            "row_ids": [int(rid) for rid in row_ids],
            "columns": list(column_names),
            "query_hash": query_hash,
            "schema_version": schema_version,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ColumnRole = Literal["value", "media_display", "media_ref"]
MediaPolicy = Literal["references", "omit"]
ValuePolicy = Literal["typed", "display_scalars"]


@dataclass(frozen=True)
class ExportColumn:
    """One column in the rendered output.

    ``role`` distinguishes plain value columns from the flattened media display
    column and its reference siblings. ``source_column_id`` is the underlying
    sheet column a media sibling derives from; ``media_field`` names which
    reference field a sibling carries (Lane C).
    """

    name: str
    type: str
    role: ColumnRole
    source_column_id: int | None
    column_id: int | None = None
    media_field: str | None = None


@dataclass(frozen=True)
class ExportSource:
    kind: Literal["current_sheet", "current_view", "all_sheets"]
    sheet_id: int | None = None
    query: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExportColumns:
    mode: Literal["all_visible", "selected"] = "all_visible"
    column_ids: tuple[int, ...] | None = None


@dataclass(frozen=True)
class SheetExportPlan:
    sheet_id: int
    sheet_name: str
    columns: list[ExportColumn]
    row_ids: list[int] | None
    query: dict[str, Any] | None
    query_hash: str | None
    query_total: int | None
    evaluator: dict[str, str] | None
    media_policy: MediaPolicy
    value_policy: ValuePolicy
    schema_version: str = EXPORT_PLAN_SCHEMA_VERSION

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    @property
    def source_column_ids(self) -> list[int]:
        """Distinct underlying sheet column ids contributing to the output."""
        seen: dict[int, None] = {}
        for column in self.columns:
            cid = column.source_column_id
            if cid is not None and cid not in seen:
                seen[cid] = None
        return list(seen)


@dataclass(frozen=True)
class ExportRowBatch:
    row_ids: list[int]
    rows: list[list[Any]] = field(default_factory=list)  # aligned to plan.columns


def plan_receipt_payload(plan: SheetExportPlan, row_ids: list[int]) -> dict[str, Any]:
    """Build the receipt/artifact payload shared by every dataset destination."""
    payload: dict[str, Any] = {
        "sheet_id": plan.sheet_id,
        "sheet_name": plan.sheet_name,
        "columns": [
            {
                "column_id": column.column_id,
                "source_column_id": column.source_column_id,
                "name": column.name,
                "type": column.type,
                "role": column.role,
                "media_field": column.media_field,
            }
            for column in plan.columns
        ],
        "column_count": len(plan.columns),
        "row_ids": list(row_ids),
        "row_count": len(row_ids),
        "schema_version": plan.schema_version,
    }
    if plan.query is not None:
        payload["query"] = plan.query
        payload["query_hash"] = plan.query_hash
        payload["query_total"] = plan.query_total
        payload["query_evaluator"] = plan.evaluator
    return payload


def build_sheet_export_plan(
    project: Project,
    sheet_id: int,
    *,
    query: dict[str, Any] | None = None,
    columns: ExportColumns | None = None,
    media_policy: MediaPolicy = "references",
    value_policy: ValuePolicy = "typed",
    max_rows: int | None = None,
    query_field: str = "params.query",
    streaming_rowset: bool = False,
) -> SheetExportPlan:
    columns = columns or ExportColumns()
    if columns.mode != "all_visible":
        # Selected-column projection is deferred to avoid the all_sheets x
        # selected ambiguity.
        raise ExportError(
            "unsupported_export_columns",
            "dataset exports support only columns.mode='all_visible' in this slice",
            field="params.columns.mode",
            details={"mode": columns.mode},
        )
    if media_policy not in ("references", "omit"):
        raise ExportError(
            "unsupported_media_policy",
            f"unsupported media_policy {media_policy!r}",
            field="params.media_policy",
            details={"media_policy": media_policy},
        )

    sheet = _rowset.resolve_sheet(project, sheet_id)
    resolved = _rowset.resolve_rowset(
        project,
        sheet_id,
        query,
        max_rows=max_rows,
        query_field=query_field,
        materialize_small=not streaming_rowset,
    )
    export_columns = _build_export_columns(project, sheet_id, media_policy)
    return SheetExportPlan(
        sheet_id=sheet.sheet_id,
        sheet_name=sheet.sheet_name,
        columns=export_columns,
        row_ids=resolved.row_ids,
        query=resolved.query,
        query_hash=resolved.query_hash,
        query_total=resolved.total,
        evaluator=resolved.evaluator,
        media_policy=media_policy,
        value_policy=value_policy,
    )


def _build_export_columns(
    project: Project, sheet_id: int, media_policy: MediaPolicy
) -> list[ExportColumn]:
    """Project visible sheet columns into output columns via media projection."""
    from frisket.server.exports import media as _media

    return _media.project_export_columns(project, sheet_id, media_policy)


def iter_export_row_batches(
    project: Project,
    plan: SheetExportPlan,
    *,
    batch_size: int = 1000,
) -> Iterator[ExportRowBatch]:
    """Yield typed row batches whose values align to ``plan.columns``.

    Values come straight from ``Project.get_values`` (manual edit > current run
    result > source cell) with no stringification. Media columns resolve their
    fixed reference siblings per row.
    """
    from frisket.server.exports import media as _media

    media_source_ids = {
        column.source_column_id
        for column in plan.columns
        if column.role in ("media_display", "media_ref")
        and column.source_column_id is not None
    }
    for batch_row_ids in _rowset.iter_row_id_batches(
        project,
        plan.sheet_id,
        plan.row_ids,
        batch_size=batch_size,
        query=plan.query,
    ):
        values_by_col = {
            cid: project.get_values(plan.sheet_id, cid, row_ids=batch_row_ids)
            for cid in plan.source_column_ids
        }
        rows: list[list[Any]] = []
        for row_id in batch_row_ids:
            ref_by_src = {
                sid: _media.resolve_media_reference(
                    project, values_by_col[sid].get(row_id)
                )
                for sid in media_source_ids
            }
            rows.append(
                [
                    _cell_for_column(column, values_by_col, ref_by_src, row_id)
                    for column in plan.columns
                ]
            )
        yield ExportRowBatch(row_ids=batch_row_ids, rows=rows)


def _cell_for_column(
    column: ExportColumn,
    values_by_col: dict[int, dict[int, Any]],
    ref_by_src: dict[int, dict[str, Any]],
    row_id: int,
) -> Any:
    if column.source_column_id is None:
        return None
    if column.role in ("media_display", "media_ref"):
        reference = ref_by_src[column.source_column_id]
        return reference.get(column.media_field)
    return values_by_col[column.source_column_id].get(row_id)
