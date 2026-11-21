"""Let human edits outrank later run results and Reapply copies.

Only read views and the rebuildable current-cell cache change. Edits, results
and history stay exactly as saved; cells whose cached head hid a surviving
edit are invalidated so the bounded repair resolves them again. Both steps
apply the same payload: a project at either earlier stamp reinstalls the
runtime change views and repairs the cells they now resolve differently.
"""

from __future__ import annotations
import sqlite3
from frisket.engine.store.change_schema import CHANGE_SCHEMA_SQL
from frisket.engine.store.schema import BundleSchemaMismatch, SCHEMA_DIGEST
from frisket.upgrades.project._step import execute_ddl
from frisket.upgrades.project.citations_migration import CITATIONS_TO_DIGEST


# Edits outrank later run results.
EDIT_PRECEDENCE_TO_DIGEST = "frisket.schema.v1:bd2b8aa51fcac3d5a7ba3753630a4fcd"
# Edits also outrank later Reapply copies. Pinned endpoint: the payload
# installs the runtime change-schema views, so a later schema change must
# extend these steps explicitly.
COPY_PRECEDENCE_TO_DIGEST = "frisket.schema.v1:6a94206a03cff0c69e0188ff74317800"

_CHANGE_VIEWS = (
    "generated_result_candidates",
    "effective_current_cells",
    "dirty_cell_heads",
    "canonical_cell_heads",
    "cell_contributions",
)


def _invalidate_hidden_edits(db: sqlite3.Connection) -> None:
    targets = db.execute(
        "SELECT edit.column_id,col.sheet_id,MIN(edit.row_id),MAX(edit.row_id) "
        "FROM edits edit JOIN current_cells head ON head.column_id=edit.column_id "
        "AND head.row_id=edit.row_id AND head.origin_kind!='manual_edit' "
        "JOIN columns col ON col.id=edit.column_id GROUP BY edit.column_id,col.sheet_id"
    ).fetchall()
    for column_id, sheet_id, start, end in targets:
        partners = db.execute(
            "SELECT source_column_id,text_column_id FROM document_text_column_roles "
            "WHERE source_column_id=? OR text_column_id=?",
            (column_id, column_id),
        ).fetchone()
        for related in sorted(set(partners or (column_id,))):
            db.execute(
                "INSERT INTO search_dirty_scopes(sheet_id,column_id,row_id_start,"
                "row_id_end) VALUES (?,?,?,?)",
                (sheet_id, related, start, end),
            )
        revision = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='search_dirty_scopes'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO cell_authority_revisions(column_id,revision) VALUES (?,?) "
            "ON CONFLICT(column_id) DO UPDATE SET revision=excluded.revision",
            (column_id, revision),
        )
        # One repair range per column: overlapping ranges are merged first.
        overlap = db.execute(
            "SELECT MIN(row_id_start),MAX(row_id_end) FROM current_cell_repair "
            "WHERE column_id=? AND row_id_start<=? AND row_id_end>=?",
            (column_id, end + 1, start - 1),
        ).fetchone()
        if overlap[0] is not None:
            start, end = min(start, overlap[0]), max(end, overlap[1])
        db.execute(
            "DELETE FROM current_cell_repair WHERE column_id=? "
            "AND row_id_start<=? AND row_id_end>=?",
            (column_id, end, start),
        )
        db.execute(
            "INSERT INTO current_cell_repair(column_id,row_id_start,row_id_end,cursor) "
            "VALUES (?,?,?,?)",
            (column_id, start, end, start - 1),
        )


def apply_edit_precedence(db: sqlite3.Connection) -> None:
    if SCHEMA_DIGEST != CITATIONS_TO_DIGEST:
        raise BundleSchemaMismatch(
            "Edit precedence migration endpoint needs updating for this build"
        )
    for view in _CHANGE_VIEWS:
        db.execute(f"DROP VIEW IF EXISTS {view}")
    execute_ddl(db, CHANGE_SCHEMA_SQL)
    _invalidate_hidden_edits(db)
