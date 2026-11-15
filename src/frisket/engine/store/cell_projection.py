"""Current-cell cache maintenance. Dirty scopes always read canonical winners.

Small writes update heads in their transaction. Large visibility changes only
invalidate ranges; bounded repair can resume after interruption without making
old cached values visible or acknowledging a replacement work item.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

SMALL_WRITE_LIMIT = 512
HEAD_COLUMNS = (
    "column_id,row_id,retrieval_revision,origin_kind,origin_op_id,origin_run_id,"
    "base_producer_id,validity,inline_value_kind,inline_value,outcome"
)


def uses_ordered_changes(db: sqlite3.Connection) -> bool:
    # Old schema migration steps still use the historical projector until the
    # cutover installs this authority. This is not a per-project runtime mode.
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='canonical_cell_heads'"
        ).fetchone()
        is not None
    )


def project_region(db: sqlite3.Connection, column_id: int, start: int, end: int) -> int:
    params = (column_id, start, end)
    db.execute(
        "DELETE FROM current_cells WHERE column_id=? AND row_id BETWEEN ? AND ?", params
    )
    db.execute(
        f"INSERT INTO current_cells ({HEAD_COLUMNS}) SELECT {HEAD_COLUMNS} "
        "FROM canonical_cell_heads WHERE column_id=? AND row_id BETWEEN ? AND ?",
        params,
    )
    return int(db.execute("SELECT changes()").fetchone()[0])


def invalidate_region(
    db: sqlite3.Connection, column_id: int, start: int, end: int
) -> None:
    if not db.in_transaction:
        raise RuntimeError("projection invalidation requires the mutation transaction")
    overlap = db.execute(
        "SELECT MIN(row_id_start),MAX(row_id_end) FROM current_cell_repair "
        "WHERE column_id=? AND row_id_start<=? AND row_id_end>=?",
        (column_id, end + 1, start - 1),
    ).fetchone()
    if overlap[0] is not None:
        start, end = min(start, int(overlap[0])), max(end, int(overlap[1]))
    db.execute(
        "DELETE FROM current_cell_repair WHERE column_id=? AND row_id_start<=? AND row_id_end>=?",
        (column_id, end, start),
    )
    db.execute(
        "INSERT INTO current_cell_repair(column_id,row_id_start,row_id_end) VALUES (?,?,?)",
        (column_id, start, end),
    )


def refresh_ordered_cells(
    db: sqlite3.Connection,
    column_ids: Collection[int],
    row_ids: Collection[int] | None,
    *,
    retrieval_revision: int,
) -> int:
    total = 0
    for column_id in column_ids:
        # This is invalidation of authoritative facts, not acknowledgement of a
        # cache refresh. It happens even if the winning pointer stays unchanged.
        db.execute(
            "INSERT INTO cell_authority_revisions(column_id,revision) VALUES (?,?) "
            "ON CONFLICT(column_id) DO UPDATE SET revision=excluded.revision",
            (column_id, retrieval_revision),
        )
        if row_ids is None:
            scope = db.execute(
                "SELECT MIN(row.id),MAX(row.id),COUNT(*) FROM rows row "
                "JOIN columns col ON col.sheet_id=row.sheet_id WHERE col.id=?",
                (column_id,),
            ).fetchone()
            if not scope[2]:
                continue
            start, end, count = map(int, scope)
        else:
            start, end, count = min(row_ids), max(row_ids), len(row_ids)
        if count > SMALL_WRITE_LIMIT:
            invalidate_region(db, column_id, start, end)
        elif row_ids is not None:
            for row_id in row_ids:
                total += project_region(db, column_id, row_id, row_id)
        else:
            total += project_region(db, column_id, start, end)
    return total


def invalidate_run_scope(db: sqlite3.Connection, run_id: int, column_id: int) -> None:
    """Start/close a run without fetching its fixed row cohort into Python."""
    from .current_cells import _enqueue_region

    scope = db.execute(
        "SELECT MIN(row_id),MAX(row_id),COUNT(*) FROM run_rows WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not scope[2]:
        return
    revision = _enqueue_region(db, column_ids=[column_id], row_ids=None)
    db.execute(
        "INSERT INTO cell_authority_revisions(column_id,revision) VALUES (?,?) "
        "ON CONFLICT(column_id) DO UPDATE SET revision=excluded.revision",
        (column_id, revision),
    )
    if scope[2] <= SMALL_WRITE_LIMIT:
        for row in db.execute(
            "SELECT row_id FROM run_rows WHERE run_id=?", (run_id,)
        ).fetchall():
            project_region(db, column_id, int(row[0]), int(row[0]))
    else:
        invalidate_region(db, column_id, int(scope[0]), int(scope[1]))


def repair_current_cells(project: object, *, limit: int = SMALL_WRITE_LIMIT) -> int:
    """Repair at most one bounded row window in one short write transaction."""
    db = project.db
    if db.in_transaction:
        raise RuntimeError("bounded repair owns its transaction")
    if not 1 <= limit <= SMALL_WRITE_LIMIT:
        raise ValueError(f"repair limit must be between 1 and {SMALL_WRITE_LIMIT}")
    db.execute("BEGIN IMMEDIATE")
    try:
        work = db.execute(
            "SELECT * FROM current_cell_repair ORDER BY id LIMIT 1"
        ).fetchone()
        if work is None:
            db.commit()
            return 0
        start = max(work["row_id_start"], work["cursor"] + 1)
        rows = db.execute(
            "SELECT row.id FROM rows row JOIN columns col ON col.sheet_id=row.sheet_id "
            "WHERE col.id=? AND row.id BETWEEN ? AND ? ORDER BY row.id LIMIT ?",
            (work["column_id"], start, work["row_id_end"], limit),
        ).fetchall()
        end = int(rows[-1][0]) if len(rows) == limit else int(work["row_id_end"])
        project_region(db, work["column_id"], start, end)
        if end >= work["row_id_end"]:
            db.execute("DELETE FROM current_cell_repair WHERE id=?", (work["id"],))
        else:
            db.execute(
                "UPDATE current_cell_repair SET cursor=? WHERE id=? AND cursor=?",
                (end, work["id"], work["cursor"]),
            )
        db.commit()
        return len(rows)
    except BaseException:
        db.rollback()
        raise
