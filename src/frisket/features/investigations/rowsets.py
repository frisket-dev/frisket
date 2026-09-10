"""Read-only rowset resolver for investigative mapping workflows.

FollowTheMoney export/import and graph projection need a stable mapping
surface over ordinary Frisket sheets. This module deliberately does not create
virtual tables or canonical FtM storage; it resolves visible rows/columns and
current cell values into source-aware records that later mappers can consume.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from frisket.engine.store.evidence import list_cell_evidence
from frisket.server.exports import rowset as export_rowset
from frisket.engine.store import Project

INVESTIGATIVE_ROWSET_SCHEMA_VERSION = "frisket.investigative_rowset.v1"
SUPPORTED_ROWSET_KINDS = frozenset({"sheet", "materialized_sheet"})


class InvestigativeRowsetError(Exception):
    """Typed rowset failure carrying a stable code for future action mappers."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}


def resolve_investigative_rowset(
    project: Project,
    rowset: Mapping[str, Any],
    *,
    include_evidence: bool = True,
    project_id: str | None = None,
    batch_size: int = 1000,
) -> dict[str, Any]:
    """Resolve a normal/materialized sheet into mapping-ready records.

    Values come from :meth:`Project.get_values_with_refs`, preserving Frisket's
    existing precedence: manual edits, then current run results, then source
    cells. Evidence comes through :func:`frisket.store.evidence.list_cell_evidence`
    and is scoped to the current value ref.
    """

    kind = _rowset_kind(rowset)
    sheet_id = _sheet_id(rowset)
    sheet = _resolve_sheet(project, sheet_id)
    if kind == "materialized_sheet" and sheet["parent_sheet_id"] is None:
        raise InvestigativeRowsetError(
            "invalid_materialized_sheet",
            f"sheet_id {sheet_id} is not a materialized sheet",
            field="rowset.sheet_id",
            details={"sheet_id": sheet_id},
        )
    if batch_size <= 0:
        raise InvestigativeRowsetError(
            "invalid_rowset_spec",
            "batch_size must be a positive integer",
            field="batch_size",
            details={"batch_size": batch_size},
        )

    canonical_rowset = {"kind": kind, "sheet_id": sheet_id}
    columns = _visible_columns(project, sheet_id)
    row_ids = _visible_row_ids(project, sheet_id, batch_size=batch_size)
    row_meta = _row_metadata(project, sheet_id, row_ids, batch_size=batch_size)
    membership_by_row = _materialized_membership_by_row(project, row_ids)
    values_by_column: dict[int, dict[int, Any]] = {}
    refs_by_column: dict[int, dict[int, dict[str, Any]]] = {}
    for column in columns:
        values, refs = project.get_values_with_refs(
            sheet_id, column["id"], row_ids=row_ids
        )
        values_by_column[column["id"]] = values
        refs_by_column[column["id"]] = refs

    records = [
        _record_for_row(
            project,
            sheet=sheet,
            columns=columns,
            row_id=row_id,
            position=row_meta[row_id]["position"],
            parent_row_id=row_meta[row_id]["parent_row_id"],
            materialized_membership=membership_by_row.get(row_id, []),
            values_by_column=values_by_column,
            refs_by_column=refs_by_column,
            include_evidence=include_evidence,
            project_id=project_id,
        )
        for row_id in row_ids
    ]

    return {
        "schema_version": INVESTIGATIVE_ROWSET_SCHEMA_VERSION,
        "rowset": canonical_rowset,
        "sheet": {
            "id": sheet["id"],
            "name": sheet["name"],
            "parent_sheet_id": sheet["parent_sheet_id"],
            "parent_op_id": sheet["parent_op_id"],
            "is_materialized": sheet["parent_sheet_id"] is not None,
        },
        "columns": columns,
        "row_refs": [record["row_ref"] for record in records],
        "records": records,
    }


