"""Internal materialized sheet writers.

These helpers own common project-write mechanics only. Callers keep source
resolution, freshness checks, replay rules, and receipt construction local to
their action family. Empty materialized sheets are allowed here; action-family
validators decide whether an empty result is semantically valid.
"""

from __future__ import annotations

from frisket.engine.store.transactions import inserted_rowid
from .derived_updates import MaterializedIdentity, establish_identity

import json
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from frisket.contracts.action import canonical_column_type
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)

_SQLITE_PARAM_CHUNK_SIZE = 500


@dataclass(frozen=True)
class MaterializedColumnSpec:
    name: str
    type: str
    ai_generated: bool = False
    hidden: bool = False
    format: str | None = None


@dataclass(frozen=True)
class SingleParentMaterializedRow:
    parent_row_id: int
    values: Mapping[str, Any]


@dataclass(frozen=True)
class SingleParentChildSheetPlan:
    action_kind: str
    label: str
    target_sheet_name: str
    parent_sheet_id: int
    op_spec: Mapping[str, Any]
    columns: Sequence[MaterializedColumnSpec]
    rows: Sequence[SingleParentMaterializedRow]


@dataclass(frozen=True)
class SingleParentChildSheetWrite:
    op_id: int
    sheet_id: int
    column_ids: dict[str, int]
    row_ids: list[int]
    parent_row_ids: list[int]
    undo_info: dict[str, list[int]]
    materialized_sheet_ref: dict[str, Any]
    materialized_column_refs: dict[str, dict[str, Any]]
    materialized_rows_ref: dict[str, Any]
    lineage_parent_rows_ref: dict[str, Any]


@dataclass(frozen=True)
class ContributorMaterializedRow:
    values: Mapping[str, Any]
    parent_row_id: int | None = None
    # Canonical role-bearing associations additional to plain parent lineage.
    sources: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class ContributorTablePlan:
    action_kind: str
    label: str
    target_sheet_name: str
    parent_sheet_id: int
    op_spec: Mapping[str, Any]
    columns: Sequence[MaterializedColumnSpec]
    rows: Sequence[ContributorMaterializedRow]
    refresh_identity: MaterializedIdentity | None = None


@dataclass(frozen=True)
class ContributorTableWrite(SingleParentChildSheetWrite):
    parent_row_ids: list[int | None]
    materialized_row_sources_ref: dict[str, Any]


@dataclass(frozen=True)
class EdgeTableColumnSpec:
    name: str
    type: str
    ai_generated: bool = True
    hidden: bool = False
    format: str | None = None


@dataclass(frozen=True)
class MaterializedEdgeRecord:
    source_row_id: int
    target_row_id: int | None
    values: Mapping[str, Any]


@dataclass(frozen=True)
class EdgeTablePlan:
    action_kind: str
    label: str
    target_sheet_name: str
    parent_sheet_id: int
    op_spec: Mapping[str, Any]
    columns: Sequence[EdgeTableColumnSpec]
    edges: Sequence[MaterializedEdgeRecord]


@dataclass(frozen=True)
class EdgeTableWrite:
    op_id: int
    sheet_id: int
    column_ids: dict[str, int]
    row_ids: list[int]
    source_row_ids: list[int]
    target_row_ids: list[int | None]
    undo_info: dict[str, list[int]]
    materialized_link_table_ref: dict[str, Any]
    materialized_link_rows_ref: dict[str, Any]
    materialized_row_sources_ref: dict[str, Any]
    membership_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class AggregateColumnSpec:
    name: str
    type: str
    ai_generated: bool = False
    hidden: bool = False
    format: str | None = None


@dataclass(frozen=True)
class AggregateMaterializedRow:
    values: Mapping[str, Any]
    source_row_ids: Sequence[int]


