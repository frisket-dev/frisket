"""Indexed descriptor contributions; values remain in operation undo records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any
from .transactions import require_transaction, write_transaction


DESCRIPTOR_FIELDS = {
    "type": "column_types",
    "format": "column_formats",
    "semantic_type": "column_semantic_types",
}

DESCRIPTOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS column_descriptor_changes (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  property TEXT NOT NULL CHECK(property IN ('type','format','semantic_type')),
  PRIMARY KEY(column_id,property,op_id)
);
CREATE INDEX IF NOT EXISTS idx_descriptor_change_op ON column_descriptor_changes(op_id);
"""


def record_descriptor_changes(
    db: sqlite3.Connection, op_id: int, info: Mapping[str, Any]
) -> None:
    """Index existing before/after facts inside the mutation transaction."""
    require_transaction(db, "descriptor change")
    for property_name, field in DESCRIPTOR_FIELDS.items():
        before = info.get(field, {})
        for column_id in info.get(f"{field}_after", {}):
            if column_id not in before:
                raise ValueError(f"descriptor change lacks {field} before value")
            db.execute(
                "INSERT OR IGNORE INTO column_descriptor_changes "
                "(op_id,column_id,property) VALUES (?,?,?)",
                (op_id, int(column_id), property_name),
            )


def _effective_properties(db: sqlite3.Connection, column_id: int):
    # Include inactive Changes to retain the first before-value as the baseline.
    # A later active contribution wins even when an older change is reverted.
    rows = db.execute(
        "WITH ordered AS (SELECT change.property,op.id,op.status,op.undo_info, "
        "FIRST_VALUE(op.undo_info) OVER (PARTITION BY change.property "
        "ORDER BY op.change_order) AS baseline_info, "
        "ROW_NUMBER() OVER (PARTITION BY change.property "
        "ORDER BY (op.status='applied') DESC,op.change_order DESC) AS rank "
        "FROM column_descriptor_changes change JOIN ops op ON op.id=change.op_id "
        "WHERE change.column_id=? AND op.change_order IS NOT NULL) "
        "SELECT property,id,status,undo_info,baseline_info FROM ordered WHERE rank=1",
        (column_id,),
    )
    for property_name, op_id, status, info_json, baseline_json in rows:
        field = DESCRIPTOR_FIELDS[property_name]
        active = status == "applied"
        info = json.loads(info_json if active else baseline_json)
        yield (
            property_name,
            info[f"{field}_after" if active else field][str(column_id)],
            int(op_id) if active else 0,
        )


def descriptor_identity(db_or_project: Any, column_id: int) -> dict[str, Any] | None:
    """Read descriptor values and immutable owners in the caller's snapshot."""
    db = getattr(db_or_project, "db", db_or_project)
    row = db.execute(
        "SELECT col.type,col.format,col.semantic_type FROM columns col "
        "JOIN sheets sheet ON sheet.id=col.sheet_id "
        "WHERE col.id=? AND col.active=1 AND sheet.hidden=0",
        (column_id,),
    ).fetchone()
    if row is None:
        return None
    values = dict(zip(DESCRIPTOR_FIELDS, row, strict=True))
    owners = {field: 0 for field in DESCRIPTOR_FIELDS}
    for field, value, owner in _effective_properties(db, column_id):
        values[field], owners[field] = value, owner
    return {"column_id": column_id, **values, "owners": owners}


def project_column_descriptors(db: sqlite3.Connection, column_ids) -> None:
    """Project effective descriptor fields without overwriting later Changes."""
    require_transaction(db, "descriptor projection")
    for column_id in set(column_ids):
        for field, value, _owner in _effective_properties(db, int(column_id)):
            # Field names come exclusively from DESCRIPTOR_FIELDS.
            db.execute(f"UPDATE columns SET {field}=? WHERE id=?", (value, column_id))


def set_descriptor(
    project,
    column_id,
    property_name,
    value,
    *,
    op_id=None,
    initialize=False,
    commit=True,
):
    """Set a descriptor through its owner, or explicitly initialize a new schema."""
    if property_name not in DESCRIPTOR_FIELDS:
        raise ValueError("unknown column descriptor property")
    db = project.db
    with write_transaction(db, commit=commit, what="column descriptor change"):
        column = db.execute(
            f"SELECT {property_name} FROM columns WHERE id=?", (column_id,)
        ).fetchone()
        if column is None:
            raise KeyError(f"no column {column_id}")
        if initialize:
            if op_id is not None:
                raise ValueError("initial descriptor cannot also specify an owner")
            if db.execute(
                "SELECT 1 FROM column_descriptor_changes WHERE column_id=? UNION ALL SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
                (column_id, column_id),
            ).fetchone():
                raise ValueError(
                    "an existing column descriptor requires a Change owner"
                )
            db.execute(
                f"UPDATE columns SET {property_name}=? WHERE id=?", (value, column_id)
            )
        else:
            from .changes import start_change, ChangeStore
            from .op_log import append_op, set_undo_info

            if op_id is None:
                op_id = append_op(
                    project,
                    "column.descriptor",
                    {"column_id": column_id, "property": property_name},
                    label=f"change column {property_name}",
                    commit=False,
                )
                start_change(db, op_id)
            else:
                start_change(db, op_id, writing=True)
                ChangeStore(project).require_publication(op_id)
            info = json.loads(
                db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()[
                    0
                ]
                or "{}"
            )
            field = DESCRIPTOR_FIELDS[property_name]
            info.setdefault(field, {}).setdefault(str(column_id), column[0])
            info.setdefault(f"{field}_after", {})[str(column_id)] = value
            set_undo_info(project, op_id, info, commit=False)
            project_column_descriptors(db, [column_id])
        from .current_cells import refresh_current_cells

        refresh_current_cells(db, column_ids=[column_id])
