"""Active-name cutover preserves column IDs, child data and hidden live outputs."""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.bundle_open import (
    _ACTIVE_COLUMNS_TO_DIGEST,
    _migrate_active_columns,
)
from frisket.engine.store.schema import (
    BundleSchemaMismatch,
    SCHEMA,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
)
from tests.engine.test_bundle_schema_fence import _without_typed_values

_PRIOR_DIGEST = "frisket.schema.v1:f078f2bc57411d372468936618f2f884"


def _prior_bundle(tmp_path):
    schema = (
        _without_typed_values(SCHEMA)
        .replace(
            "  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1) AND (active=1 OR hidden=1)),\n",
            "",
        )
        .replace(
            "  created_at TEXT NOT NULL DEFAULT (datetime('now'))\n);\n"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_columns_active_name\n"
            "  ON columns(sheet_id,name) WHERE active=1;",
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')),\n"
            "  UNIQUE(sheet_id, name)\n);",
        )
    )
    path = tmp_path / "prior.frisket"
    path.mkdir()
    with sqlite3.connect(path / "project.db") as db:
        db.executescript(schema)
        db.execute(
            "INSERT INTO meta(key,value) VALUES (?,?)",
            (SCHEMA_DIGEST_META_KEY, _PRIOR_DIGEST),
        )
        db.execute("INSERT INTO sheets(id,name) VALUES (1,'Sheet')")
        db.execute("INSERT INTO rows(id,sheet_id) VALUES (1,1)")
        db.executemany(
            "INSERT INTO columns(id,sheet_id,name,hidden) VALUES (?,1,?,?)",
            [
                (1, "undone", 1),
                (2, "discarded", 1),
                (3, "revived", 1),
                (4, "hidden-live", 1),
                (5, "visible", 0),
            ],
        )
        db.executemany(
            "INSERT INTO ops(id,kind,status,undo_info) VALUES (?,'create',?,?)",
            [
                (1, "undone", '{"created_columns":[1]}'),
                (2, "discarded", '{"created_columns":[2,3]}'),
                (3, "applied", '{"created_columns":[3,5]}'),
            ],
        )
        db.execute("INSERT INTO cells(row_id,column_id,value) VALUES (1,1,'\"old\"')")
        db.execute(
            "INSERT INTO edits(op_id,row_id,column_id,value) VALUES (1,1,1,'\"edited\"')"
        )
        db.execute(
            "INSERT INTO current_cells(column_id,row_id,value,origin_kind,validity) "
            "VALUES (1,1,'\"old\"','source_cell','valid')"
        )
        db.execute(
            "INSERT INTO runs(id,op_id,sheet_id,action_kind) VALUES (1,1,1,'create')"
        )
        db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value,outcome) "
            "VALUES (1,1,1,'\"result\"','ok')"
        )
    return path


