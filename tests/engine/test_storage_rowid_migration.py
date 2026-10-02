"""Data-preserving migration from index-organized to rowid cell tables."""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY


_PRIOR_DIGEST = "frisket.schema.v1:348c367f3a24a414ba6f1e612e40ea15"
_CELLS = "row_id,column_id,value,producer_id"
_CURRENT = (
    "column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
    "base_producer_id,validity"
)


def _rows(db: sqlite3.Connection, table: str, fields: str, order: str) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            f"SELECT {fields} FROM {table} ORDER BY {order}"
        ).fetchall()
    ]


def _index(db: sqlite3.Connection, name: str) -> tuple:
    return next(
        row for row in db.execute("PRAGMA index_list(current_cells)") if row[1] == name
    )


def _replace_with_without_rowid_tables(db: sqlite3.Connection) -> None:
    """Make a current-schema bundle that predates only the physical repack."""

    db.execute("PRAGMA foreign_keys=OFF")
    db.executescript(
        """
        CREATE TABLE cells_without_rowid (
          row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
          column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
          value TEXT,
          producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
          PRIMARY KEY (row_id,column_id)
        ) WITHOUT ROWID;
        CREATE TABLE current_cells_without_rowid (
          column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
          row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
          value TEXT,
          origin_kind TEXT NOT NULL CHECK (
            origin_kind IN ('source_cell', 'run_result', 'manual_edit')
          ),
          origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,
          origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
          base_producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
          validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),
          PRIMARY KEY (column_id,row_id),
          CHECK (
            (
              origin_kind='source_cell'
              AND origin_op_id IS NULL
              AND origin_run_id IS NULL
            )
            OR (
              origin_kind='run_result'
              AND origin_op_id IS NOT NULL
              AND origin_run_id IS NOT NULL
              AND base_producer_id IS NULL
            )
            OR (
              origin_kind='manual_edit'
              AND origin_op_id IS NOT NULL
              AND origin_run_id IS NULL
              AND base_producer_id IS NULL
            )
          )
        ) WITHOUT ROWID;
        INSERT INTO cells_without_rowid(row_id,column_id,value,producer_id)
          SELECT row_id,column_id,value,producer_id FROM cells;
        INSERT INTO current_cells_without_rowid(
          column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,
          base_producer_id,validity
        ) SELECT
          column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,
          base_producer_id,validity
        FROM current_cells;
        DROP TABLE cells;
        DROP TABLE current_cells;
        ALTER TABLE cells_without_rowid RENAME TO cells;
        ALTER TABLE current_cells_without_rowid RENAME TO current_cells;
        CREATE INDEX idx_cells_column ON cells(column_id,row_id);
        CREATE INDEX idx_current_cells_row ON current_cells(row_id);
        CREATE INDEX idx_current_cells_column_row ON current_cells(column_id,row_id);
        """
    )
    db.execute(
        "UPDATE meta SET value=? WHERE key=?", (_PRIOR_DIGEST, SCHEMA_DIGEST_META_KEY)
    )
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")


def _prior_bundle(tmp_path):
    path = tmp_path / "prior-rowid-layout.frisket"
    project = Project.create(path, name="Prior rowid layout")
    sheet_id = project.add_sheet("Ledger")
    title_id = project.add_column(sheet_id, "title")
    amount_id = project.add_column(sheet_id, "amount", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [{"title": "first", "amount": 4}, {"title": "second", "amount": 9}],
        {"title": title_id, "amount": amount_id},
    )
    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": title_id, "value": "edited"}]
    )
    # Exercise SQL NULL values as well as JSON `null` values. These rows are
    # copied directly by the physical migration; it must not recalculate them.
    project.db.execute(
        "UPDATE cells SET value=NULL WHERE row_id=? AND column_id=?",
        (row_ids[1], amount_id),
    )
    project.db.execute(
        "UPDATE current_cells SET value=NULL,validity='missing' "
        "WHERE row_id=? AND column_id=?",
        (row_ids[1], amount_id),
    )
    project.db.commit()
    before_cells = _rows(project.db, "cells", _CELLS, "row_id,column_id")
    before_current = _rows(project.db, "current_cells", _CURRENT, "column_id,row_id")
    project.close()

    with sqlite3.connect(path / "project.db") as db:
        _replace_with_without_rowid_tables(db)
    return path, sheet_id, title_id, row_ids, before_cells, before_current


def test_rowid_layout_migration_copies_values_provenance_constraints_and_plans(
    tmp_path,
) -> None:
    path, sheet_id, title_id, row_ids, before_cells, before_current = _prior_bundle(
        tmp_path
    )

    migrated = Project(path)
    assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert _rows(migrated.db, "cells", _CELLS, "row_id,column_id") == before_cells
    assert (
        _rows(migrated.db, "current_cells", _CURRENT, "column_id,row_id")
        == before_current
    )
    assert migrated.get_values(sheet_id, title_id) == {
        row_ids[0]: "edited",
        row_ids[1]: "second",
    }
    assert migrated.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.db.execute("SELECT rowid FROM cells LIMIT 1").fetchone() is not None
    assert _index(migrated.db, "idx_current_cells_column_row")[2] == 1
    assert [
        row[2]
        for row in migrated.db.execute("PRAGMA index_info(idx_current_cells_row)")
    ] == ["row_id", "column_id"]
    with pytest.raises(sqlite3.IntegrityError):
        migrated.db.execute(
            "INSERT INTO cells(row_id,column_id,value) VALUES (?,?,?)",
            (row_ids[0], title_id, '"duplicate"'),
        )
    migrated.db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        migrated.db.execute(
            "INSERT INTO current_cells("
            "column_id,row_id,value,origin_kind,validity"
            ") VALUES (?,?,?,'source_cell','valid')",
            (title_id, row_ids[0], '"duplicate"'),
        )
    migrated.db.rollback()
    plan = "\n".join(
        row[3]
        for row in migrated.db.execute(
            "EXPLAIN QUERY PLAN SELECT row_id FROM current_cells "
            "INDEXED BY idx_current_cells_column_row "
            "WHERE column_id=? ORDER BY row_id",
            (title_id,),
        )
    )
    assert "COVERING INDEX idx_current_cells_column_row" in plan
    row_plan = "\n".join(
        row[3]
        for row in migrated.db.execute(
            "EXPLAIN QUERY PLAN SELECT column_id,value FROM current_cells "
            "WHERE row_id=? ORDER BY column_id",
            (row_ids[0],),
        )
    )
    assert "idx_current_cells_row" in row_plan
    assert "TEMP B-TREE" not in row_plan
    migrated.close()


def test_rowid_layout_migration_rolls_back_swap_and_stamp_together(tmp_path) -> None:
    path, _sheet_id, _title_id, _row_ids, before_cells, before_current = _prior_bundle(
        tmp_path
    )
    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER interrupt_rowid_layout_stamp BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' AND NEW.value<>OLD.value BEGIN "
            "SELECT RAISE(ABORT,'rowid migration interrupted'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="rowid migration interrupted"):
        Project(path)

    with sqlite3.connect(path / "project.db") as db:
        assert db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone() == (_PRIOR_DIGEST,)
        assert _rows(db, "cells", _CELLS, "row_id,column_id") == before_cells
        assert (
            _rows(db, "current_cells", _CURRENT, "column_id,row_id") == before_current
        )
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='cells'"
        ).fetchone()[0]
        assert "WITHOUT ROWID" in table_sql