def _rowset_kind(rowset: Mapping[str, Any]) -> str:
    kind = rowset.get("kind")
    if not isinstance(kind, str) or not kind:
        raise InvestigativeRowsetError(
            "invalid_rowset_spec",
            "rowset.kind must be a non-empty string",
            field="rowset.kind",
            details={"kind": kind},
        )
    if kind not in SUPPORTED_ROWSET_KINDS:
        raise InvestigativeRowsetError(
            "unsupported_rowset_kind",
            f"unsupported investigative rowset kind: {kind}",
            field="rowset.kind",
            details={"kind": kind},
        )
    return kind


def _sheet_id(rowset: Mapping[str, Any]) -> int:
    sheet_id = rowset.get("sheet_id")
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool) or sheet_id <= 0:
        raise InvestigativeRowsetError(
            "invalid_rowset_spec",
            "rowset.sheet_id must be a positive integer",
            field="rowset.sheet_id",
            details={"sheet_id": sheet_id},
        )
    return int(sheet_id)


def _resolve_sheet(project: Project, sheet_id: int) -> dict[str, Any]:
    try:
        export_rowset.resolve_sheet(project, sheet_id)
    except export_rowset.ExportError as exc:
        raise InvestigativeRowsetError(
            exc.code,
            exc.message,
            field="rowset.sheet_id",
            details=exc.details or {"sheet_id": sheet_id},
        ) from exc
    row = project.db.execute(
        "SELECT id, name, parent_sheet_id, parent_op_id "
        "FROM sheets WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    if row is None:
        raise InvestigativeRowsetError(
            "invalid_sheet_ref",
            f"sheet_id {sheet_id} does not identify a visible sheet",
            field="rowset.sheet_id",
            details={"sheet_id": sheet_id},
        )
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "parent_sheet_id": (
            int(row["parent_sheet_id"]) if row["parent_sheet_id"] is not None else None
        ),
        "parent_op_id": (
            int(row["parent_op_id"]) if row["parent_op_id"] is not None else None
        ),
    }


def _visible_columns(project: Project, sheet_id: int) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for column in project.columns(sheet_id):
        column_id = int(column["id"])
        name = str(column["name"])
        columns.append(
            {
                "id": column_id,
                "name": name,
                "type": str(column["type"]),
                "position": int(column["position"]),
                "ai_generated": bool(column["ai_generated"]),
                "format": column["format"],
                "source_column_ref": {
                    "kind": "column",
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                    "column_name": name,
                },
            }
        )
    return columns


def _visible_row_ids(project: Project, sheet_id: int, *, batch_size: int) -> list[int]:
    row_ids: list[int] = []
    for batch in export_rowset.iter_row_id_batches(
        project, sheet_id, None, batch_size=batch_size
    ):
        row_ids.extend(batch)
    return row_ids


