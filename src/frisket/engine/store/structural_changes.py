"""Shared revision invalidation for changes in sheet row membership."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .current_cells import refresh_current_cells


def note_row_membership_change(
    db: sqlite3.Connection,
    sheet_ids: Iterable[int],
    *,
    already_refreshed_columns: Iterable[int] = (),
) -> None:
    """Invalidate every affected column once, including empty source columns."""
    if not db.in_transaction:
        raise RuntimeError("membership changes require a caller-owned transaction")
    columns = {
        int(row[0])
        for sheet_id in set(sheet_ids)
        for row in db.execute("SELECT id FROM columns WHERE sheet_id=?", (sheet_id,))
    } - set(already_refreshed_columns)
    refresh_current_cells(db, column_ids=columns)


STRUCTURAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS structural_changes (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('row','column','sheet')),
  object_id INTEGER NOT NULL,
  before_visible INTEGER NOT NULL CHECK(before_visible IN (0,1)),
  after_visible INTEGER NOT NULL CHECK(after_visible IN (0,1)),
  PRIMARY KEY(kind,object_id,op_id)
);
CREATE INDEX IF NOT EXISTS idx_structural_change_op ON structural_changes(op_id);
"""

# These are existing undo facts, not a second authored mutation language.
_MEMBERSHIP_FIELDS = {
    "created_rows": ("row", 0, 1),
    "deleted_rows": ("row", 1, 0),
    "restored_rows": ("row", 0, 1),
    "created_columns": ("column", 0, 1),
    "deleted_columns": ("column", 1, 0),
    "created_sheets": ("sheet", 0, 1),
    "deleted_sheets": ("sheet", 1, 0),
}


def record_structural_changes(db, op_id, info):
    import json

    if not db.in_transaction:
        raise RuntimeError("structural changes require a caller-owned transaction")
    for field, (kind, before, after) in _MEMBERSHIP_FIELDS.items():
        if info.get(field):
            db.execute(
                "INSERT OR IGNORE INTO structural_changes "
                "(op_id,kind,object_id,before_visible,after_visible) "
                "SELECT ?,?,value,?,? FROM json_each(?)",
                (op_id, kind, before, after, json.dumps(info[field])),
            )

    # Streaming imports already have a durable compact membership ledger.
    # Register committed rows set-wise, never expanding them into Python/JSON.
    session = info.get("created_rows_import_session_id")
    if session is not None:
        db.execute(
            "INSERT OR IGNORE INTO structural_changes (op_id,kind,object_id,before_visible,after_visible) "
            "SELECT ?, 'row', member.row_id, 0, 1 FROM import_session_rows member "
            "JOIN import_sessions session ON session.id=member.session_id "
            "WHERE member.session_id=? AND session.op_id=?",
            (op_id, session, op_id),
        )
    replaced_session = info.get("replaced_rows_import_session_id")
    if replaced_session is not None:
        db.execute(
            "INSERT OR IGNORE INTO structural_changes (op_id,kind,object_id,before_visible,after_visible) "
            "SELECT ?, 'row', source.row_id, 1, 0 FROM import_session_pdf_sources source "
            "JOIN import_sessions session ON session.id=source.session_id "
            "WHERE source.session_id=? AND source.replaced=1 AND session.op_id=?",
            (op_id, replaced_session, op_id),
        )


def project_structural_changes(db, op_id):
    """Project only the touched identities from their winning active Change."""
    if not db.in_transaction:
        raise RuntimeError("structural projection requires a caller-owned transaction")
    # Scope stays in SQLite, including a million-row import. A structural edit
    # never replays or traverses downstream content.
    for kind, table, field in (
        ("row", "rows", "hidden"),
        ("column", "columns", "active"),
        ("sheet", "sheets", "hidden"),
    ):
        visible = "visible" if field == "active" else "1-visible"
        hidden_update = (
            ",hidden=CASE WHEN (SELECT visible FROM winners WHERE winners.object_id=columns.id)=0 THEN 1 ELSE hidden END"
            if kind == "column"
            else ""
        )
        db.execute(
            "WITH touched AS (SELECT object_id FROM structural_changes WHERE op_id=? AND kind=?), "
            "ranked AS (SELECT effect.object_id,effect.after_visible,owner.status, "
            "FIRST_VALUE(effect.before_visible) OVER (PARTITION BY effect.object_id ORDER BY owner.change_order) AS baseline, "
            "ROW_NUMBER() OVER (PARTITION BY effect.object_id ORDER BY (owner.status='applied') DESC,owner.change_order DESC) AS rank "
            "FROM touched JOIN structural_changes effect USING(object_id) JOIN ops owner ON owner.id=effect.op_id "
            "WHERE effect.kind=? AND owner.change_order IS NOT NULL), "
            "winners AS (SELECT object_id,CASE WHEN status='applied' THEN after_visible ELSE baseline END AS visible "
            "FROM ranked WHERE rank=1) "
            f"UPDATE {table} SET {field}=(SELECT {visible} FROM winners WHERE winners.object_id={table}.id){hidden_update} "
            "WHERE id IN (SELECT object_id FROM winners)",
            (op_id, kind, kind),
        )
