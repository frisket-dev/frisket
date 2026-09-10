"""model_pulls table provisioning through the queue's ordered migration
ledger, mirroring tests/test_queue_sqlite_migration_ledger.py's pattern: a
fresh queue gets
the table for free, and an existing pre-model_pulls .queue.db upgrades in
place without losing data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from frisket.engine.jobs import QUEUE_DB_NAME, open_queue
from frisket.engine.jobs.model_pull_store import model_pulls_table
from frisket.engine.jobs.queue_migrations import (
    QUEUE_SCHEMA_LEDGER,
    QUEUE_SCHEMA_VERSION,
)

_ISO = "2020-01-01T00:00:00.000000+00:00"

# A jobs/worker_heartbeats/ledger shape from BEFORE model_pulls existed
# (queue schema version 1 only) -- the "real pre-model_pulls .queue.db" the
# v2 migration must upgrade in place.
_PRE_MODEL_PULLS_DDL = """
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  org_id TEXT,
  storage_org_id INTEGER,
  project_id TEXT,
  run_id INTEGER,
  source_id INTEGER,
  sheet_id INTEGER,
  row_id INTEGER,
  receipt_id TEXT,
  action_kind TEXT,
  trace_id TEXT,
  workspace_root TEXT,
  dedupe_key TEXT,
  code_version TEXT,
  claimed_code_version TEXT,
  version_mismatch_requeues INTEGER NOT NULL DEFAULT 0,
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
  queue TEXT,
  first_seen_at TEXT NOT NULL,
  last_heartbeat_at TEXT NOT NULL,
  worker_version TEXT
);
CREATE TABLE frisket_queue_schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  applying_code_identity TEXT NOT NULL
);
"""


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()


def _ledger_versions(db_path: Path) -> list[int]:
    con = sqlite3.connect(db_path)
    try:
        return [
            int(row[0])
            for row in con.execute(
                f"SELECT version FROM {QUEUE_SCHEMA_LEDGER} ORDER BY version"
            )
        ]
    finally:
        con.close()


def test_fresh_sqlite_queue_provisions_model_pulls_table(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        pass
    finally:
        queue.close()

    db_path = tmp_path / QUEUE_DB_NAME
    assert "model_pulls" in _tables(db_path)
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))


def test_pre_model_pulls_queue_db_upgrades_in_place(tmp_path) -> None:
    db_path = tmp_path / QUEUE_DB_NAME
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_MODEL_PULLS_DDL)
        con.execute(
            "INSERT INTO jobs (kind, payload, status, attempts, max_attempts,"
            " available_at, created_at) VALUES (?, ?, 'queued', 0, 3, ?, ?)",
            ("echo", '{"sentinel": true}', _ISO, _ISO),
        )
        con.execute(
            "INSERT INTO frisket_queue_schema_migrations "
            "(version, applied_at, applying_code_identity) VALUES (1, ?, 'test')",
            (_ISO,),
        )
        con.commit()
    finally:
        con.close()

    assert "model_pulls" not in _tables(db_path)

    queue = open_queue(workspace=tmp_path)
    try:
        payloads = [job.payload for job in queue.list_jobs()]
        engine = queue.engine
        with engine.connect() as cx:
            import sqlalchemy as sa

            count = cx.execute(
                sa.select(sa.func.count()).select_from(model_pulls_table)
            ).scalar_one()
    finally:
        queue.close()

    assert "model_pulls" in _tables(db_path)
    assert count == 0  # the fresh table is empty, not an error
    assert {"sentinel": True} in payloads, (
        "the pre-existing jobs row was lost during the model_pulls upgrade"
    )
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))


def _columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _index_names(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA index_list({table})")}
    finally:
        con.close()


# A jobs/worker_heartbeats/model_pulls/ledger shape from BEFORE v3 -- queue
# schema version 2 only. The real pre-hardening ``model_pulls`` shape v3 must
# upgrade in place.
_PRE_V3_MODEL_PULLS_DDL = """
CREATE TABLE model_pulls (
  id INTEGER PRIMARY KEY,
  workspace_root TEXT NOT NULL,
  model_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  job_id INTEGER,
  phase TEXT,
  total_bytes INTEGER,
  completed_bytes INTEGER,
  error_code TEXT,
  error_message TEXT,
  correlation_id TEXT,
  resolved_digest TEXT,
  resolved_size INTEGER,
  cancel_requested_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE UNIQUE INDEX uq_model_pulls_active_ref ON model_pulls(workspace_root, model_ref)
  WHERE status IN ('pending', 'running');
"""


def test_v3_migration_adds_hardening_columns_and_workspace_index(tmp_path) -> None:
    """A real pre-v3 ``.queue.db`` (``model_pulls`` present but missing
    ``endpoint_origin``/``initiated_by``/the workspace-level unique index,
    ``worker_heartbeats`` missing ``kinds``) upgrades in place without
    losing data -- mirrors ``test_pre_model_pulls_queue_db_upgrades_in_place``
    one version up."""
    db_path = tmp_path / QUEUE_DB_NAME
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_MODEL_PULLS_DDL)
        con.executescript(_PRE_V3_MODEL_PULLS_DDL)
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, created_at)"
            " VALUES (?, ?, 'pending', ?)",
            (str(tmp_path), "smollm:135m", _ISO),
        )
        con.execute(
            "INSERT INTO frisket_queue_schema_migrations "
            "(version, applied_at, applying_code_identity) VALUES (1, ?, 'test'),"
            " (2, ?, 'test')",
            (_ISO, _ISO),
        )
        con.commit()
    finally:
        con.close()

    assert "endpoint_origin" not in _columns(db_path, "model_pulls")
    assert "kinds" not in _columns(db_path, "worker_heartbeats")

    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        with engine.connect() as cx:
            import sqlalchemy as sa

            row = cx.execute(
                sa.select(model_pulls_table.c.model_ref, model_pulls_table.c.status)
            ).first()
    finally:
        queue.close()

    assert (
        row is not None and row.model_ref == "smollm:135m" and row.status == "pending"
    ), "the pre-existing model_pulls row was lost during the v3 upgrade"
    columns = _columns(db_path, "model_pulls")
    assert {"endpoint_origin", "initiated_by"} <= columns
    assert "kinds" in _columns(db_path, "worker_heartbeats")
    assert {"uq_model_pulls_active_ref", "uq_model_pulls_active_workspace"} <= (
        _index_names(db_path, "model_pulls")
    )
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))


def test_v3_migration_reconciles_two_active_different_ref_rows_in_one_workspace(
    tmp_path,
) -> None:
    """A legal v2 database can contain duplicate active pulls because v2's
    ``find_active_for_workspace`` pre-check was a non-atomic TOCTOU. It can
    therefore have two
    ACTIVE (pending/running) rows for the SAME workspace with DIFFERENT
    refs. `_PRE_V3_MODEL_PULLS_DDL` only creates the per-REF unique index
    (`uq_model_pulls_active_ref`), not the new workspace-wide one, so this
    is a legitimate on-disk v2 state, not a corrupt one. Creating
    `uq_model_pulls_active_workspace` against it without reconciling first
    would raise `sqlite3.IntegrityError` and abort the migration, leaving
    the installation unable to start. The fix reconciles first: the OLDEST
    active row survives active; every other active row in that workspace
    is marked failed with a canonical, machine-readable reason."""
    db_path = tmp_path / QUEUE_DB_NAME
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_MODEL_PULLS_DDL)
        con.executescript(_PRE_V3_MODEL_PULLS_DDL)
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, created_at)"
            " VALUES (?, ?, 'pending', ?)",
            (str(tmp_path), "smollm:135m", "2020-01-01T00:00:00.000000+00:00"),
        )
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, created_at)"
            " VALUES (?, ?, 'running', ?)",
            (str(tmp_path), "qwen3:8b", "2020-01-02T00:00:00.000000+00:00"),
        )
        # A THIRD row in a DIFFERENT workspace must be left alone entirely
        # -- reconciliation is scoped per workspace, not global.
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, created_at)"
            " VALUES (?, ?, 'pending', ?)",
            ("/some/other/workspace", "llama3:8b", "2020-01-01T00:00:00.000000+00:00"),
        )
        con.execute(
            "INSERT INTO frisket_queue_schema_migrations "
            "(version, applied_at, applying_code_identity) VALUES (1, ?, 'test'),"
            " (2, ?, 'test')",
            (_ISO, _ISO),
        )
        con.commit()
    finally:
        con.close()

    # Sanity: this really is a legal v2 state -- two active rows, one
    # workspace, different refs, no error raised writing it.
    con = sqlite3.connect(db_path)
    try:
        active_rows = con.execute(
            "SELECT id FROM model_pulls WHERE workspace_root = ? AND status IN"
            " ('pending', 'running')",
            (str(tmp_path),),
        ).fetchall()
    finally:
        con.close()
    assert len(active_rows) == 2

    # The migration must not raise -- this is the whole point of the fix.
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        with engine.connect() as cx:
            import sqlalchemy as sa

            from frisket.engine.jobs.model_pull_store import model_pulls_table

            rows = {
                row.model_ref: row
                for row in cx.execute(
                    sa.select(
                        model_pulls_table.c.model_ref,
                        model_pulls_table.c.status,
                        model_pulls_table.c.error_code,
                        model_pulls_table.c.workspace_root,
                    )
                ).fetchall()
            }
    finally:
        queue.close()

    # The oldest row (smollm, created first) survives active.
    assert rows["smollm:135m"].status == "pending"
    assert rows["smollm:135m"].error_code is None

    # The younger row for the SAME workspace is superseded.
    assert rows["qwen3:8b"].status == "failed"
    assert rows["qwen3:8b"].error_code == "superseded_by_migration"

    # The unrelated workspace's row is untouched.
    assert rows["llama3:8b"].workspace_root == "/some/other/workspace"
    assert rows["llama3:8b"].status == "pending"
    assert rows["llama3:8b"].error_code is None

    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))
    # The new workspace-wide index now holds for the reconciled data.
    assert "uq_model_pulls_active_workspace" in _index_names(db_path, "model_pulls")


def test_v3_migration_terminalizes_losers_linked_jobs_and_worker_never_contacts_daemon(
    tmp_path,
) -> None:
    """The sibling test proves the LOSER model_pulls row is failed, but a REAL
    v2 database also has that
    row's job still 'queued' (never yet claimed) or 'running' (mid-upgrade --
    the worker that claimed it died or was stopped for the upgrade, nothing
    is actually executing). Left executable, `mark_running`'s own
    admin-retry allowance (a job_id reclaiming ITS OWN row is allowed
    'failed' -> 'running') means that job re-attaching to the very row this
    migration just superseded would reopen it, causing both an IntegrityError
    while the survivor is active and a reopened
    'superseded_by_migration' row racing it. The fix cancels the loser's
    linked job in the SAME migration transaction, whatever its pre-migration
    status. Proven two ways: (1) direct DB state -- both a 'queued' and a
    'running' loser job end up 'cancelled'; (2) behaviorally -- a real
    Worker, registered against a client_factory that raises on ANY HTTP call,
    finds nothing left to claim at all, so a full drain can never reach the
    daemon."""
    db_path = tmp_path / QUEUE_DB_NAME
    ws_a = str(tmp_path / "workspace-a")
    ws_b = str(tmp_path / "workspace-b")
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_MODEL_PULLS_DDL)
        con.executescript(_PRE_V3_MODEL_PULLS_DDL)

        # workspace A: survivor (no job yet) + loser with a QUEUED job.
        con.execute(
            "INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts,"
            " available_at, created_at) VALUES"
            " (101, 'model.pull', '{}', 'queued', 0, 3, ?, ?)",
            (_ISO, _ISO),
        )
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, job_id,"
            " created_at) VALUES (?, 'smollm:135m', 'pending', NULL, ?)",
            (ws_a, "2020-01-01T00:00:00.000000+00:00"),
        )
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, job_id,"
            " created_at) VALUES (?, 'qwen3:8b', 'pending', 101, ?)",
            (ws_a, "2020-01-02T00:00:00.000000+00:00"),
        )

        # workspace B: survivor (no job) + loser with a RUNNING job
        # (mid-upgrade -- claimed by a worker that is gone, lease expired).
        con.execute(
            "INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts,"
            " available_at, created_at, locked_by, lease_expires_at) VALUES"
            " (102, 'model.pull', '{}', 'running', 1, 3, ?, ?, 'stale-worker', ?)",
            (_ISO, _ISO, _ISO),
        )
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, job_id,"
            " created_at) VALUES (?, 'llama3:8b', 'pending', NULL, ?)",
            (ws_b, "2020-01-01T00:00:00.000000+00:00"),
        )
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, job_id,"
            " created_at) VALUES (?, 'phi3:mini', 'running', 102, ?)",
            (ws_b, "2020-01-02T00:00:00.000000+00:00"),
        )

        con.execute(
            "INSERT INTO frisket_queue_schema_migrations "
            "(version, applied_at, applying_code_identity) VALUES (1, ?, 'test'),"
            " (2, ?, 'test')",
            (_ISO, _ISO),
        )
        con.commit()
    finally:
        con.close()

    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        import sqlalchemy as sa

        from frisket.engine.jobs.queue import jobs_table

        with engine.connect() as cx:
            pulls = {
                row.model_ref: row
                for row in cx.execute(
                    sa.select(
                        model_pulls_table.c.model_ref,
                        model_pulls_table.c.status,
                        model_pulls_table.c.job_id,
                    )
                ).fetchall()
            }
            job_statuses = {
                row.id: row.status
                for row in cx.execute(
                    sa.select(jobs_table.c.id, jobs_table.c.status)
                ).fetchall()
            }

        # Both loser rows AND their linked jobs are terminalized -- not just
        # the pull row.
        assert pulls["qwen3:8b"].status == "failed"
        assert job_statuses[pulls["qwen3:8b"].job_id] == "cancelled"
        assert pulls["phi3:mini"].status == "failed"
        assert job_statuses[pulls["phi3:mini"].job_id] == "cancelled"

        # The survivors are untouched (no job linked in this scenario).
        assert pulls["smollm:135m"].status == "pending"
        assert pulls["llama3:8b"].status == "pending"

        # Behavioral proof: nothing is left claimable, so a worker running
        # against a client_factory that raises on ANY HTTP call never even
        # attempts to contact a daemon.
        import httpx

        from frisket.engine.jobs.model_pull import register_model_pull_handler
        from frisket.engine.jobs.worker import HandlerRegistry, Worker

        def poison(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                "must never contact a daemon for a migration-superseded job"
            )

        registry = HandlerRegistry()
        register_model_pull_handler(
            registry,
            workspace_root=tmp_path,
            queue=queue,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(poison)),
        )
        worker = Worker(queue, registry, retry_base_seconds=0.0, lease_seconds=60)
        assert worker.run_once() is False, (
            "a migration-superseded job was still claimable by a worker"
        )
    finally:
        queue.close()

    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))


def test_strict_validation_passes_a_fully_migrated_queue(tmp_path) -> None:
    import sqlalchemy as sa

    from frisket.engine.jobs.queue_migrations import validate_queue_schema

    queue = open_queue(workspace=tmp_path)
    queue.close()
    # validate_queue_schema is read-only; a fresh disposed-engine reconnect
    # exercises it exactly like a strict-mode app/worker startup would.
    engine2 = sa.create_engine(f"sqlite:///{tmp_path / QUEUE_DB_NAME}", future=True)
    try:
        validate_queue_schema(engine2)  # must not raise
    finally:
        engine2.dispose()


def test_strict_validation_fails_closed_on_missing_model_pulls_columns(
    tmp_path,
) -> None:
    import sqlalchemy as sa

    from frisket.engine.jobs.queue_migrations import (
        QueueSchemaError,
        validate_queue_schema,
    )

    db_path = tmp_path / QUEUE_DB_NAME
    queue = open_queue(workspace=tmp_path)
    queue.close()

    con = sqlite3.connect(db_path)
    try:
        # Simulate a table that predates the v3 hardening columns (item 3)
        # slipping past a broken/partial migration -- table PRESENT, but not
        # the required shape.
        con.execute(
            "ALTER TABLE model_pulls RENAME COLUMN endpoint_origin TO x_removed"
        )
        con.commit()
    finally:
        con.close()

    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with pytest.raises(QueueSchemaError, match="model_pulls"):
            validate_queue_schema(engine)
    finally:
        engine.dispose()


def test_strict_validation_fails_closed_on_missing_model_pulls_index(
    tmp_path,
) -> None:
    import sqlalchemy as sa

    from frisket.engine.jobs.queue_migrations import (
        QueueSchemaError,
        validate_queue_schema,
    )

    db_path = tmp_path / QUEUE_DB_NAME
    queue = open_queue(workspace=tmp_path)
    queue.close()

    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP INDEX uq_model_pulls_active_workspace")
        con.commit()
    finally:
        con.close()

    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with pytest.raises(QueueSchemaError, match="model_pulls"):
            validate_queue_schema(engine)
    finally:
        engine.dispose()


# A v3-shaped model_pulls (hardening columns + workspace index present) but
# WITHOUT the v4 artifact columns -- the real pre-v4 shape v4 must retrofit.
_PRE_V4_MODEL_PULLS_DDL = """
CREATE TABLE model_pulls (
  id INTEGER PRIMARY KEY,
  workspace_root TEXT NOT NULL,
  model_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  job_id INTEGER,
  phase TEXT,
  total_bytes INTEGER,
  completed_bytes INTEGER,
  error_code TEXT,
  error_message TEXT,
  correlation_id TEXT,
  resolved_digest TEXT,
  resolved_size INTEGER,
  cancel_requested_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  endpoint_origin TEXT,
  initiated_by TEXT
);
CREATE UNIQUE INDEX uq_model_pulls_active_ref ON model_pulls(workspace_root, model_ref)
  WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX uq_model_pulls_active_workspace ON model_pulls(workspace_root)
  WHERE status IN ('pending', 'running');
