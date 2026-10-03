"""Active-name cutover preserves column IDs, child data and hidden live outputs."""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.bundle_open import _migrate_active_columns
from frisket.engine.store.schema import (
    SCHEMA,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
    schema_digest,
)

_PRIOR_DIGEST = "frisket.schema.v1:f078f2bc57411d372468936618f2f884"


def _prior_bundle(tmp_path):
    schema = SCHEMA.replace(
        "  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),\n", ""
    ).replace(
        "  created_at TEXT NOT NULL DEFAULT (datetime('now'))\n);\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_columns_active_name\n"
        "  ON columns(sheet_id,name) WHERE active=1;",
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),\n"
        "  UNIQUE(sheet_id, name)\n);",
    )
    assert schema_digest(schema) == _PRIOR_DIGEST
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
    with sqlite3.connect(path / "project.db") as db:
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("cells", "edits", "results", "current_cells", "ops")
        }
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
    for table, rows in before.items():
        assert [
            tuple(row) for row in project.db.execute(f"SELECT * FROM {table}")
        ] == rows
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
