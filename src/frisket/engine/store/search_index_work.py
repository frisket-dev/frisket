"""Durable, bounded worklist for the rebuildable search sidecar."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchDirtyScope:
    id: int
    sheet_id: int | None
    column_id: int | None
    row_id_start: int | None
    row_id_end: int | None
    scan_cursor: str


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError("search worklist writes require a caller transaction")


def enqueue_dirty_scope(
    db: sqlite3.Connection,
    *,
    sheet_id: int | None = None,
    column_id: int | None = None,
    row_id_start: int | None = None,
    row_id_end: int | None = None,
) -> int:
    """Append one invalidated scope inside the authoritative write transaction."""

    _require_transaction(db)
    if column_id is not None and sheet_id is None:
        raise ValueError("column scope requires sheet_id")
    if (row_id_start is None) != (row_id_end is None):
        raise ValueError("row range requires both bounds")
    if row_id_start is not None and row_id_start > row_id_end:
        raise ValueError("row range is reversed")
    if sheet_id is None:
        # A fresh global repair supersedes every older scope. Deleting first is
        # safe because the replacement ID fences a worker holding an old cursor.
        db.execute("DELETE FROM search_dirty_scopes")
    elif column_id is None and row_id_start is None:
        db.execute("DELETE FROM search_dirty_scopes WHERE sheet_id=?", (sheet_id,))
    elif row_id_start is None:
        db.execute(
            "DELETE FROM search_dirty_scopes WHERE sheet_id=? AND column_id=?",
            (sheet_id, column_id),
        )
    else:
        db.execute(
            "DELETE FROM search_dirty_scopes WHERE sheet_id=? "
            "AND column_id IS ? AND row_id_start IS ? AND row_id_end IS ?",
            (sheet_id, column_id, row_id_start, row_id_end),
        )
    cursor = db.execute(
        "INSERT INTO search_dirty_scopes "
        "(sheet_id,column_id,row_id_start,row_id_end) VALUES (?,?,?,?)",
        (sheet_id, column_id, row_id_start, row_id_end),
    )
    return int(cursor.lastrowid)


def latest_revision(db: sqlite3.Connection) -> int:
    """Return the monotonic high-water mark, including acknowledged work."""

    row = db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='search_dirty_scopes'"
    ).fetchone()
    return 0 if row is None else int(row[0])


def read_dirty_scopes(db: sqlite3.Connection, *, limit: int) -> list[SearchDirtyScope]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = db.execute(
        "SELECT id,sheet_id,column_id,row_id_start,row_id_end,scan_cursor "
        "FROM search_dirty_scopes ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        SearchDirtyScope(
            id=int(row[0]),
            sheet_id=None if row[1] is None else int(row[1]),
            column_id=None if row[2] is None else int(row[2]),
            row_id_start=None if row[3] is None else int(row[3]),
            row_id_end=None if row[4] is None else int(row[4]),
            scan_cursor=str(row[5]),
        )
        for row in rows
    ]


def advance_dirty_scope(
    db: sqlite3.Connection, *, scope_id: int, expected_cursor: str, scan_cursor: str
) -> bool:
    _require_transaction(db)
    if not scan_cursor:
        raise ValueError("scan cursor must not be empty")
    changed = db.execute(
        "UPDATE search_dirty_scopes SET scan_cursor=? WHERE id=? AND scan_cursor=?",
        (scan_cursor, scope_id, expected_cursor),
    )
    return changed.rowcount == 1


def ack_dirty_scope(
    db: sqlite3.Connection, *, scope_id: int, expected_cursor: str
) -> bool:
    _require_transaction(db)
    changed = db.execute(
        "DELETE FROM search_dirty_scopes WHERE id=? AND scan_cursor=?",
        (scope_id, expected_cursor),
    )
    return changed.rowcount == 1