@dataclass(frozen=True)
class AggregateSheetPlan:
    action_kind: str
    label: str
    target_sheet_name: str
    parent_sheet_id: int
    op_spec: Mapping[str, Any]
    columns: Sequence[AggregateColumnSpec]
    rows: Sequence[AggregateMaterializedRow]
    # A model-backed aggregate family that must
    # establish its durable accounting envelope BEFORE provider egress mints
    # its op at execution start with a provisional spec, then finalizes THE
    # SAME op row here (kind/label/spec rewritten in the write transaction)
    # instead of inserting a second one.  None keeps today's insert.
    reuse_op_id: int | None = None
    refresh_identity: MaterializedIdentity | None = None


@dataclass(frozen=True)
class AggregateSheetWrite:
    op_id: int
    sheet_id: int
    column_ids: dict[str, int]
    row_ids: list[int]
    source_row_ids_by_row: list[list[int]]
    membership_rows: list[dict[str, Any]]
    undo_info: dict[str, list[int]]
    materialized_sheet_ref: dict[str, Any]
    materialized_column_refs: dict[str, dict[str, Any]]
    materialized_rows_ref: dict[str, Any]
    materialized_row_sources_ref: dict[str, Any]


def _append_new_materialization_op(
    cur: sqlite3.Cursor,
    plan: (SingleParentChildSheetPlan | AggregateSheetPlan | EdgeTablePlan),
) -> int:
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
        "VALUES (?, ?, ?, ?, 0)",
        (
            plan.action_kind,
            plan.label,
            json.dumps(plan.op_spec, sort_keys=True),
            "{}",
        ),
    )
    op_id = inserted_rowid(cur)
    from .changes import start_change

    start_change(cur.connection, op_id, writing=True)
    return op_id


def _insert_sheet_schema(
    cur: sqlite3.Cursor,
    plan: (SingleParentChildSheetPlan | AggregateSheetPlan | EdgeTablePlan),
    op_id: int,
) -> tuple[int, dict[str, int]]:
    cur.execute(
        "INSERT INTO sheets (name, position, parent_sheet_id, parent_op_id) "
        "VALUES (?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets), ?, ?)",
        (plan.target_sheet_name, plan.parent_sheet_id, op_id),
    )
    sheet_id = inserted_rowid(cur)
    column_ids: dict[str, int] = {}
    for idx, column in enumerate(plan.columns, start=1):
        cur.execute(
            "INSERT INTO columns (sheet_id, name, type, position, "
            "ai_generated, hidden, format) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sheet_id,
                column.name,
                canonical_column_type(column.type),
                idx,
                int(column.ai_generated),
                int(column.hidden),
                column.format,
            ),
        )
        column_ids[column.name] = inserted_rowid(cur)
    return sheet_id, column_ids


def _materialized_cell_writes(
    row_id: int,
    column_ids: Mapping[str, int],
    values: Mapping[str, Any],
) -> list[BaseCellWrite]:
    return [
        BaseCellWrite(row_id, column_ids[name], value)
        for name, value in values.items()
        if value is not None
    ]


def _write_undo_info(
    cur: sqlite3.Cursor,
    op_id: int,
    sheet_id: int,
    column_ids: Mapping[str, int],
    row_ids: list[int],
) -> dict[str, list[int]]:
    undo_info = {
        "created_sheets": [sheet_id],
        "created_columns": list(column_ids.values()),
        "created_rows": row_ids,
    }
    cur.execute(
        "UPDATE ops SET undo_info=? WHERE id=?",
        (json.dumps(undo_info, sort_keys=True), op_id),
    )
    from .structural_changes import record_structural_changes

    record_structural_changes(cur.connection, op_id, undo_info)
    return undo_info


def _close_atomic_materialization(cur, op_id):
    from types import SimpleNamespace
    from .changes import ChangeStore

    ChangeStore(SimpleNamespace(db=cur.connection)).close(op_id)


