from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY


_PRIOR_DIGEST = "frisket.schema.v1:43e64204b7c8109bc79bfaa1dfdf8b0e"
_CURRENT_CELLS_DIGEST = "frisket.schema.v1:eb7a1342f9f42a48ba6b1ec3863b3a5a"


def _prior_bundle(tmp_path, *, column_count: int = 1):
    path = tmp_path / "prior.frisket"
    project = Project.create(path, name="Historical")
    db = project.db
    db.execute("INSERT INTO sheets (id,name) VALUES (1,'Sheet')")
    db.execute("INSERT INTO rows (id,sheet_id,position) VALUES (100,1,1)")
    db.execute("INSERT INTO rows (id,sheet_id,position) VALUES (101,1,2)")
    db.executemany(
        "INSERT INTO columns (id,sheet_id,name,position) VALUES (?,1,?,?)",
        [(10 + index, f"c{index}", index) for index in range(column_count)],
    )
    db.executemany(
        "INSERT INTO cells (row_id,column_id,value) VALUES (100,?,?)",
        [(10 + index, f'"base-{index}"') for index in range(column_count)],
    )
    db.execute(
        "INSERT INTO cells (row_id,column_id,value) VALUES (101,10,'\"fallback\"')"
    )
    db.execute("INSERT INTO ops (id,kind,spec) VALUES (1,'edit','{}')")
    db.execute(
        "INSERT INTO edits (op_id,row_id,column_id,value) "
        "VALUES (1,100,10,'\"edited\"')"
    )
    db.execute("INSERT INTO ops (id,kind,spec) VALUES (2,'map.test','{}')")
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (20,2,1,'map.test')"
    )
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (20,10,'value','text','create','active','claim')"
    )
    db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,outcome,publication_effect) "
        "VALUES (20,101,10,NULL,'ok','publish_null')"
    )
    db.execute(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (10,101,20)"
    )
    db.commit()
    project.close()

    with sqlite3.connect(path / "project.db") as downgrade:
        downgrade.execute("PRAGMA foreign_keys=OFF")
        downgrade.execute("DROP TABLE current_cells")
        downgrade.execute("DROP INDEX idx_cells_column")
        downgrade.execute("ALTER TABLE cells DROP COLUMN producer_id")
        downgrade.execute("DROP TABLE base_cell_producers")
        downgrade.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (_PRIOR_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
    return path


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_migration_backfills_current_precedence_without_inventing_base_origins(
    tmp_path,
) -> None:
    path = _prior_bundle(tmp_path)
    project = Project(path)

    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert "producer_id" in {
        str(row[1]) for row in project.db.execute("PRAGMA table_info(cells)")
    }
    assert (
        project.db.execute(
            "SELECT producer_id FROM cells WHERE row_id=100 AND column_id=10"
        ).fetchone()[0]
        is None
    )
    rows = project.db.execute(
        "SELECT row_id,value,origin_kind,origin_op_id,origin_run_id,base_producer_id "
        "FROM current_cells WHERE column_id=10 ORDER BY row_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (100, '"edited"', "manual_edit", 1, None, None),
        (101, None, "run_result", 2, 20, None),
    ]
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []
    project.close()

    # The fixed endpoint makes a second open a no-op, not a destructive rebuild.
    reopened = Project(path)
    assert reopened.db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0] == 2
    reopened.close()


def test_migration_backfill_batches_past_sqlites_bind_limit(tmp_path) -> None:
    path = _prior_bundle(tmp_path, column_count=905)
    project = Project(path)
    assert project.db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0] == 906
    assert project.db.execute(
        "SELECT value,origin_kind FROM current_cells WHERE row_id=100 AND column_id=?",
        (10 + 904,),
    ).fetchone()[:] == ('"base-904"', "source_cell")
    project.close()


def test_migration_stamp_and_backfill_roll_back_together(tmp_path) -> None:
    path = _prior_bundle(tmp_path)
    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER interrupt_current_cell_stamp BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' AND NEW.value<>OLD.value BEGIN "
            "SELECT RAISE(ABORT,'migration interrupted'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        Project(path)

    with sqlite3.connect(path / "project.db") as db:
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == _PRIOR_DIGEST
        )
        assert "producer_id" not in {
            str(row[1]) for row in db.execute("PRAGMA table_info(cells)")
        }
        assert "current_cells" not in _table_names(db)
        assert "base_cell_producers" not in _table_names(db)
        db.execute("DROP TRIGGER interrupt_current_cell_stamp")

    Project(path).close()


def test_validity_migration_rebuilds_without_rewriting_values(tmp_path) -> None:
    path = tmp_path / "validity-prior.frisket"
    project = Project.create(path)
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "amount", type="number")
    row_ids = project.add_rows(
        sheet_id, [{"amount": 9}, {"amount": "unknown"}], {"amount": column_id}
    )
    project.close()

    with sqlite3.connect(path / "project.db") as downgrade:
        downgrade.execute("ALTER TABLE current_cells DROP COLUMN validity")
        downgrade.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (_CURRENT_CELLS_DIGEST, SCHEMA_DIGEST_META_KEY),
        )

    migrated = Project(path)
    assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert migrated.get_values(sheet_id, column_id) == {
        row_ids[0]: 9,
        row_ids[1]: None,
    }
    assert migrated.get_values(sheet_id, column_id, preserve_invalid=True) == {
        row_ids[0]: 9,
        row_ids[1]: "unknown",
    }
    assert migrated.db.execute(
        "SELECT value,validity FROM current_cells WHERE row_id=? AND column_id=?",
        (row_ids[1], column_id),
    ).fetchone()[:] == ('"unknown"', "invalid")
    migrated.close()
