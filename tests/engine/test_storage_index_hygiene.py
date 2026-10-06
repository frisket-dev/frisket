"""Data-preserving removal of redundant project indexes."""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import bundle_open
from frisket.engine.store.bundle_io import export_database
from frisket.engine.store.project import Project
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from tests.engine.test_bundle_schema_fence import _restore_legacy_authorities


_PRIOR_DIGEST = "frisket.schema.v1:f2d652e33a1633c4367813af3c2d3746"
_REDUNDANT_INDEXES = {
    "idx_execution_attempts_run",
    "idx_source_items_source_dedupe",
    "idx_source_runs_source",
}
_REVIEW_STATS_INDEXES = {
    "idx_cell_result_heads_run",
    "idx_columns_current_run",
}


def _seed_ledgers(project: Project) -> None:
    project.db.execute("INSERT INTO sheets(id,name) VALUES(1,'Ledger')")
    project.db.execute(
        "INSERT INTO ops(id,kind,spec) VALUES(1,'test.index_hygiene','{}')"
    )
    project.db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind,status) "
        "VALUES(1,1,1,'test.index_hygiene','completed')"
    )
    project.db.execute(
        "INSERT INTO execution_attempts("
        "id,run_id,seq,state,action_identity_hash,scope_json,created_at"
        ") VALUES('attempt-1',1,1,'created','identity','[]','2026-10-02')"
    )
    project.db.execute("INSERT INTO sources(id,name,sheet_id) VALUES(1,'Source',1)")
    project.db.execute(
        "INSERT INTO source_runs(id,source_id,status,started_at) "
        "VALUES(1,1,'ok','2026-10-02')"
    )
    project.db.execute(
        "INSERT INTO source_items("
        "id,source_id,source_item_id,dedupe_key,item_hash,"
        "first_seen_run_id,last_seen_run_id"
        ") VALUES(1,1,'item-1','dedupe-1','hash-1',1,1)"
    )
    project.db.commit()


def _index_names(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


def _restore_redundant_indexes(db: sqlite3.Connection) -> None:
    db.executescript(
        "CREATE INDEX IF NOT EXISTS idx_execution_attempts_run "
        "ON execution_attempts(run_id,seq DESC);"
        "CREATE INDEX IF NOT EXISTS idx_source_items_source_dedupe "
        "ON source_items(source_id,dedupe_key);"
        "CREATE INDEX IF NOT EXISTS idx_source_runs_source "
        "ON source_runs(source_id);"
    )


def _plan(db: sqlite3.Connection, query: str, params: tuple[object, ...]) -> str:
    return "\n".join(
        str(row[3])
        for row in db.execute("EXPLAIN QUERY PLAN " + query, params).fetchall()
    )


def _assert_runtime_access_paths(project: Project) -> None:
    assert "sqlite_autoindex_execution_attempts_2" in _plan(
        project.db,
        "SELECT id FROM execution_attempts WHERE run_id=? ORDER BY seq DESC",
        (1,),
    )
    assert "sqlite_autoindex_source_items_1" in _plan(
        project.db,
        "SELECT id FROM source_items WHERE source_id=? AND dedupe_key=?",
        (1, "dedupe-1"),
    )
    assert "idx_source_runs_source_started" in _plan(
        project.db,
        "SELECT COUNT(*) FROM source_runs WHERE source_id=?",
        (1,),
    )


def _assert_uniqueness(project: Project) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute(
            "INSERT INTO execution_attempts("
            "id,run_id,seq,state,action_identity_hash,scope_json,created_at"
            ") VALUES('attempt-duplicate',1,1,'created','other','[]','2026-10-02')"
        )
    project.db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute(
            "INSERT INTO source_items("
            "source_id,source_item_id,dedupe_key,item_hash,"
            "first_seen_run_id,last_seen_run_id"
            ") VALUES(1,'item-duplicate','dedupe-1','other',1,1)"
        )
    project.db.rollback()


def test_fresh_bundle_uses_constraint_and_composite_indexes(tmp_path) -> None:
    project = Project.create(tmp_path / "fresh.frisket", name="Fresh")
    _seed_ledgers(project)

    assert not (_index_names(project.db) & _REDUNDANT_INDEXES)
    _assert_runtime_access_paths(project)
    _assert_uniqueness(project)
    project.close()


def test_known_bundle_upgrade_preserves_ledgers_reopen_and_export(tmp_path) -> None:
    path = tmp_path / "prior.frisket"
    project = Project.create(path, name="Prior")
    _seed_ledgers(project)
    project.close()

    db = sqlite3.connect(path / "project.db")
    db.execute("PRAGMA foreign_keys=OFF")
    _restore_legacy_authorities(db)
    _restore_redundant_indexes(db)
    before_indexes = _index_names(db)
    db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (_PRIOR_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    db.commit()
    db.close()

    migrated = Project(path)
    assert _index_names(migrated.db) == (
        before_indexes - _REDUNDANT_INDEXES
    ) | _REVIEW_STATS_INDEXES | {
        "sqlite_autoindex_extraction_layouts_1",
        "sqlite_autoindex_extraction_layouts_2",
        "idx_extraction_layout_documents_layout",
    }
    assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert [
        tuple(row)
        for row in migrated.db.execute(
            "SELECT id,seq FROM execution_attempts WHERE run_id=1"
        ).fetchall()
    ] == [("attempt-1", 1)]
    assert [
        tuple(row)
        for row in migrated.db.execute(
            "SELECT id,item_hash FROM source_items "
            "WHERE source_id=1 AND dedupe_key='dedupe-1'"
        ).fetchall()
    ] == [(1, "hash-1")]
    assert [
        tuple(row)
        for row in migrated.db.execute(
            "SELECT id,status FROM source_runs WHERE source_id=1"
        ).fetchall()
    ] == [(1, "ok")]
    _assert_runtime_access_paths(migrated)
    _assert_uniqueness(migrated)

    exported = export_database(migrated, tmp_path / "export.db")
    migrated.close()
    reopened = Project(path)
    assert not (_index_names(reopened.db) & _REDUNDANT_INDEXES)
    reopened.close()

    export_db = sqlite3.connect(exported)
    assert not (_index_names(export_db) & _REDUNDANT_INDEXES)
    assert (
        export_db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 1
    )
    assert export_db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
    export_db.close()


def test_index_hygiene_upgrade_rolls_back_all_changes_on_failure(tmp_path) -> None:
    path = tmp_path / "rollback.frisket"
    project = Project.create(path, name="Rollback")
    _seed_ledgers(project)
    project.close()

    db = sqlite3.connect(path / "project.db")
    _restore_redundant_indexes(db)
    db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (_PRIOR_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    db.executescript(
        "CREATE TRIGGER fail_index_hygiene_digest "
        "BEFORE UPDATE OF value ON meta "
        "WHEN OLD.key='schema_digest' AND NEW.value != OLD.value "
        "BEGIN SELECT RAISE(ABORT,'forced migration failure'); END;"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced migration failure"):
        bundle_open._migrate_index_hygiene(db)

    assert _REDUNDANT_INDEXES <= _index_names(db)
    assert db.execute(
        "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
    ).fetchone() == (_PRIOR_DIGEST,)
    assert db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM source_items").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM source_runs").fetchone() == (1,)
    db.close()