def write_single_parent_child_sheet(
    cur: sqlite3.Cursor,
    plan: SingleParentChildSheetPlan,
) -> SingleParentChildSheetWrite:
    """Write a child sheet whose every row has exactly one parent row.

    The caller must already hold the write transaction and is responsible for
    duplicate target checks, receipts, and commits/rollbacks.
    """

    _validate_single_parent_plan(plan)
    write = write_contributor_table(
        cur,
        ContributorTablePlan(
            action_kind=plan.action_kind,
            label=plan.label,
            target_sheet_name=plan.target_sheet_name,
            parent_sheet_id=plan.parent_sheet_id,
            op_spec=plan.op_spec,
            columns=plan.columns,
            rows=[
                ContributorMaterializedRow(
                    values=row.values, parent_row_id=row.parent_row_id
                )
                for row in plan.rows
            ],
        ),
    )
    return SingleParentChildSheetWrite(
        **{
            name: getattr(write, name)
            for name in SingleParentChildSheetWrite.__dataclass_fields__
        }
    )


def write_contributor_table(
    cur: sqlite3.Cursor,
    plan: ContributorTablePlan,
    *,
    defer_publication_close: bool = False,
) -> ContributorTableWrite:
    """Publish rows with independently declared navigation and contributors."""
    if not plan.columns or len({column.name for column in plan.columns}) != len(
        plan.columns
    ):
        raise ValueError("materialized table columns must be nonempty and unique")
    known = {column.name for column in plan.columns}
    for row in plan.rows:
        if set(row.values) - known or (
            row.parent_row_id is not None and not _is_positive_int(row.parent_row_id)
        ):
            raise ValueError("invalid materialized row")
        if len(set(row.sources)) != len(row.sources) or any(
            not _is_positive_int(source) for source, _role in row.sources
        ):
            raise ValueError("invalid or duplicate source membership")
    op_id = _append_new_materialization_op(cur, plan)
    producer_id = create_base_cell_producer(
        cur.connection, stage_id=f"op:{op_id}", op_id=op_id
    )
    sheet_id, column_ids = _insert_sheet_schema(cur, plan, op_id=op_id)

    row_ids: list[int] = []
    parent_row_ids: list[int | None] = []
    memberships = []
    cell_writes: list[BaseCellWrite] = []
    for idx, row in enumerate(plan.rows, start=1):
        parent_row_ids.append(row.parent_row_id)
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, ?)",
            (sheet_id, idx, row.parent_row_id),
        )
        row_id = inserted_rowid(cur)
        row_ids.append(row_id)
        cell_writes.extend(
            _materialized_cell_writes(row_id, column_ids, values=row.values)
        )
        for source_row_id, role in row.sources:
            memberships.append(
                _insert_materialized_row_source(
                    cur,
                    materialized_row_id=row_id,
                    source_row_id=source_row_id,
                    op_id=op_id,
                    role=role,
                )
            )

    initialize_base_cells(cur.connection, producer_id=producer_id, cells=cell_writes)
    undo_info = _write_undo_info(cur, op_id, sheet_id, column_ids, row_ids=row_ids)
    if plan.refresh_identity is not None:
        establish_identity(
            cur.connection,
            sheet_id=sheet_id,
            op_id=op_id,
            column_ids=column_ids,
            rows=[row.values for row in plan.rows],
            row_ids=row_ids,
            identity=plan.refresh_identity,
        )

    materialized_column_refs = {
        name: {
            "kind": "materialized_column",
            "sheet_id": sheet_id,
            "column_id": column_id,
            "op_id": op_id,
        }
        for name, column_id in column_ids.items()
    }
    materialized_rows_ref = {
        "kind": "materialized_rows",
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "parent_row_ids": parent_row_ids,
        "op_id": op_id,
    }
    lineage_parent_rows_ref = {
        "kind": "lineage_parent_rows",
        "parent_sheet_id": plan.parent_sheet_id,
        "child_sheet_id": sheet_id,
        "pairs": [
            {"child_row_id": child_id, "parent_row_id": parent_id}
            for child_id, parent_id in zip(row_ids, parent_row_ids, strict=True)
        ],
        "op_id": op_id,
    }
    if not defer_publication_close:
        _close_atomic_materialization(cur, op_id)
    return ContributorTableWrite(
        op_id=op_id,
        sheet_id=sheet_id,
        column_ids=column_ids,
        row_ids=row_ids,
        parent_row_ids=parent_row_ids,
        undo_info=undo_info,
        materialized_sheet_ref={
            "kind": "materialized_sheet",
            "sheet_id": sheet_id,
            "parent_sheet_id": plan.parent_sheet_id,
            "op_id": op_id,
        },
        materialized_column_refs=materialized_column_refs,
        materialized_rows_ref=materialized_rows_ref,
        lineage_parent_rows_ref=lineage_parent_rows_ref,
        materialized_row_sources_ref=materialized_row_sources_ref(
            op_id=op_id, rows=memberships
        ),
    )