def test_upgrade_frees_only_undone_names_and_preserves_history(tmp_path):
    path = _prior_bundle(tmp_path)
    project = Project(path)
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT id,name,hidden,active FROM columns ORDER BY id"
        )
    ] == [
        (1, "undone", 1, 0),
        (2, "discarded", 1, 0),
        (3, "revived", 1, 1),
        (4, "hidden-live", 1, 1),
        (5, "visible", 0, 1),
    ]
    assert project.db.execute(
        "SELECT row_id,column_id,value_kind,value,producer_id FROM cells"
    ).fetchone()[:] == (1, 1, "text", "old", None)
    assert project.db.execute(
        "SELECT op_id,row_id,column_id,value_kind,value FROM edits"
    ).fetchone()[:] == (1, 1, 1, "text", "edited")
    assert project.db.execute(
        "SELECT run_id,row_id,column_id,value_kind,value,outcome FROM results"
    ).fetchone()[:] == (1, 1, 1, "text", "result", "ok")
    assert project.db.execute(
        "SELECT column_id,row_id,origin_kind,validity FROM current_cells"
    ).fetchone()[:] == (1, 1, "source_cell", "valid")
    assert project.db.execute(
        "SELECT value_kind,value FROM current_cell_values"
    ).fetchone()[:] == ("text", "old")
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 3
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert project.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert project.db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    fresh = project.add_column(1, "undone")
    assert fresh != 1
    for name in ("revived", "hidden-live", "visible"):
        with pytest.raises(sqlite3.IntegrityError):
            project.db.execute(
                "INSERT INTO columns(sheet_id,name) VALUES (1,?)", (name,)
            )
    project.db.rollback()
    project.db.execute("DELETE FROM search_dirty_scopes")
    project.db.execute("UPDATE columns SET name='changed' WHERE id=?", (fresh,))
    assert (
        project.db.execute(
            "SELECT column_id FROM search_dirty_scopes WHERE column_id=?", (fresh,)
        ).fetchone()
        is not None
    )
    project.db.commit()
    project.close()
    reopened = Project(path)
    assert (
        reopened.db.execute("SELECT active FROM columns WHERE id=1").fetchone()[0] == 0
    )
    assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_upgrade_rolls_back_table_swap_and_restores_connection_settings(tmp_path):
    path = _prior_bundle(tmp_path)
    with sqlite3.connect(path / "project.db") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "CREATE TRIGGER interrupt_column_stamp BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' BEGIN "
            "SELECT RAISE(ABORT,'migration interrupted'); END"
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
            _migrate_active_columns(db)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
        assert "active" not in {
            row[1] for row in db.execute("PRAGMA table_info(columns)")
        }
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == _PRIOR_DIGEST
        )
        assert db.execute("SELECT count(*) FROM cells").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute("DROP TRIGGER interrupt_column_stamp")
        db.commit()
        _migrate_active_columns(db)
        assert db.execute("SELECT active FROM columns WHERE id=1").fetchone()[0] == 0


@pytest.mark.parametrize("same_child_table", [False, True])
def test_upgrade_does_not_audit_unrelated_foreign_keys(tmp_path, same_child_table):
    path = _prior_bundle(tmp_path)
    with sqlite3.connect(path / "project.db") as db:
        if same_child_table:
            db.execute("INSERT INTO cells(row_id,column_id) VALUES (999,5)")
        else:
            db.execute("UPDATE rows SET parent_row_id=999 WHERE id=1")
        db.commit()
        assert db.execute("PRAGMA foreign_key_check").fetchone() is not None
        _migrate_active_columns(db)
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == _ACTIVE_COLUMNS_TO_DIGEST
        )
        # Migration neither repairs nor deletes unrelated historical data.
        assert db.execute("PRAGMA foreign_key_check").fetchone() is not None


def test_upgrade_reports_column_relationship_problem_without_losing_data(tmp_path):
    path = _prior_bundle(tmp_path)
    with sqlite3.connect(path / "project.db") as db:
        db.execute("UPDATE cells SET column_id=999")
        db.commit()
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(BundleSchemaMismatch, match="rolled back.*keep the project"):
            _migrate_active_columns(db)
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == _PRIOR_DIGEST
        )
        assert db.execute("SELECT column_id FROM cells").fetchone()[0] == 999
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("migrated", [False, True])
def test_inactive_columns_cannot_be_visible(tmp_path, migrated):
    project = (
        Project(_prior_bundle(tmp_path))
        if migrated
        else Project.create(tmp_path / "fresh.frisket")
    )
    try:
        sheet_id = project.add_sheet("invariant")
        column_id = project.add_column(sheet_id, "visible")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            project.db.execute("UPDATE columns SET active=0 WHERE id=?", (column_id,))
        project.db.rollback()
        project.db.execute(
            "UPDATE columns SET active=0,hidden=1 WHERE id=?", (column_id,)
        )
        project.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            project.db.execute("UPDATE columns SET hidden=0 WHERE id=?", (column_id,))
        project.db.rollback()
    finally:
        project.close()