def _row_metadata(
    project: Project, sheet_id: int, row_ids: list[int], *, batch_size: int
) -> dict[int, dict[str, int | None]]:
    if not row_ids:
        return {}
    by_id: dict[int, dict[str, int | None]] = {}
    chunk_size = max(1, min(int(batch_size), 500))
    for start in range(0, len(row_ids), chunk_size):
        chunk = row_ids[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = project.db.execute(
            "SELECT id, position, parent_row_id FROM rows "
            f"WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
            (sheet_id, *chunk),
        ).fetchall()
        by_id.update(
            {
                int(row["id"]): {
                    "position": int(row["position"]),
                    "parent_row_id": (
                        int(row["parent_row_id"])
                        if row["parent_row_id"] is not None
                        else None
                    ),
                }
                for row in rows
            }
        )
    missing = [row_id for row_id in row_ids if row_id not in by_id]
    if missing:
        raise InvestigativeRowsetError(
            "row_metadata_missing",
            "rowset row metadata is missing for one or more visible rows",
            field="rowset.rows",
            details={"row_ids": missing},
        )
    return {row_id: by_id[row_id] for row_id in row_ids}


def _value_ref_for(
    refs: dict[int, dict[str, Any]], *, row_id: int, column_id: int
) -> dict[str, Any]:
    value_ref = refs.get(row_id)
    if value_ref is None:
        raise InvestigativeRowsetError(
            "row_value_ref_missing",
            "rowset value ref is missing for one or more visible cells",
            field="rowset.cells",
            details={"row_id": row_id, "column_id": column_id},
        )
    return value_ref


def _record_for_row(
    project: Project,
    *,
    sheet: dict[str, Any],
    columns: list[dict[str, Any]],
    row_id: int,
    position: int,
    parent_row_id: int | None,
    materialized_membership: list[dict[str, Any]],
    values_by_column: dict[int, dict[int, Any]],
    refs_by_column: dict[int, dict[int, dict[str, Any]]],
    include_evidence: bool,
    project_id: str | None,
) -> dict[str, Any]:
    sheet_id = int(sheet["id"])
    row_ref = _row_ref(sheet_id, row_id)
    lineage = _lineage_ref(sheet, parent_row_id)
    cells: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    value_refs: dict[str, dict[str, Any]] = {}
    for column in columns:
        column_id = int(column["id"])
        name = str(column["name"])
        value = values_by_column[column_id].get(row_id)
        value_ref = _value_ref_for(
            refs_by_column[column_id],
            row_id=row_id,
            column_id=column_id,
        )
        values[name] = value
        value_refs[name] = value_ref
        cells.append(
            _cell_for_column(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column=column,
                value=value,
                value_ref=value_ref,
                include_evidence=include_evidence,
                project_id=project_id,
            )
        )
    return {
        "row_ref": row_ref,
        "source_row_ref": row_ref,
        "materialized_membership": materialized_membership,
        "position": position,
        "lineage": lineage,
        "values": values,
        "value_refs": value_refs,
        "cells": cells,
    }


def _cell_for_column(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    column: dict[str, Any],
    value: Any,
    value_ref: dict[str, Any],
    include_evidence: bool,
    project_id: str | None,
) -> dict[str, Any]:
    column_id = int(column["id"])
    column_name = str(column["name"])
    evidence = (
        list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            project_id=project_id,
        )
        if include_evidence
        else None
    )
    return {
        "column_id": column_id,
        "column_name": column_name,
        "column_type": column["type"],
        "value": value,
        "value_ref": value_ref,
        "source_sheet_ref": _sheet_ref(sheet_id),
        "source_row_ref": _row_ref(sheet_id, row_id),
        "source_column_ref": column["source_column_ref"],
        "source_cell_ref": {
            "kind": "cell",
            "sheet_id": sheet_id,
            "row_id": row_id,
            "column_id": column_id,
            "column_name": column_name,
        },
        "evidence_refs": list(evidence["links"]) if evidence is not None else [],
        "evidence": evidence,
    }


def _lineage_ref(
    sheet: dict[str, Any], parent_row_id: int | None
) -> dict[str, Any] | None:
    parent_sheet_id = sheet["parent_sheet_id"]
    parent_op_id = sheet["parent_op_id"]
    if parent_sheet_id is None and parent_row_id is None and parent_op_id is None:
        return None
    return {
        "parent_sheet_ref": (
            _sheet_ref(parent_sheet_id) if parent_sheet_id is not None else None
        ),
        "parent_row_ref": (
            _row_ref(parent_sheet_id, parent_row_id)
            if parent_sheet_id is not None and parent_row_id is not None
            else None
        ),
        "parent_op_id": parent_op_id,
    }


def _sheet_ref(sheet_id: int) -> dict[str, int | str]:
    return {"kind": "sheet", "sheet_id": int(sheet_id)}


def _row_ref(sheet_id: int, row_id: int) -> dict[str, int | str]:
    return {"kind": "row", "sheet_id": int(sheet_id), "row_id": int(row_id)}


def _materialized_membership_by_row(
    project: Project, row_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    from frisket.engine.store.materialization import active_materialized_row_sources

    rows = active_materialized_row_sources(
        project.db,
        materialized_row_ids=row_ids,
        roles=("edge_source", "edge_target", "aggregate_source"),
    )
    by_row: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_row.setdefault(int(row["materialized_row_id"]), []).append(row)
    return by_row
