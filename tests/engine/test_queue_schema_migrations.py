"""Real-Postgres checks for the single-owner hosted queue schema upgrade."""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from frisket.engine.jobs.queue_schema import (
    QUEUE_SCHEMA_MIGRATION_LOCK_ID,
    QUEUE_SCHEMA_VERSION,
)
from tests.runtime_foundation_test_helpers import (
    RUN_QUEUE_RUNTIME_PASSWORD,
    execute,
    postgres_server,
    queue_cli,
    queue_cli_process,
    require_ok,
    rows,
    run_waiting_on_advisory_lock,
)


ADMIN_ENV = "FRISKET_DATABASE_ADMIN_URL"
QUEUE_ENV = "FRISKET_RUN_QUEUE_DATABASE_URL"
STORAGE_RECONCILIATION_INDEX = "idx_jobs_storage_project_status_id"
pytestmark = [pytest.mark.gap, pytest.mark.gap_env]


def _provision(postgres) -> None:
    require_ok(
        queue_cli(
            "provision",
            env={ADMIN_ENV: postgres.admin_url, QUEUE_ENV: postgres.runtime_url},
        ),
        what="queue database provision",
    )


def _migrate(postgres):
    return queue_cli(
        "migrate",
        env={ADMIN_ENV: postgres.admin_url, QUEUE_ENV: postgres.runtime_url},
    )


def _strict_queue(url: str):
    from frisket.engine.jobs.queue import PostgresJobQueue

    return PostgresJobQueue(url, schema_mode="strict")


def _seed_pre_ledger_queue(postgres, *jobs: tuple[int, str, str]) -> None:
    """The jobs shape from before the version ledger and storage identity."""
    from frisket.engine.jobs.queue import jobs_metadata

    engine = sa.create_engine(postgres.queue_url, future=True)
    try:
        jobs_metadata.tables["jobs"].create(engine)
    finally:
        engine.dispose()
    execute(postgres.queue_url, f"DROP INDEX {STORAGE_RECONCILIATION_INDEX}")
    execute(postgres.queue_url, "ALTER TABLE jobs DROP COLUMN storage_organization_id")
    now = datetime(2026, 7, 1, tzinfo=UTC)
    for job_id, kind, status in jobs:
        execute(
            postgres.queue_url,
            "INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts,"
            " available_at, created_at) VALUES (:id, :kind, '{}', :status, 0, 3, :now, :now)",
            {"id": job_id, "kind": kind, "status": status, "now": now},
        )


def test_migrate_replaces_an_older_queue_once_under_the_advisory_lock() -> None:
    with postgres_server() as postgres:
        _provision(postgres)
        _seed_pre_ledger_queue(
            postgres,
            (700, "project.run", "succeeded"),
            (701, "search.index", "queued"),
        )
        first = run_waiting_on_advisory_lock(
            postgres.queue_url,
            lock_id=QUEUE_SCHEMA_MIGRATION_LOCK_ID,
            start=lambda: queue_cli_process(
                "migrate",
                env={ADMIN_ENV: postgres.admin_url, QUEUE_ENV: postgres.runtime_url},
            ),
        )
        require_ok(first, what="first queue schema migration")
        output = first.stdout + first.stderr
        assert postgres.admin_url not in output
        assert postgres.runtime_url not in output
        assert RUN_QUEUE_RUNTIME_PASSWORD not in output

        ledger = rows(
            postgres.queue_url,
            "SELECT version, applying_code_identity FROM frisket_queue_schema_migrations",
        )
        assert [row["version"] for row in ledger] == [QUEUE_SCHEMA_VERSION]
        assert ledger[0]["applying_code_identity"] not in ("", "unknown")
        assert rows(postgres.queue_url, "SELECT id FROM jobs") == []

        queue = _strict_queue(postgres.runtime_url)
        try:
            # The runtime role can write the replacement tables, and project
            # stores' old ids (up to 701) are never handed out again.
            assert queue.enqueue("project.run", {"project_id": "alpha"}) == 702
            assert queue.job_execution_stopped(700) is True
        finally:
            queue.close()

        require_ok(_migrate(postgres), what="idempotent queue schema migration")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for proc in pool.map(lambda _index: _migrate(postgres), range(2)):
                require_ok(proc, what="concurrent advisory-locked migration")
        assert rows(postgres.queue_url, "SELECT id FROM jobs") == [{"id": 702}]
        assert rows(
            postgres.queue_url, "SELECT version FROM frisket_queue_schema_migrations"
        ) == [{"version": QUEUE_SCHEMA_VERSION}]


def test_migrate_refuses_an_older_queue_with_queued_user_work() -> None:
    with postgres_server() as postgres:
        _provision(postgres)
        _seed_pre_ledger_queue(postgres, (800, "project.run", "queued"))

        refused = _migrate(postgres)

        assert refused.returncode != 0
        assert "unfinished work (1 project.run)" in refused.stderr
        assert rows(postgres.queue_url, "SELECT id, status FROM jobs") == [
            {"id": 800, "status": "queued"}
        ]
        assert (
            rows(
                postgres.queue_url,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'frisket_queue_schema_migrations'",
            )
            == []
        )