"""


def test_v4_migration_adds_artifact_columns_and_pre_v4_rows_default_to_ollama(
    tmp_path,
) -> None:
    """A pre-v4 ``.queue.db`` (v3 model_pulls, missing the four artifact
    columns) retrofits in place, its existing row survives,
    and that row reads back with ``artifact_kind`` NULL -> effective 'ollama'
    (the read convention) so its DTO carries a null ``artifact`` object."""
    from frisket.engine.jobs import model_pull_store as store

    db_path = tmp_path / QUEUE_DB_NAME
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_MODEL_PULLS_DDL)
        con.executescript(_PRE_V4_MODEL_PULLS_DDL)
        # v3-shaped worker_heartbeats already carries `kinds`; the base DDL's
        # heartbeat table is pre-v3, so add it to satisfy v3's hardening pass.
        con.execute("ALTER TABLE worker_heartbeats ADD COLUMN kinds TEXT")
        con.execute(
            "INSERT INTO model_pulls (workspace_root, model_ref, status, created_at)"
            " VALUES (?, ?, 'done', ?)",
            (str(tmp_path), "smollm:135m", _ISO),
        )
        con.execute(
            "INSERT INTO frisket_queue_schema_migrations "
            "(version, applied_at, applying_code_identity) VALUES (1, ?, 'test'),"
            " (2, ?, 'test'), (3, ?, 'test')",
            (_ISO, _ISO, _ISO),
        )
        con.commit()
    finally:
        con.close()

    assert "artifact_kind" not in _columns(db_path, "model_pulls")

    queue = open_queue(workspace=tmp_path)
    try:
        import sqlalchemy as sa

        engine = queue.engine
        with engine.connect() as cx:
            raw = cx.execute(
                sa.select(model_pulls_table.c.id).where(
                    model_pulls_table.c.model_ref == "smollm:135m"
                )
            ).first()
        assert raw is not None
        row = store.get(engine, raw.id)
    finally:
        queue.close()

    columns = _columns(db_path, "model_pulls")
    assert {
        "artifact_kind",
        "artifact_source_url",
        "artifact_license",
        "artifact_manifest_version",
    } <= columns
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))

    assert row is not None
    assert row.artifact_kind is None
    assert row.effective_artifact_kind == "ollama"
    dto = store.to_dto(row)
    assert dto["schemaVersion"] == "frisket.model_pull.v3"
    assert dto["artifact"] is None


def test_v4_artifact_metadata_roundtrips_into_the_dto(tmp_path) -> None:
    """A pull row stamped with artifact provenance surfaces a populated nested
    ``artifact`` object in the DTO."""
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
    assert fresh.effective_artifact_kind == "ct2_pair"
    dto = store.to_dto(fresh)
    assert dto["artifact"] == {
        "kind": "ct2_pair",
        "source_url": "https://huggingface.co/frisket-models/x@rev",
        "license": "CC-BY-4.0",
        "manifest_version": "2026.07.1",
    }


_A29_MODEL_PULL_COLUMNS = {
    "id",
    "workspace_root",
    "model_ref",
    "status",
    "job_id",
    "phase",
    "total_bytes",
    "completed_bytes",
    "error_code",
    "error_message",
    "correlation_id",
    "resolved_digest",
    "resolved_size",
    "cancel_requested_at",
    "created_at",
    "started_at",
    "finished_at",
    "endpoint_origin",
    "initiated_by",
    "artifact_kind",
    "artifact_source_url",
    "artifact_license",
    "artifact_manifest_version",
}


def test_v9_upgrades_actual_a29_v8_model_pull_shape_and_recovers_rows(
    tmp_path,
) -> None:
    """Upgrade the exact model_pulls shape shipped by v0.1.1a29 (08fd6846).

    Terminal legacy history survives. A current qualified ref can be
    backfilled deterministically. An a29 local pull still in flight cannot be
    assigned one of today's endpoint ids safely, so it and its queued job are
    terminalized with an actionable retry message. A pending non-local
    artifact remains untouched even before its artifact metadata is stamped.
    """
    from frisket.engine.jobs import model_pull_store as store
    from frisket.engine.jobs.queue_migrations import validate_queue_schema

    db_path = tmp_path / QUEUE_DB_NAME
    fresh = open_queue(workspace=tmp_path)
    fresh.close()

    workspaces = {
        "terminal": str(tmp_path / "terminal"),
        "legacy_active": str(tmp_path / "legacy-active"),
        "qualified": str(tmp_path / "qualified"),
        "artifact": str(tmp_path / "artifact"),
        "wrong_kind": str(tmp_path / "wrong-kind"),
        "wrong_payload": str(tmp_path / "wrong-payload"),
    }
    con = sqlite3.connect(db_path)
    try:
        con.execute("ALTER TABLE model_pulls DROP COLUMN endpoint_id")
        con.execute(f"DELETE FROM {QUEUE_SCHEMA_LEDGER} WHERE version >= 9")
        con.executemany(
            "INSERT INTO jobs "
            "(id, kind, payload, status, attempts, max_attempts, available_at, created_at) "
            "VALUES (?, 'model.pull', ?, 'queued', 0, 3, ?, ?)",
            (
                (
                    101,
                    json.dumps(
                        {
                            "pull_id": 2,
                            "workspace_root": workspaces["legacy_active"],
                            "server_scoped": True,
                        }
                    ),
                    _ISO,
                    _ISO,
                ),
                (
                    102,
                    json.dumps(
                        {
                            "pull_id": 3,
                            "workspace_root": workspaces["qualified"],
                            "endpoint_id": "studio",
                            "server_scoped": True,
                        }
                    ),
                    _ISO,
                    _ISO,
                ),
                (
                    103,
                    json.dumps(
                        {
                            "pull_id": 4,
                            "workspace_root": workspaces["artifact"],
                            "server_scoped": True,
                        }
                    ),
                    _ISO,
                    _ISO,
                ),
                (
                    104,
                    json.dumps(
                        {
                            "pull_id": 5,
                            "workspace_root": workspaces["wrong_kind"],
                        }
                    ),
                    _ISO,
                    _ISO,
                ),
                (
                    105,
                    json.dumps(
                        {
                            "pull_id": 999,
                            "workspace_root": workspaces["wrong_payload"],
                            "server_scoped": True,
                        }
                    ),
                    _ISO,
                    _ISO,
                ),
            ),
        )
        con.execute("UPDATE jobs SET kind = 'echo' WHERE id = 104")
        con.executemany(
            "INSERT INTO model_pulls "
            "(id, workspace_root, model_ref, status, job_id, created_at, "
            " endpoint_origin, artifact_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    1,
                    workspaces["terminal"],
                    "llama3:8b",
                    "done",
                    None,
                    _ISO,
                    "http://127.0.0.1:11434",
                    None,
                ),
                (
                    2,
                    workspaces["legacy_active"],
                    "smollm:135m",
                    "pending",
                    101,
                    _ISO,
                    "http://127.0.0.1:11434",
                    None,
                ),
                (
                    3,
                    workspaces["qualified"],
                    "ollama/@studio/qwen3:8b",
                    "pending",
                    102,
                    _ISO,
                    "http://127.0.0.1:1234",
                    None,
                ),
                (
                    4,
                    workspaces["artifact"],
                    "opus-mt:en-es",
                    "pending",
                    103,
                    _ISO,
                    None,
                    None,
                ),
                (
                    5,
                    workspaces["wrong_kind"],
                    "legacy-wrong-kind:7b",
                    "pending",
                    104,
                    _ISO,
                    "http://127.0.0.1:11434",
                    None,
                ),
                (
                    6,
                    workspaces["wrong_payload"],
                    "legacy-wrong-payload:7b",
                    "pending",
                    105,
                    _ISO,
                    "http://127.0.0.1:11434",
                    None,
                ),
            ),
        )
        con.commit()
    finally:
        con.close()

    assert _columns(db_path, "model_pulls") == _A29_MODEL_PULL_COLUMNS
    assert _ledger_versions(db_path) == list(range(1, 9))

    migrated = open_queue(workspace=tmp_path)
    try:
        validate_queue_schema(migrated.engine)
        rows = {pull_id: store.get(migrated.engine, pull_id) for pull_id in range(1, 7)}
        jobs = {job_id: migrated.get(job_id) for job_id in range(101, 106)}
        assert store.list_recent(migrated.engine, workspaces["terminal"])
        stale_reactivated = store.mark_running(migrated.engine, 2, job_id=101)
    finally:
        migrated.close()

    assert "endpoint_id" in _columns(db_path, "model_pulls")
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1))
    assert rows[1] is not None and rows[1].status == "done"
    assert rows[1].endpoint_id is None
    assert rows[2] is not None and rows[2].status == "cancelled"
    assert rows[2].error_code == "endpoint_identity_missing"
    assert rows[2].endpoint_id is None
    assert jobs[101] is not None and jobs[101].status == "cancelled"
    assert stale_reactivated is False
    assert rows[3] is not None and rows[3].status == "pending"
    assert rows[3].endpoint_id == "studio"
    assert jobs[102] is not None and jobs[102].status == "queued"
    assert rows[4] is not None and rows[4].status == "pending"
    assert rows[4].endpoint_id is None
    assert jobs[103] is not None and jobs[103].status == "queued"
    assert rows[5] is not None and rows[5].status == "cancelled"
    assert jobs[104] is not None and jobs[104].status == "queued"
    assert rows[6] is not None and rows[6].status == "cancelled"
    assert jobs[105] is not None and jobs[105].status == "queued"
