"""Upgrade assertions use the frozen bd402937 schema, not current DDL."""

import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from frisket.engine.store.domain_migration import (
    archive_history,
    capture_current_baseline,
    install_current_baseline,
    retire_unfinished_work,
)
from frisket.engine.store.schema import BundleSchemaMismatch


@pytest.fixture
def old_db(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.row_factory = sqlite3.Row
    fixture = Path(__file__).parents[1] / "fixtures/domain-v0-schema.sql.gz"
    db.executescript(gzip.decompress(fixture.read_bytes()).decode())
    db.execute("INSERT INTO sheets(id,name) VALUES (1,'Source')")
    db.execute("INSERT INTO columns(id,sheet_id,name) VALUES (1,1,'Text')")
    db.execute("INSERT INTO rows(id,sheet_id) VALUES (1,1)")
    db.execute("INSERT INTO ops(id,kind) VALUES (1,'import')")
    db.execute("INSERT INTO ops(id,kind) VALUES (2,'edit')")
    db.execute("INSERT INTO base_cell_producers(id,stage_id,op_id) VALUES (1,'old',1)")
    db.execute(
        "INSERT INTO cells(row_id,column_id,value_kind,value,producer_id) "
        "VALUES (1,1,'text','source',1)"
    )
    db.execute(
        "INSERT INTO edits(op_id,row_id,column_id,value_kind,value) "
        "VALUES (2,1,1,'text','corrected')"
    )
    db.execute(
        "INSERT INTO current_cells(column_id,row_id,retrieval_revision,"
        "origin_kind,origin_op_id,validity) VALUES (1,1,1,'manual_edit',2,'valid')"
    )
    db.commit()
    yield db
    db.close()


def test_baseline_retains_winning_reference_not_a_payload_copy(old_db):
    db = old_db
    db.execute("BEGIN IMMEDIATE")
    capture_current_baseline(db)
    db.execute(
        "CREATE TABLE cell_baseline(row_id,column_id,origin_kind,origin_op_id,"
        "origin_run_id,base_producer_id,PRIMARY KEY(column_id,row_id))"
    )
    install_current_baseline(db)
    db.commit()
    assert tuple(db.execute("SELECT * FROM cell_baseline").fetchone()) == (
        1,
        1,
        "manual_edit",
        2,
        None,
        None,
    )
    assert db.execute("SELECT value FROM edits").fetchone()[0] == "corrected"
    assert db.execute("SELECT value FROM cells").fetchone()[0] == "source"
    assert not db.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name='domain_baseline_capture'"
    ).fetchone()


def test_retirement_keeps_returned_work_and_completed_history(old_db):
    db = old_db
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind) VALUES (1,1,1,'map.ask')"
    )
    for identity, state, payload in (
        ("pending", "reserved", None),
        ("returned", "returned", '{"answer":"saved"}'),
        ("consumed", "consumed", '{"usage":42}'),
    ):
        db.execute(
            "INSERT INTO effect_checkpoints"
            "(id,family,group_key,unit_key,action_kind,identity,state,payload) "
            "VALUES (?,'model','run-1',?,'map.ask',?,?,?)",
            (identity, identity, identity, state, payload),
        )
    for identity, status in (("active", "running"), ("done", "completed")):
        db.execute(
            "INSERT INTO receipts(id,run_id,action_kind,status,body) VALUES (?,1,'map.ask',?,?)",
            (identity, status, json.dumps({"receipt_id": identity, "status": status})),
        )
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    retire_unfinished_work(db)
    db.commit()
    assert db.execute("SELECT status FROM runs").fetchone()[0] == "cancelled"
    assert dict(db.execute("SELECT id,status FROM receipts")) == {
        "active": "cancelled",
        "done": "completed",
    }
    assert dict(db.execute("SELECT id,payload FROM effect_checkpoints")) == {
        "returned": '{"answer":"saved"}',
        "consumed": '{"usage":42}',
    }
    body = json.loads(
        db.execute("SELECT body FROM receipts WHERE id='active'").fetchone()[0]
    )
    assert body["status"] == "cancelled"
    assert body["errors"][0]["code"] == "execution_cutover"