def write_aggregate_sheet(
    cur: sqlite3.Cursor,
    plan: AggregateSheetPlan,
) -> AggregateSheetWrite:
    """Write a materialized aggregate sheet with canonical source membership."""

    _validate_aggregate_sheet_plan(plan)
    if plan.reuse_op_id is not None:
        # The family already appended this op (its pre-egress accounting
        # envelope); finalize it in place.  The cursor already advanced when
        # the op was appended, so it is not rewound here — rewinding could
        # discard redo state appended since.
        updated = cur.execute(
            "UPDATE ops SET kind=?, label=?, spec=?, undo_info=?, barrier=0 WHERE id=?",
            (
                plan.action_kind,
                plan.label,
                json.dumps(plan.op_spec, sort_keys=True),
                "{}",
                int(plan.reuse_op_id),
            ),
        ).rowcount
        if updated != 1:
            raise ValueError(
                f"aggregate sheet write cannot reuse op {plan.reuse_op_id}: "
                "the op row is missing"
            )
        op_id = int(plan.reuse_op_id)
        from .changes import start_change

        start_change(cur.connection, op_id, writing=True)
    else:
        op_id = _append_new_materialization_op(cur, plan)
    producer_id = create_base_cell_producer(
        cur.connection, stage_id=f"op:{op_id}", op_id=op_id
    )
    sheet_id, column_ids = _insert_sheet_schema(cur, plan, op_id=op_id)

    row_ids: list[int] = []
    source_row_ids_by_row: list[list[int]] = []
    membership_rows: list[dict[str, Any]] = []
    cell_writes: list[BaseCellWrite] = []
    for idx, row in enumerate(plan.rows, start=1):
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, NULL)",
            (sheet_id, idx),
        )
        row_id = inserted_rowid(cur)
        row_ids.append(row_id)
        source_row_ids = sorted(
            int(source_row_id) for source_row_id in row.source_row_ids
        )
        source_row_ids_by_row.append(source_row_ids)
        cell_writes.extend(
            _materialized_cell_writes(row_id, column_ids, values=row.values)
        )
        for source_row_id in source_row_ids:
            membership_rows.append(
                _insert_materialized_row_source(
                    cur,
                    materialized_row_id=row_id,
                    source_row_id=source_row_id,
                    op_id=op_id,
                    role="aggregate_source",
                )
            )

    initialize_base_cells(cur.connection, producer_id=producer_id, cells=cell_writes)
    undo_info = _write_undo_info(cur, op_id, sheet_id, column_ids, row_ids=row_ids)
    if plan.refresh_identity is not None:
        establish_identity(
            cur.connection,
            sheet_id=sheet_id,
            op_id=op_id,
            column_ids=column_ids,
            rows=[row.values for row in plan.rows],
            row_ids=row_ids,
            identity=plan.refresh_identity,
        )

    materialized_column_refs = {
        name: {
            "kind": "materialized_column",
            "sheet_id": sheet_id,
            "column_id": column_id,
            "op_id": op_id,
        }
        for name, column_id in column_ids.items()
    }
    materialized_rows_ref = {
        "kind": "materialized_rows",
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "source_row_ids_by_group": source_row_ids_by_row,
        "op_id": op_id,
    }
    return AggregateSheetWrite(
        op_id=op_id,
        sheet_id=sheet_id,
        column_ids=column_ids,
        row_ids=row_ids,
        source_row_ids_by_row=source_row_ids_by_row,
        membership_rows=membership_rows,
        undo_info=undo_info,
        materialized_sheet_ref={
            "kind": "materialized_sheet",
            "sheet_id": sheet_id,
            "op_id": op_id,
        },
        materialized_column_refs=materialized_column_refs,
        materialized_rows_ref=materialized_rows_ref,
        materialized_row_sources_ref=materialized_row_sources_ref(
            op_id=op_id,
            rows=membership_rows,
            include_rows=False,
        ),
    )


