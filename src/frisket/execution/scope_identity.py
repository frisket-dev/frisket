"""Canonical identity of the live rows a paid/egressing action will read.

Action identity answers *what operation is this?*  This module answers the
orthogonal authorization question *which selected rows and columns does this
invocation cover?* Inputs are read coherently when each bounded batch runs.
This selection contains no values, revisions, or per-row quote allocations.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


WORK_SCOPE_SCHEMA_VERSION = "frisket.work-scope-selection.v2"


class AdmittedWorkScopeSnapshot(dict[str, Any]):
    """Fixed row/column selection, with no source values or revision archive."""


def canonical_work_scope_binding(
    project: Any,
    program: Any,
    spec: Mapping[str, Any],
    row_ids: Sequence[int],
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded, receipt-safe binding to the current source cells.

    ``row_ids`` are a set for authorization purposes, so their canonical form
    is sorted and duplicate-free. Source-column order remains the recipe's
    order because it is part of how the provider input is assembled.
    """

    sheet_id = int(spec["sheet_id"])
    columns_by_name = {
        str(column["name"]): int(column["id"]) for column in project.columns(sheet_id)
    }
    source_names = [str(name) for name in program.source_columns(dict(spec))]
    return canonical_work_scope_binding_for_columns(
        project,
        sheet_id=sheet_id,
        row_ids=row_ids,
        source_columns=[(name, columns_by_name.get(name)) for name in source_names],
        snapshot=snapshot,
    )


def canonical_work_scope_binding_for_columns(
    project: Any,
    *,
    sheet_id: int,
    row_ids: Sequence[int],
    source_columns: Sequence[tuple[str, int | None]],
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind current cells for an already-resolved source-column projection.

    Most recipes resolve source names themselves and use
    :func:`canonical_work_scope_binding`.  Boundaries such as direct plugin
    actions already hold exact column ids after their own validation.  This
    lower-level form lets those boundaries share the same value/revision
    identity without resolving the columns a second, potentially different,
    way.
    """

    canonical_row_ids: list[int] = []
    for raw_row_id in row_ids:
        if isinstance(raw_row_id, bool):
            raise ValueError("work-scope row ids must be integers, not booleans")
        canonical_row_ids.append(int(raw_row_id))
    canonical_row_ids = sorted(set(canonical_row_ids))

    sheet_id = int(sheet_id)
    canonical_sources: list[tuple[str, int | None]] = []
    for raw_name, raw_column_id in source_columns:
        name = str(raw_name)
        if isinstance(raw_column_id, bool):
            raise ValueError("work-scope column ids must be integers, not booleans")
        column_id = int(raw_column_id) if raw_column_id is not None else None
        canonical_sources.append((name, column_id))

    from frisket.engine.store.cells import replay_generated_value_hash

    # Approval describes fixed configuration/selection, not a frozen dataset.
    # No source values, revisions or per-cell allocation are materialized here.
    manifest = {
        "schema_version": WORK_SCOPE_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "row_ids": canonical_row_ids,
        "sources": [
            {"name": name, "column_id": cid} for name, cid in canonical_sources
        ],
    }
    if snapshot is not None:
        snapshot.clear()
        snapshot.update(manifest)
    return {
        "schema_version": manifest["schema_version"],
        "identity": replay_generated_value_hash(manifest),
        "sheet_id": sheet_id,
        "row_count": len(canonical_row_ids),
        "source_columns": manifest["sources"],
    }


__all__ = [
    "AdmittedWorkScopeSnapshot",
    "WORK_SCOPE_SCHEMA_VERSION",
    "canonical_work_scope_binding",
    "canonical_work_scope_binding_for_columns",
]
