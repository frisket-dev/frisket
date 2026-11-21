"""The local run queue has one current schema; older queues are replaced only
once their user work is finished, and job ids never restart."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from frisket.engine.jobs import QUEUE_DB_NAME, open_queue
from frisket.engine.jobs.queue_schema import (
    QUEUE_SCHEMA_LEDGER,
    QUEUE_SCHEMA_VERSION,
    QueueSchemaError,
    validate_queue_schema,
)
from frisket.upgrades.queue import QueueUpgradeBlocked


_ISO = "2020-01-01T00:00:00.000000+00:00"

# The base jobs shape from before the version ledger and the later queue
# tables: a plain rowid primary key, which restarts at 1 on an empty table.
_PRE_LEDGER_DDL = """
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  locked_by TEXT,
  locked_at TEXT,
  lease_expires_at TEXT,
  available_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  result TEXT,
  error TEXT
);
CREATE TABLE worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  last_heartbeat_at TEXT NOT NULL
);
"""


def _db(tmp_path: Path) -> Path:
    return tmp_path / QUEUE_DB_NAME


def _query(db_path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _execute(db_path: Path, *statements: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def _seed_pre_ledger_queue(db_path: Path, *rows: tuple[int, str, str]) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_LEDGER_DDL)
        con.executemany(
            "INSERT INTO jobs (id, kind, status, available_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(job_id, kind, status, _ISO, _ISO) for job_id, kind, status in rows],
        )
        con.commit()
    finally:
        con.close()


def _mark_previous_version(db_path: Path) -> None:
    _execute(
        db_path,
        f"UPDATE {QUEUE_SCHEMA_LEDGER} SET version = {QUEUE_SCHEMA_VERSION - 1}",
    )


def _validate(db_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        validate_queue_schema(engine)
    finally:
        engine.dispose()


def _jobs_columns(db_path: Path) -> set[str]:
    return {str(row[1]) for row in _query(db_path, "PRAGMA table_info(jobs)")}


def _seed_v11_queue(
    tmp_path: Path, *rows: tuple[int, str, str, int, int | None]
) -> Path:
    open_queue(workspace=tmp_path).close()
    db_path = _db(tmp_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "ALTER TABLE jobs ADD COLUMN priority_class INTEGER NOT NULL DEFAULT 0"
        )
        con.execute("ALTER TABLE jobs ADD COLUMN preferred_row_id INTEGER")
        con.executemany(
            "INSERT INTO jobs"
            " (id, kind, status, available_at, created_at, priority_class, preferred_row_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (job_id, kind, status, _ISO, _ISO, priority_class, preferred_row_id)
                for job_id, kind, status, priority_class, preferred_row_id in rows
            ],
        )
        con.execute(f"UPDATE {QUEUE_SCHEMA_LEDGER} SET version = 11")
        con.commit()
    finally:
        con.close()
    return db_path


def test_fresh_queue_is_created_current_and_validates(tmp_path) -> None:
    open_queue(workspace=tmp_path).close()

    db_path = _db(tmp_path)
    assert _query(db_path, f"SELECT version FROM {QUEUE_SCHEMA_LEDGER}") == [
        (QUEUE_SCHEMA_VERSION,)
    ]
    jobs_ddl = _query(db_path, "SELECT sql FROM sqlite_master WHERE name = 'jobs'")
    assert "AUTOINCREMENT" in jobs_ddl[0][0]
    columns = _jobs_columns(db_path)
    assert "priority_class" not in columns
    assert "preferred_row_id" not in columns
    _validate(db_path)


def test_current_queue_is_left_unchanged_on_routine_open(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        queued = queue.enqueue("project.run", {"project_id": "alpha"})
    finally:
        queue.close()
    db_path = _db(tmp_path)
    ledger_before = _query(db_path, f"SELECT * FROM {QUEUE_SCHEMA_LEDGER}")

    queue = open_queue(workspace=tmp_path)
    try:
        assert queue.get(queued).status == "queued"
    finally:
        queue.close()

    assert _query(db_path, f"SELECT * FROM {QUEUE_SCHEMA_LEDGER}") == ledger_before


def test_older_queue_with_only_finished_and_background_work_is_replaced(
    tmp_path,
) -> None:
    db_path = _db(tmp_path)
    _seed_pre_ledger_queue(
        db_path,
        (40, "project.run", "done"),
        (41, "action.run", "cancelled"),
        (57, "search.index", "queued"),
        (58, "blob.metadata.backfill", "running"),
    )

    queue = open_queue(workspace=tmp_path)
    try:
        assert queue.list_jobs() == []
        # Project stores still hold old ids; recovery must see their handlers
        # as stopped, exactly as the old terminal rows would have shown.
        assert queue.job_execution_stopped(40)
        assert queue.handler_execution_stopped(41, "old-authority")
        new_id = queue.enqueue("project.run", {"project_id": "alpha"})
        # A never-issued id stays unknown, never proof of a stopped handler.
        assert not queue.job_execution_stopped(new_id + 1)
        assert not queue.handler_execution_stopped(new_id + 1, "authority")
        assert not queue.job_execution_stopped(new_id)
    finally:
        queue.close()

    # A new job must never alias an id that project stores still hold.
    assert new_id == 59
    assert _query(db_path, f"SELECT version FROM {QUEUE_SCHEMA_LEDGER}") == [
        (QUEUE_SCHEMA_VERSION,)
    ]
    _validate(db_path)


def test_replacing_an_autoincrement_queue_keeps_its_sequence_high_water(
    tmp_path,
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        for _ in range(3):
            queue.enqueue("project.run", {"project_id": "alpha"})
    finally:
        queue.close()
    db_path = _db(tmp_path)
    _execute(
        db_path, "UPDATE jobs SET status = 'done'", "DELETE FROM jobs WHERE id = 3"
    )
    _mark_previous_version(db_path)

    queue = open_queue(workspace=tmp_path)
    try:
        assert queue.enqueue("project.run", {"project_id": "alpha"}) == 4
    finally:
        queue.close()


def test_v11_queue_with_priority_columns_upgrades_and_drops_them(tmp_path) -> None:
    db_path = _seed_v11_queue(
        tmp_path,
        (10, "project.run", "done", 10, 42),
        (12, "search.index", "queued", 0, None),
    )
    assert {"priority_class", "preferred_row_id"} <= _jobs_columns(db_path)
    with pytest.raises(QueueSchemaError, match="older"):
        _validate(db_path)

    queue = open_queue(workspace=tmp_path)
    try:
        assert queue.list_jobs() == []
        assert queue.job_execution_stopped(10)
        new_id = queue.enqueue("project.run", {"project_id": "alpha"})
        claimed = queue.claim("worker-1")
        assert claimed is not None
        assert claimed.id == new_id
        assert not hasattr(claimed, "priority_class")
        assert not hasattr(claimed, "preferred_row_id")
    finally:
        queue.close()

    assert new_id == 13
    columns = _jobs_columns(db_path)
    assert "priority_class" not in columns
    assert "preferred_row_id" not in columns
    assert _query(db_path, f"SELECT version FROM {QUEUE_SCHEMA_LEDGER}") == [
        (QUEUE_SCHEMA_VERSION,)
    ]
    _validate(db_path)


def test_v11_queue_with_unfinished_user_work_refuses_upgrade_untouched(
    tmp_path,
) -> None:
    db_path = _seed_v11_queue(
        tmp_path,
        (7, "project.run", "queued", 10, 99),
        (8, "action.run", "running", 0, None),
    )
    before = _query(
        db_path,
        "SELECT id, kind, status, priority_class, preferred_row_id FROM jobs ORDER BY id",
    )

    with pytest.raises(QueueUpgradeBlocked, match="unfinished work"):
        open_queue(workspace=tmp_path)

    assert {"priority_class", "preferred_row_id"} <= _jobs_columns(db_path)
    assert (
        _query(
            db_path,
            "SELECT id, kind, status, priority_class, preferred_row_id"
            " FROM jobs ORDER BY id",
        )
        == before
    )
    assert _query(db_path, f"SELECT version FROM {QUEUE_SCHEMA_LEDGER}") == [(11,)]


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(
            "UPDATE jobs SET status = 'queued' WHERE kind = 'project.run'",
            id="queued-user-job",
        ),
        pytest.param(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, created_at)"
            f" VALUES ('/w', 'm', 'running', '{_ISO}')",
            id="active-model-download",
        ),
        pytest.param(
            "INSERT INTO search_preparation_batches (id, workspace_root, project_id,"
            " intent_key, scope_json, force_ocr, created_at, updated_at)"
            f" VALUES ('b', '/w', 'alpha', 'k', '{{}}', 0, '{_ISO}', '{_ISO}')",
            id="unsettled-search-preparation",
        ),
        pytest.param(
            "INSERT INTO job_handler_authorities"
            " (authority_id, job_id, worker_id, storage_organization_id, project_id, claimed_at)"
            f" SELECT 'a', id, 'w', 1, 'alpha', '{_ISO}' FROM jobs WHERE kind = 'project.run'",
            id="handler-still-running-a-cancelled-user-job",
        ),
    ],
)
def test_older_queue_with_unfinished_user_work_is_refused_untouched(
    tmp_path, seed: str
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        queue.enqueue("project.run", {"project_id": "alpha"})
        queue.enqueue("search.index", {"project_id": "alpha"})
    finally:
        queue.close()
    db_path = _db(tmp_path)
    _execute(db_path, "UPDATE jobs SET status = 'cancelled'", seed)
    _mark_previous_version(db_path)
    before = _query(db_path, "SELECT id, kind, status FROM jobs ORDER BY id")

    with pytest.raises(QueueUpgradeBlocked, match="unfinished work"):
        open_queue(workspace=tmp_path)

    assert _query(db_path, "SELECT id, kind, status FROM jobs ORDER BY id") == before
    assert _query(db_path, f"SELECT version FROM {QUEUE_SCHEMA_LEDGER}") == [
        (QUEUE_SCHEMA_VERSION - 1,)
    ]


def test_replacement_leaves_co_tenant_tables_in_the_same_database(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed_pre_ledger_queue(db_path, (5, "project.run", "done"))
    _execute(
        db_path,
        "CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT)",
        "INSERT INTO orgs (name) VALUES ('newsroom')",
    )

    open_queue(workspace=tmp_path).close()

    assert _query(db_path, "SELECT name FROM orgs") == [("newsroom",)]


def test_newer_queue_is_refused(tmp_path) -> None:
    open_queue(workspace=tmp_path).close()
    db_path = _db(tmp_path)
    _execute(
        db_path,
        f"UPDATE {QUEUE_SCHEMA_LEDGER} SET version = {QUEUE_SCHEMA_VERSION + 1}",
    )

    with pytest.raises(QueueSchemaError, match="newer"):
        open_queue(workspace=tmp_path)


def test_strict_validation_fails_closed_on_missing_model_pulls_column(
    tmp_path,
) -> None:
    open_queue(workspace=tmp_path).close()
    db_path = _db(tmp_path)
    _execute(
        db_path, "ALTER TABLE model_pulls RENAME COLUMN endpoint_origin TO x_removed"
    )

    with pytest.raises(QueueSchemaError, match="model_pulls"):
        _validate(db_path)


def test_strict_validation_fails_closed_on_missing_model_pulls_index(
    tmp_path,
) -> None:
    open_queue(workspace=tmp_path).close()
    db_path = _db(tmp_path)
    _execute(db_path, "DROP INDEX uq_model_pulls_active_workspace")

    with pytest.raises(QueueSchemaError, match="model_pulls"):
        _validate(db_path)


def test_strict_validation_refuses_an_older_queue(tmp_path) -> None:
    open_queue(workspace=tmp_path).close()
    db_path = _db(tmp_path)
    _mark_previous_version(db_path)

    with pytest.raises(QueueSchemaError, match="older"):
        _validate(db_path)


def test_model_pull_artifact_metadata_roundtrips_into_the_dto(tmp_path) -> None:
    from frisket.engine.jobs import model_pull_store as store

    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="opus-mt:en-es"
        )
        store.set_artifact_metadata(
            engine,
            row.id,
            artifact_kind="ct2_pair",
            artifact_source_url="https://huggingface.co/frisket-models/x@rev",
            artifact_license="CC-BY-4.0",
            artifact_manifest_version="2026.07.1",
        )
        fresh = store.get(engine, row.id)
    finally:
        queue.close()

    assert fresh is not None
    assert store.to_dto(fresh)["artifact"] == {
        "kind": "ct2_pair",
        "source_url": "https://huggingface.co/frisket-models/x@rev",
        "license": "CC-BY-4.0",
        "manifest_version": "2026.07.1",
    }