def write_edge_table(
    cur: sqlite3.Cursor,
    plan: EdgeTablePlan,
) -> EdgeTableWrite:
    """Write a materialized source/target edge table.

    ``rows.parent_row_id`` is set to the source row as a fast pointer. The
    canonical source/target membership is inserted into
    ``materialized_row_sources`` in the same transaction.
    """

    _validate_edge_table_plan(plan)
    op_id = _append_new_materialization_op(cur, plan)
    producer_id = create_base_cell_producer(
        cur.connection, stage_id=f"op:{op_id}", op_id=op_id
    )
    sheet_id, column_ids = _insert_sheet_schema(cur, plan, op_id=op_id)

    row_ids: list[int] = []
    source_row_ids: list[int] = []
    target_row_ids: list[int | None] = []
    membership_rows: list[dict[str, Any]] = []
    cell_writes: list[BaseCellWrite] = []
    for idx, edge in enumerate(plan.edges, start=1):
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, ?)",
            (sheet_id, idx, int(edge.source_row_id)),
        )
        row_id = inserted_rowid(cur)
        row_ids.append(row_id)
        source_row_ids.append(int(edge.source_row_id))
        target_row_ids.append(
            int(edge.target_row_id) if edge.target_row_id is not None else None
        )
        cell_writes.extend(
            _materialized_cell_writes(row_id, column_ids, values=edge.values)
        )
        membership_rows.append(
            _insert_materialized_row_source(
                cur,
                materialized_row_id=row_id,
                source_row_id=int(edge.source_row_id),
                op_id=op_id,
                role="edge_source",
            )
        )
        if edge.target_row_id is not None:
            membership_rows.append(
                _insert_materialized_row_source(
                    cur,
                    materialized_row_id=row_id,
                    source_row_id=int(edge.target_row_id),
                    op_id=op_id,
                    role="edge_target",
                )
            )

    initialize_base_cells(cur.connection, producer_id=producer_id, cells=cell_writes)
    undo_info = _write_undo_info(cur, op_id, sheet_id, column_ids, row_ids=row_ids)
    materialized_link_rows_ref = {
        "kind": "materialized_link_rows",
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "parent_row_ids": source_row_ids,
        "source_row_ids": source_row_ids,
        "target_row_ids": target_row_ids,
        "op_id": op_id,
    }
    _close_atomic_materialization(cur, op_id)
    return EdgeTableWrite(
        op_id=op_id,
        sheet_id=sheet_id,
        column_ids=column_ids,
        row_ids=row_ids,
        source_row_ids=source_row_ids,
        target_row_ids=target_row_ids,
        undo_info=undo_info,
        materialized_link_table_ref={
            "kind": "materialized_link_table",
            "sheet_id": sheet_id,
            "name": plan.target_sheet_name,
            "op_id": op_id,
            "column_ids": column_ids,
        },
        materialized_link_rows_ref=materialized_link_rows_ref,
        materialized_row_sources_ref=materialized_row_sources_ref(
            op_id=op_id,
            rows=membership_rows,
        ),
        membership_rows=membership_rows,
    )


def materialized_row_sources_ref(
    *,
    op_id: int,
    rows: Sequence[Mapping[str, Any]],
    include_rows: bool = True,
) -> dict[str, Any]:
    normalized = _normalized_materialized_row_sources(rows)
    ref: dict[str, Any] = {
        "kind": "materialized_row_sources",
        "op_id": op_id,
        "row_count": len(normalized),
        "sha256": _materialized_row_sources_hash(normalized),
    }
    if include_rows:
        ref["rows"] = [
            {
                "materialized_row_id": materialized_row_id,
                "source_row_id": source_row_id,
                "source_sheet_id": source_sheet_id,
                "role": role,
            }
            for (
                materialized_row_id,
                source_row_id,
                source_sheet_id,
                role,
            ) in normalized
        ]
    else:
        ref["storage"] = "normalized_relation_v1"
    return ref


