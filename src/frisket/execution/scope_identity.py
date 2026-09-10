"""Canonical identity of the live rows a paid/egressing action will read.

Action identity answers *what operation is this?*  This module answers the
orthogonal authorization question *which current data does this invocation
cover?*  The returned object contains no source values: it carries a digest
over canonical row ids, source-column identity, each live value's stable
hash, and the value ref that names the revision currently winning the cell
overlay.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


WORK_SCOPE_SCHEMA_VERSION = "frisket.work-scope-binding.v1"


def canonical_work_scope_binding(
    project: Any,
    recipe: Any,
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
    source_names = [str(name) for name in recipe.source_columns(dict(spec))]
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

    # Reuse the store's canonical JSON-value digest. It is already the
    # durable identity used when a generated cell is accepted/dismissed, so
    # consent and the cell overlay cannot disagree about value equality.
    from frisket.engine.store.cells import replay_generated_value_hash

    sources: list[dict[str, Any]] = []
    source_summary: list[dict[str, Any]] = []
    for name, column_id in canonical_sources:
        source_summary.append({"name": name, "column_id": column_id})
        if column_id is None:
            sources.append(
                {
                    "name": name,
                    "column_id": None,
                    "cells": [
                        {
                            "row_id": row_id,
                            "value_hash": replay_generated_value_hash(None),
                            "value_ref": {
                                "kind": "missing_column",
                                "row_id": row_id,
                                "column_id": None,
                            },
                        }
                        for row_id in canonical_row_ids
                    ],
                }
            )
            continue
        values, refs = project.get_values_with_refs(
            sheet_id,
            column_id,
            row_ids=canonical_row_ids,
        )
        cells = []
        for row_id in canonical_row_ids:
            ref = refs.get(row_id)
            cells.append(
                {
                    "row_id": row_id,
                    "value_hash": replay_generated_value_hash(values.get(row_id)),
                    "value_ref": dict(ref) if isinstance(ref, Mapping) else None,
                }
            )
        sources.append(
            {
                "name": name,
                "column_id": column_id,
                "cells": cells,
            }
        )

    manifest = {
        "schema_version": WORK_SCOPE_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "row_ids": canonical_row_ids,
        "sources": sources,
    }
    if snapshot is not None:
        # This is the exact cell-identity material that produced ``identity``
        # below.  Attempt admission keeps it in memory (never in a receipt)
        # so the row dispatcher can compare the value/ref pair it is about to
        # use with the pair admission actually verified.  Merely recomputing
        # the aggregate identity here and throwing the cells away left a
        # check/use window between ``AttemptAuthority.mint`` and egress.
        snapshot.clear()
        snapshot.update(manifest)
    return {
        "schema_version": WORK_SCOPE_SCHEMA_VERSION,
        "identity": replay_generated_value_hash(manifest),
        "sheet_id": sheet_id,
        "row_count": len(canonical_row_ids),
        "source_columns": source_summary,
    }


__all__ = [
    "WORK_SCOPE_SCHEMA_VERSION",
    "canonical_work_scope_binding",
    "canonical_work_scope_binding_for_columns",
]