def test_bad_receipt_rolls_back_the_entire_upgrade(old_db):
    db = old_db
    db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES ('bad','map.ask','running','{}')"
    )
    db.commit()
    with pytest.raises(BundleSchemaMismatch), db:
        db.execute("BEGIN IMMEDIATE")
        capture_current_baseline(db)
        db.execute("UPDATE cells SET value='must roll back'")
        retire_unfinished_work(db)
    assert db.execute("SELECT value FROM cells").fetchone()[0] == "source"
    assert db.execute("SELECT status FROM receipts").fetchone()[0] == "running"


def test_old_history_is_inspectable_but_not_navigable(old_db):
    db = old_db
    db.execute("ALTER TABLE ops ADD COLUMN change_order INTEGER")
    db.execute("ALTER TABLE ops ADD COLUMN publication_closed INTEGER DEFAULT 1")
    db.execute("ALTER TABLE ops ADD COLUMN redo_eligible INTEGER DEFAULT 1")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    archive_history(db)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key='domain_history_floor'"
        ).fetchone()[0]
        == "2"
    )
    assert all(
        row[0] is None and row[1:] == (1, 0)
        for row in (
            tuple(row)
            for row in db.execute(
                "SELECT change_order,publication_closed,redo_eligible FROM ops"
            )
        )
    )


def _stamp_old(db):
    from frisket.engine.store.domain_migration import DOMAIN_FROM_DIGEST

    db.execute(
        "INSERT INTO meta(key,value) VALUES ('schema_digest',?)", (DOMAIN_FROM_DIGEST,)
    )
    db.commit()


def test_full_cutover_preserves_values_and_starts_new_history(old_db):
    from frisket.engine.store.domain_migration import migrate_domain
    from frisket.engine.store.schema import SCHEMA_DIGEST

    db = old_db
    _stamp_old(db)
    db.execute("PRAGMA foreign_keys=ON")
    migrate_domain(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    assert (
        db.execute("SELECT value FROM meta WHERE key='schema_digest'").fetchone()[0]
        == SCHEMA_DIGEST
    )
    assert (
        db.execute(
            "SELECT value FROM current_cell_values WHERE column_id=1 AND row_id=1"
        ).fetchone()[0]
        == "corrected"
    )
    assert (
        db.execute(
            "SELECT origin_op_id FROM canonical_cell_heads WHERE column_id=1 AND row_id=1"
        ).fetchone()[0]
        == 2
    )
    assert db.execute("SELECT value FROM cells").fetchone()[0] == "source"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM ops WHERE change_order IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    migrate_domain(db)  # Reopening does not recapture or reset new history.


def test_cutover_preserves_anonymous_source_identity(old_db):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    db.execute("DELETE FROM edits")
    db.execute("UPDATE cells SET producer_id=NULL")
    db.execute(
        "UPDATE current_cells SET origin_kind='source_cell',origin_op_id=NULL,base_producer_id=NULL"
    )
    _stamp_old(db)
    migrate_domain(db)
    head = db.execute(
        "SELECT base_producer_id,value FROM current_cell_values"
    ).fetchone()
    assert head[0] is not None and head[1] == "source"
    assert (
        db.execute(
            "SELECT op_id FROM base_cell_producers WHERE id=?", (head[0],)
        ).fetchone()[0]
        is None
    )
    assert (
        db.execute("SELECT base_producer_id FROM cell_baseline").fetchone()[0]
        == head[0]
    )


def test_full_cutover_failure_restores_schema_and_pragmas(old_db):
    from frisket.engine.store.domain_migration import migrate_domain, DOMAIN_FROM_DIGEST

    db = old_db
    db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES('bad','map.ask','running','{}')"
    )
    _stamp_old(db)
    db.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(BundleSchemaMismatch, match="malformed receipt"):
        migrate_domain(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not db.in_transaction
    assert (
        db.execute("SELECT value FROM meta WHERE key='schema_digest'").fetchone()[0]
        == DOMAIN_FROM_DIGEST
    )
    assert "change_order" not in {r[1] for r in db.execute("PRAGMA table_info(ops)")}
    assert db.execute("SELECT value FROM edits").fetchone()[0] == "corrected"
    assert not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='cell_baseline'"
    ).fetchone()