def _materialized_row_source_dict(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "materialized_row_id": int(row["materialized_row_id"]),
        "source_row_id": int(row["source_row_id"]),
        "source_sheet_id": int(row["source_sheet_id"]),
        "op_id": int(row["op_id"]),
        "role": str(row["role"]),
    }


def load_materialized_row_sources_for_op(
    cur: sqlite3.Cursor,
    *,
    op_id: int,
    roles: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [op_id]
    role_clause = ""
    if roles is not None:
        if not roles:
            return []
        role_clause = f" AND role IN ({','.join('?' for _ in roles)})"
        params.extend(roles)
    rows = cur.execute(
        "SELECT materialized_row_id, source_row_id, source_sheet_id, op_id, role "
        "FROM materialized_row_sources WHERE op_id=?"
        f"{role_clause}",
        params,
    ).fetchall()
    return [_materialized_row_source_dict(row) for row in rows]


def active_materialized_row_sources(
    cur: sqlite3.Cursor,
    *,
    materialized_row_ids: Sequence[int] | None = None,
    roles: Sequence[str] | None = None,
    include_historical: bool = False,
) -> list[dict[str, Any]]:
    """Return materialized membership rows for active materialized rows.

    Active filtering follows the materialized output lifecycle: hidden
    materialized rows or sheets are excluded. Source rows are not filtered by
    visibility because membership is historical provenance for the materialized
    output; diagnostics report source-row orphaning and sheet-id drift.
    """

    normalized_roles: list[str] | None = None
    if roles is not None:
        if not roles:
            return []
        normalized_roles = [str(role) for role in roles]
    normalized_row_ids: list[int] | None = None
    if materialized_row_ids is not None:
        if not materialized_row_ids:
            return []
        normalized_row_ids = sorted({int(row_id) for row_id in materialized_row_ids})
    if (
        normalized_row_ids is not None
        and len(normalized_row_ids) > _SQLITE_PARAM_CHUNK_SIZE
    ):
        rows: list[dict[str, Any]] = []
        for start in range(0, len(normalized_row_ids), _SQLITE_PARAM_CHUNK_SIZE):
            rows.extend(
                _active_materialized_row_sources_query(
                    cur,
                    materialized_row_ids=normalized_row_ids[
                        start : start + _SQLITE_PARAM_CHUNK_SIZE
                    ],
                    roles=normalized_roles,
                    include_historical=include_historical,
                )
            )
        return _sort_materialized_row_source_rows(rows)
    return _sort_materialized_row_source_rows(
        _active_materialized_row_sources_query(
            cur,
            materialized_row_ids=normalized_row_ids,
            roles=normalized_roles,
            include_historical=include_historical,
        )
    )


def _active_materialized_row_sources_query(
    cur: sqlite3.Cursor,
    *,
    materialized_row_ids: Sequence[int] | None,
    roles: Sequence[str] | None,
    include_historical: bool,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    joins = ""
    if not include_historical:
        joins = (
            "JOIN rows AS r ON r.id = m.materialized_row_id "
            "JOIN sheets AS s ON s.id = r.sheet_id "
        )
        where.extend(["r.hidden=0", "s.hidden=0"])
    if materialized_row_ids is not None:
        where.append(
            f"m.materialized_row_id IN ({','.join('?' for _ in materialized_row_ids)})"
        )
        params.extend(materialized_row_ids)
    if roles is not None:
        where.append(f"m.role IN ({','.join('?' for _ in roles)})")
        params.extend(roles)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    rows = cur.execute(
        "SELECT m.materialized_row_id, m.source_row_id, m.source_sheet_id, "
        f"m.op_id, m.role FROM {'materialized_row_sources' if include_historical else 'current_materialized_row_sources'} AS m "
        f"{joins}{where_sql} "
        "ORDER BY m.materialized_row_id, m.role, m.source_row_id",
        params,
    ).fetchall()
    return [_materialized_row_source_dict(row) for row in rows]


def _sort_materialized_row_source_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        [_materialized_row_source_dict(row) for row in rows],
        key=lambda row: (
            row["materialized_row_id"],
            row["role"],
            row["source_row_id"],
            row["op_id"],
        ),
    )


def materialized_row_source_diagnostics(
    cur: sqlite3.Cursor,
    *,
    expected_roles_by_materialized_row: Mapping[int, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    valid_roles = {
        "edge_source",
        "edge_target",
        "aggregate_source",
        "join_left",
        "join_right",
    }
    rows = cur.execute(
        "SELECT m.materialized_row_id, m.source_row_id, m.source_sheet_id, "
        "m.op_id, m.role, mr.id AS materialized_exists, "
        "mr.parent_row_id AS materialized_parent_row_id, "
        "sr.id AS source_exists, sr.sheet_id AS actual_source_sheet_id, "
        "op.id AS op_exists "
        "FROM materialized_row_sources AS m "
        "LEFT JOIN rows AS mr ON mr.id = m.materialized_row_id "
        "LEFT JOIN rows AS sr ON sr.id = m.source_row_id "
        "LEFT JOIN ops AS op ON op.id = m.op_id"
    ).fetchall()
    for row in rows:
        try:
            base = _materialized_row_source_dict(row)
        except (TypeError, ValueError):
            issues.append(
                {
                    "kind": "unparseable_row",
                    "materialized_row_id": row["materialized_row_id"],
                    "source_row_id": row["source_row_id"],
                    "source_sheet_id": row["source_sheet_id"],
                    "op_id": row["op_id"],
                    "role": row["role"],
                }
            )
            continue
        if row["role"] not in valid_roles:
            issues.append({"kind": "invalid_role", **base})
        if row["materialized_exists"] is None:
            issues.append({"kind": "orphaned_materialized_row", **base})
        elif base["role"] == "edge_source":
            parent_row_id = row["materialized_parent_row_id"]
            if parent_row_id is None or int(parent_row_id) != base["source_row_id"]:
                issues.append(
                    {
                        "kind": "edge_parent_row_id_mismatch",
                        "actual_parent_row_id": (
                            int(parent_row_id) if parent_row_id is not None else None
                        ),
                        **base,
                    }
                )
        if row["source_exists"] is None:
            issues.append({"kind": "orphaned_source_row", **base})
        if row["op_exists"] is None:
            issues.append({"kind": "orphaned_op", **base})
        actual_source_sheet_id = row["actual_source_sheet_id"]
        if actual_source_sheet_id is not None and int(actual_source_sheet_id) != int(
            row["source_sheet_id"]
        ):
            issues.append(
                {
                    "kind": "source_sheet_id_mismatch",
                    "actual_source_sheet_id": int(actual_source_sheet_id),
                    **base,
                }
            )
    for materialized_row_id, expected_roles in (
        expected_roles_by_materialized_row or {}
    ).items():
        active_roles = {
            row["role"]
            for row in active_materialized_row_sources(
                cur,
                materialized_row_ids=[int(materialized_row_id)],
            )
        }
        for role in expected_roles:
            if role not in active_roles:
                issues.append(
                    {
                        "kind": "missing_active_membership",
                        "materialized_row_id": int(materialized_row_id),
                        "role": role,
                    }
                )
    return issues


def materialized_row_sources_ref_matches(
    ref: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> bool:
    if ref.get("kind") != "materialized_row_sources":
        return False
    has_embedded_rows = "rows" in ref
    if not has_embedded_rows and ref.get("storage") != "normalized_relation_v1":
        return False
    try:
        normalized = _normalized_materialized_row_sources(rows)
        ref_rows = (
            _normalized_materialized_row_sources(ref.get("rows") or [])
            if has_embedded_rows
            else None
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        ref.get("row_count") == len(normalized)
        and ref.get("sha256") == _materialized_row_sources_hash(normalized)
        and (ref_rows is None or ref_rows == normalized)
    )


def _validate_single_parent_plan(plan: SingleParentChildSheetPlan) -> None:
    if not plan.columns:
        raise ValueError("single-parent child-sheet plan must include columns")
    column_names = [column.name for column in plan.columns]
    if len(set(column_names)) != len(column_names):
        raise ValueError("single-parent child-sheet plan has duplicate columns")
    known_columns = set(column_names)
    for row in plan.rows:
        if not _is_positive_int(row.parent_row_id):
            raise ValueError("single-parent materialized rows require parent_row_id")
        extra = set(row.values) - known_columns
        if extra:
            raise ValueError(
                "single-parent materialized row has values for unknown columns: "
                + ", ".join(sorted(extra))
            )


def _validate_edge_table_plan(plan: EdgeTablePlan) -> None:
    if not plan.columns:
        raise ValueError("edge table plan must include columns")
    column_names = [column.name for column in plan.columns]
    if len(set(column_names)) != len(column_names):
        raise ValueError("edge table plan has duplicate columns")
    known_columns = set(column_names)
    for edge in plan.edges:
        if not _is_positive_int(edge.source_row_id):
            raise ValueError("edge records require source_row_id")
        if edge.target_row_id is not None and not _is_positive_int(edge.target_row_id):
            raise ValueError("edge target_row_id must be a positive integer")
        extra = set(edge.values) - known_columns
        if extra:
            raise ValueError(
                "edge materialized row has values for unknown columns: "
                + ", ".join(sorted(extra))
            )


def _validate_aggregate_sheet_plan(plan: AggregateSheetPlan) -> None:
    if not plan.columns:
        raise ValueError("aggregate sheet plan must include columns")
    column_names = [column.name for column in plan.columns]
    if len(set(column_names)) != len(column_names):
        raise ValueError("aggregate sheet plan has duplicate columns")
    known_columns = set(column_names)
    for row in plan.rows:
        extra = set(row.values) - known_columns
        if extra:
            raise ValueError(
                "aggregate materialized row has values for unknown columns: "
                + ", ".join(sorted(extra))
            )
        if any(
            not _is_positive_int(source_row_id) for source_row_id in row.source_row_ids
        ):
            raise ValueError("aggregate source rows must be positive integers")
        if len(set(int(source_row_id) for source_row_id in row.source_row_ids)) != len(
            row.source_row_ids
        ):
            raise ValueError("aggregate materialized row has duplicate source rows")


def _insert_materialized_row_source(
    cur: sqlite3.Cursor,
    *,
    materialized_row_id: int,
    source_row_id: int,
    op_id: int,
    role: str,
) -> dict[str, Any]:
    source_sheet_id = _source_sheet_id_for_row(cur, source_row_id)
    try:
        cur.execute(
            "INSERT INTO materialized_row_sources "
            "(materialized_row_id, source_row_id, source_sheet_id, op_id, role) "
            "VALUES (?, ?, ?, ?, ?)",
            (materialized_row_id, source_row_id, source_sheet_id, op_id, role),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            "materialized row source membership insert failed "
            f"(materialized_row_id={materialized_row_id}, "
            f"source_row_id={source_row_id}, role={role})"
        ) from exc
    return {
        "materialized_row_id": materialized_row_id,
        "source_row_id": source_row_id,
        "source_sheet_id": source_sheet_id,
        "op_id": op_id,
        "role": role,
    }


def _source_sheet_id_for_row(cur: sqlite3.Cursor, row_id: int) -> int:
    row = cur.execute("SELECT sheet_id FROM rows WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise ValueError(f"source row {row_id} does not exist")
    return int(row["sheet_id"])


def _normalized_materialized_row_sources(
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[int, int, int, str]]:
    return sorted(
        (
            int(row["materialized_row_id"]),
            int(row["source_row_id"]),
            int(row["source_sheet_id"]),
            str(row["role"]),
        )
        for row in rows
    )


def _materialized_row_sources_hash(
    rows: Sequence[tuple[int, int, int, str]],
) -> str:
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
