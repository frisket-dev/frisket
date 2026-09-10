"""RED-first real-Postgres checks for single-owner queue schema migration."""

from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy as sa

from tests.runtime_foundation_test_helpers import (
    RUN_QUEUE_RUNTIME_PASSWORD,
    execute,
    postgres_server,
    queue_cli,
    queue_cli_process,
    queue_schema_fingerprint,
    require_ok,
    rows,
    run,
    run_waiting_on_advisory_lock,
    scalar,
)


ADMIN_ENV = "FRISKET_DATABASE_ADMIN_URL"
QUEUE_ENV = "FRISKET_RUN_QUEUE_DATABASE_URL"
SCHEMA_MODE_ENV = "FRISKET_RUN_QUEUE_SCHEMA_MODE"
STORAGE_RECONCILIATION_INDEX = "idx_jobs_storage_project_status_id"
pytestmark = [pytest.mark.gap, pytest.mark.gap_env]


def _queue_migrations_module() -> ModuleType:
    module_name = "frisket.engine.jobs.queue_migrations"
    assert importlib.util.find_spec(module_name) is not None, (
        "missing product seam: frisket.jobs.queue_migrations must own ordered "
        "queue migrations and QUEUE_SCHEMA_VERSION"
    )
    module = importlib.import_module(module_name)
    for symbol in (
        "QUEUE_SCHEMA_VERSION",
        "QUEUE_SCHEMA_MIGRATION_LOCK_ID",
        "MIGRATIONS",
    ):
        assert hasattr(module, symbol), f"{module_name} is missing required {symbol}"
    return module


def _provision(postgres) -> None:
    require_ok(
        queue_cli(
            "provision",
            env={
                ADMIN_ENV: postgres.admin_url,
                QUEUE_ENV: postgres.runtime_url,
            },
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

    assert "schema_mode" in inspect.signature(PostgresJobQueue).parameters, (
        "PostgresJobQueue has no explicit strict schema-mode constructor seam"
    )
    return PostgresJobQueue(url, schema_mode="strict")


def _product_env(
    postgres,
    tmp_path: Path,
    label: str,
    *,
    queue_url: str | None = None,
) -> dict[str, str]:
    return {
        QUEUE_ENV: queue_url or postgres.runtime_url,
        "FRISKET_DATABASE_URL": f"sqlite:///{tmp_path / f'control-{label}.db'}",
        "FRISKET_DATA_DIR": str(tmp_path / f"data-{label}"),
        SCHEMA_MODE_ENV: "strict",
        "FRISKET_SOURCE_SCHEDULER_INTERVAL": "0",
        "FRISKET_EMBEDDING_SCHEDULER_INTERVAL": "0",
        "FRISKET_NOTIFICATION_DIGEST_SCHEDULER_INTERVAL": "0",
    }


def _worker_startup(
    postgres,
    tmp_path: Path,
    label: str,
    *,
    queue_url: str | None = None,
):
    return run(
        [
            sys.executable,
            "-m",
            "frisket.cli",
            "hosted-worker",
            "--drain",
            "--poll-interval",
            "0.01",
            "--schedule-sources-interval",
            "0",
            "--schedule-embeddings-interval",
            "0",
            "--schedule-notification-digests-interval",
            "0",
        ],
        env=_product_env(
            postgres,
            tmp_path,
            f"worker-{label}",
            queue_url=queue_url,
        ),
        timeout=40,
    )


def _hosted_worker_with_cli_locator(postgres, tmp_path: Path):
    return run(
        [
            sys.executable,
            "-m",
            "frisket.cli",
            "hosted-worker",
            "--database-url",
            postgres.runtime_url,
            "--drain",
        ],
        env=_product_env(postgres, tmp_path, "worker-cli-locator"),
        timeout=40,
    )


def _assert_strict_refuses_without_ddl(url: str, *, match: str) -> None:
    before = queue_schema_fingerprint(url)
    with pytest.raises((RuntimeError, ValueError), match=match):
        _strict_queue(url)
    after = queue_schema_fingerprint(url)
    assert after == before, "strict app/worker startup executed queue DDL"


def _assert_schema_presence_and_version(postgres, *, expected_version: int) -> None:
    """Compact presence-level schema check: names and ledger version only.

    Deliberately not byte-exact: it does not compare opclass, collation,
    null ordering, predicates, or include columns.
    """
    tables = {
        str(row["table_name"])
        for row in rows(
            postgres.queue_url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'",
        )
    }
    for table in ("jobs", "worker_heartbeats", "frisket_queue_schema_migrations"):
        assert table in tables, f"queue schema is missing table {table}"

    columns = {
        str(row["column_name"])
        for row in rows(
            postgres.queue_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='jobs'",
        )
    }
    for column in (
        "id",
        "kind",
        "payload",
        "status",
        "attempts",
        "max_attempts",
        "available_at",
        "created_at",
        "storage_org_id",
        "project_id",
    ):
        assert column in columns, f"jobs table is missing column {column}"

    indexes = {
        str(row["indexname"])
        for row in rows(
            postgres.queue_url,
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='jobs'",
        )
    }
    assert STORAGE_RECONCILIATION_INDEX in indexes

    version = scalar(
        postgres.queue_url,
        "SELECT max(version) FROM frisket_queue_schema_migrations",
    )
    assert version == expected_version, (
        f"queue schema migration ledger version {version!r} does not match "
        f"expected {expected_version!r}"
    )


def _seed_real_preledger_prior_queue_schema(postgres) -> None:
    """Create the schema that existed immediately before storage identity v1."""
    from frisket.engine.jobs.queue import jobs_metadata

    engine = sa.create_engine(postgres.queue_url, future=True)
    try:
        jobs_metadata.create_all(engine)
    finally:
        engine.dispose()
    execute(
        postgres.queue_url,
        f"DROP INDEX {STORAGE_RECONCILIATION_INDEX}",
    )
    execute(
        postgres.queue_url,
        "ALTER TABLE jobs DROP COLUMN storage_org_id",
    )
    assert (
        rows(
            postgres.queue_url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' "
            "AND table_name='frisket_queue_schema_migrations'",
        )
        == []
    )
    assert (
        rows(
            postgres.queue_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='jobs' "
            "AND column_name='storage_org_id'",
        )
        == []
    )


def _assert_job_queue_storage_scope_interface(
    *,
    tmp_path: Path,
    project_id: str,
    storage_org_id: int,
    other_storage_org_id: int,
) -> None:
    from frisket.engine.jobs.queue import (
        ACTION_RUN_KIND,
        PROJECT_RUN_KIND,
        JobQueue,
        PostgresJobQueue,
        SqliteJobQueue,
    )

    for owner in (JobQueue, SqliteJobQueue, PostgresJobQueue):
        for method_name in (
            "find_job_by_refs",
            "get_project_run_job",
            "list_project_jobs",
        ):
            parameter = inspect.signature(getattr(owner, method_name)).parameters.get(
                "storage_org_id"
            )
            assert parameter is not None, (
                f"{owner.__name__}.{method_name} omitted the shared storage scope"
            )
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is None

    queue = SqliteJobQueue(tmp_path / "storage-scope-interface.queue.db")
    try:
        dedupe_key = "sqlite-storage-scope"
        action_a = queue.enqueue(
            ACTION_RUN_KIND,
            {
                "project_id": project_id,
                "storage_org_id": storage_org_id,
                "dedupe_key": dedupe_key,
            },
        )
        action_b = queue.enqueue(
            ACTION_RUN_KIND,
            {
                "project_id": project_id,
                "storage_org_id": other_storage_org_id,
                "dedupe_key": dedupe_key,
            },
        )
        run_a = queue.enqueue(
            PROJECT_RUN_KIND,
            {
                "project_id": project_id,
                "storage_org_id": storage_org_id,
                "run_id": 8801,
            },
        )
        run_b = queue.enqueue(
            PROJECT_RUN_KIND,
            {
                "project_id": project_id,
                "storage_org_id": other_storage_org_id,
                "run_id": 8801,
            },
        )
        assert (
            queue.find_job_by_refs(
                ACTION_RUN_KIND,
                project_id=project_id,
                storage_org_id=storage_org_id,
                dedupe_key=dedupe_key,
            ).id
            == action_a
        )
        assert (
            queue.find_job_by_refs(
                ACTION_RUN_KIND,
                project_id=project_id,
                storage_org_id=other_storage_org_id,
                dedupe_key=dedupe_key,
            ).id
            == action_b
        )
        assert (
            queue.get_project_run_job(
                project_id,
                8801,
                storage_org_id=storage_org_id,
            ).id
            == run_a
        )
        assert (
            queue.get_project_run_job(
                project_id,
                8801,
                storage_org_id=other_storage_org_id,
            ).id
            == run_b
        )
        jobs_a = queue.list_project_jobs(
            project_id,
            storage_org_id=storage_org_id,
        )
        jobs_b = queue.list_project_jobs(
            project_id,
            storage_org_id=other_storage_org_id,
        )
        assert action_a in {job.id for job in jobs_a}
        assert action_b not in {job.id for job in jobs_a}
        assert action_b in {job.id for job in jobs_b}
        assert action_a not in {job.id for job in jobs_b}
    finally:
        queue.close()


class _CapturedProjectPath(RuntimeError):
    pass


def _assert_hosted_handlers_ignore_poisoned_workspace_root(
    *,
    monkeypatch: pytest.MonkeyPatch,
    queue,
    projects_root: Path,
    project_id: str,
    expected_key,
    poison_root: Path,
) -> None:
    from frisket.engine.jobs import (
        enclosures,
        notifications_delivery,
        notifications_digest,
        runs as run_jobs,
        sources,
        watches,
    )
    from frisket.engine.jobs.queue import (
        ACTION_RUN_KIND,
        CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
        PROJECT_RUN_KIND,
    )
    from frisket.engine.jobs.ports import JobHandlerContext
    from frisket.engine.jobs.worker import HandlerRegistry, register_hosted_handlers

    opened_paths: list[Path] = []
    delegated_roots: list[Path] = []

    def capture_project(path, *_args, **_kwargs):
        opened_paths.append(Path(path))
        raise _CapturedProjectPath("captured project path")

    def capture_delivery(*, workspace_root, **_kwargs):
        delegated_roots.append(Path(workspace_root))
        raise _CapturedProjectPath("captured notification delivery root")

    def capture_digest(*, workspace_root, **_kwargs):
        delegated_roots.append(Path(workspace_root))
        raise _CapturedProjectPath("captured notification digest root")

    for module in (run_jobs, watches, sources, enclosures):
        monkeypatch.setattr(module, "Project", capture_project)
    monkeypatch.setattr(
        notifications_delivery,
        "deliver_notification_request",
        capture_delivery,
    )
    monkeypatch.setattr(
        notifications_digest,
        "compose_notification_digest_job",
        capture_digest,
    )

    registry = HandlerRegistry()
    register_hosted_handlers(
        registry,
        workspace_root=projects_root,
        queue=queue,
        notification_delivery_runtime=object(),
    )
    base = {"project_id": project_id, "workspace_root": str(poison_root)}
    handler_payloads = (
        (PROJECT_RUN_KIND, {**base, "run_id": 71}),
        (ACTION_RUN_KIND, dict(base)),
        (watches.WATCH_EVALUATE_KIND, {**base, "watch_id": 72}),
        (sources.SOURCE_POLL_KIND, {**base, "source_id": 73}),
        (
            enclosures.ENCLOSURE_DOWNLOAD_KIND,
            {**base, "sheet_id": 74, "row_id": 75},
        ),
        (
            notifications_delivery.NOTIFICATION_DELIVER_KIND,
            {**base, "request_id": 77, "job_id": 78},
        ),
        (
            notifications_digest.NOTIFICATION_DIGEST_KIND,
            {
                **base,
                "route_id": 79,
                "cadence": "daily",
                "window_key": "2026-07-10",
                "window_start_at": "2026-07-10T00:00:00Z",
                "window_end_at": "2026-07-11T00:00:00Z",
            },
        ),
    )
    expected_path = (
        projects_root
        / str(expected_key.storage_org_id)
        / f"{expected_key.project_slug}.frisket"
    )
    notification_kinds = {
        notifications_delivery.NOTIFICATION_DELIVER_KIND,
        notifications_digest.NOTIFICATION_DIGEST_KIND,
    }
    for kind, payload in handler_payloads:
        handler = registry.get(kind)
        assert handler is not None, f"hosted registry omitted {kind}"

        opened_paths.clear()
        delegated_roots.clear()
        with pytest.raises(_CapturedProjectPath):
            handler(
                {
                    **payload,
                    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: expected_key,
                },
                JobHandlerContext.without_job_row(),
            )
        if kind in notification_kinds:
            assert delegated_roots == [expected_path.parent]
            assert opened_paths == []
        else:
            assert opened_paths == [expected_path]
            assert delegated_roots == []

        opened_paths.clear()
        delegated_roots.clear()
        with pytest.raises(
            (RuntimeError, ValueError),
            match="(?i)storage|claim|identity",
        ):
            handler(dict(payload))
        assert opened_paths == [] and delegated_roots == [], (
            f"hosted {kind} opened a payload-selected project without a claimed "
            "ProjectStorageKey"
        )


def _assert_actual_hosted_enqueue_producers_persist_storage_identity(
    *,
    monkeypatch: pytest.MonkeyPatch,
    storage_root: Path,
    project_id: str,
    storage_org_id: int,
) -> None:
    from frisket.engine.jobs import (
        embeddings,
        notifications_delivery,
        notifications_digest,
        sources,
        watches,
    )

    enqueued: list[tuple[str, dict]] = []

    class RecordingQueue:
        def find_job_by_refs(self, _kind, **kwargs):
            assert kwargs.get("storage_org_id") == storage_org_id
            return None

        def enqueue(self, kind, payload, **_kwargs):
            assert payload.get("storage_org_id") == storage_org_id, (
                f"actual {kind} producer omitted first-class storage identity"
            )
            enqueued.append((str(kind), dict(payload)))
            return 9000 + len(enqueued)

    queue = RecordingQueue()
    project = object()

    monkeypatch.setattr(
        watches,
        "find_embedding_similarity_watches",
        lambda *_args, **_kwargs: [{"id": 801}],
    )
    assert watches.enqueue_index_watch_evaluations(
        queue,
        project=project,
        project_id=project_id,
        workspace_root=storage_root,
        storage_org_id=storage_org_id,
        index_id="index-801",
        trigger_ref={"source_run_id": 801},
    )

    monkeypatch.setattr(
        sources,
        "_enclosure_row_already_queued_or_downloaded",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        sources,
        "mark_enclosure_queued",
        lambda *_args, **_kwargs: None,
    )
    assert sources.enqueue_enclosure_downloads(
        queue,
        project=project,
        project_id=project_id,
        workspace_root=storage_root,
        storage_org_id=storage_org_id,
        source={"config": {"download_enclosures": "queued"}},
        sheet_id=802,
        row_ids=[803],
    )

    class FakeProject:
        def __init__(self, _path):
            pass

        def close(self):
            pass

        def notification_delivery_request(self, _request_id):
            return {"status": "queued", "job_id": None}

        def set_notification_delivery_request_job_id(self, _request_id, _job_id):
            pass

        def notification_routes(self, *, enabled_only):
            assert enabled_only
            return [{"id": 805, "delivery_mode": "digest"}]

        def notification_digest_run_by_route_window(self, **_kwargs):
            return None

    monkeypatch.setattr(notifications_delivery, "Project", FakeProject)
    notifications_delivery.enqueue_notification_delivery(
        queue,
        workspace_root=storage_root,
        project_id=project_id,
        storage_org_id=storage_org_id,
        request_id=804,
    )

    digest_window = notifications_digest.NotificationDigestWindow(
        cadence="daily",
        window_key="2026-07-10",
        start_at=datetime(2026, 7, 10, tzinfo=UTC),
        end_at=datetime(2026, 7, 11, tzinfo=UTC),
    )
    monkeypatch.setattr(notifications_digest, "Project", FakeProject)
    monkeypatch.setattr(
        notifications_digest,
        "due_digest_window_for_route",
        lambda *_args, **_kwargs: digest_window,
    )
    assert (
        notifications_digest.enqueue_due_notification_digests(
            queue,
            workspace_root=storage_root,
            project_id=project_id,
            storage_org_id=storage_org_id,
        )
        == 1
    )

    class FakeSourceStore:
        def __init__(self, _project):
            pass

        def sources(self):
            return [{"id": 806, "config": "{}"}]

    monkeypatch.setattr(sources, "Project", FakeProject)
    monkeypatch.setattr(sources, "SourceStore", FakeSourceStore)
    monkeypatch.setattr(sources, "_source_due", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        sources,
        "_source_stale_for_notification",
        lambda *_args, **_kwargs: False,
    )
    assert sources.enqueue_due_source_polls(
        workspace_root=storage_root,
        queue=queue,
        storage_org_id=storage_org_id,
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )

    launch_payloads: list[dict] = []
    from frisket.engine.executor import action_jobs

    monkeypatch.setattr(
        action_jobs,
        "reserve_queued_action_job_receipt",
        lambda *_args, **_kwargs: {
            "action_id": "action-807",
            "receipt_id": "receipt-807",
            "params_hash": "sha256:807",
        },
    )

    def capture_embedding_launch(**kwargs):
        envelope = kwargs["envelope"]
        assert isinstance(envelope, action_jobs.ActionJobEnvelope)
        assert envelope.project_id == project_id
        assert envelope.action_kind == "embedding.index_refresh"
        assert envelope.resolve_phase == "worker"
        assert envelope.action["action_id"] == "embedding.index_refresh"
        assert envelope.action["scope"] == {"kind": "project"}
        assert envelope.action["params"]["index_id"] == "index-807"
        assert envelope.action["params"]["row_scope"] == {
            "kind": "row_ids",
            "row_ids": [808],
        }
        payload_extra = dict(kwargs.get("payload_extra") or {})
        assert payload_extra.get("storage_org_id") == storage_org_id
        launch_payloads.append(payload_extra)
        return SimpleNamespace(job_id=9807)

    monkeypatch.setattr(
        embeddings,
        "launch_queued_action_job",
        capture_embedding_launch,
    )
    monkeypatch.setattr(
        embeddings,
        "find_on_source_append_indexes",
        lambda *_args, **_kwargs: [{"id": "index-807"}],
    )
    assert embeddings.enqueue_source_append_refreshes(
        queue,
        project=project,
        project_id=project_id,
        workspace_root=storage_root,
        storage_org_id=storage_org_id,
        sheet_id=807,
        row_ids=[808],
        trigger_ref={"source_run_id": 807},
    ) == [9807]
    assert launch_payloads

    assert {kind for kind, _payload in enqueued} >= {
        watches.WATCH_EVALUATE_KIND,
        sources.ENCLOSURE_DOWNLOAD_KIND,
        sources.SOURCE_POLL_KIND,
        notifications_delivery.NOTIFICATION_DELIVER_KIND,
        notifications_digest.NOTIFICATION_DIGEST_KIND,
    }


def test_one_shot_migrator_ledgers_version_under_advisory_lock() -> None:
    queue_migrations = _queue_migrations_module()

    assert isinstance(queue_migrations.QUEUE_SCHEMA_VERSION, int)
    assert queue_migrations.QUEUE_SCHEMA_VERSION > 0
    assert tuple(m.version for m in queue_migrations.MIGRATIONS) == tuple(
        range(1, queue_migrations.QUEUE_SCHEMA_VERSION + 1)
    )
    assert isinstance(queue_migrations.QUEUE_SCHEMA_MIGRATION_LOCK_ID, int)

    with postgres_server() as postgres:
        _provision(postgres)
        _seed_real_preledger_prior_queue_schema(postgres)
        first = run_waiting_on_advisory_lock(
            postgres.queue_url,
            lock_id=queue_migrations.QUEUE_SCHEMA_MIGRATION_LOCK_ID,
            start=lambda: queue_cli_process(
                "migrate",
                env={
                    ADMIN_ENV: postgres.admin_url,
                    QUEUE_ENV: postgres.runtime_url,
                },
            ),
        )
        require_ok(first, what="first queue schema migration")
        first_output = first.stdout + first.stderr
        assert postgres.admin_url not in first_output
        assert postgres.runtime_url not in first_output
        assert RUN_QUEUE_RUNTIME_PASSWORD not in first_output
        assert rows(
            postgres.queue_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='jobs' "
            "AND column_name='storage_org_id'",
        ) == [{"column_name": "storage_org_id"}]
        assert rows(
            postgres.queue_url,
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='jobs' AND indexname=:index_name",
            {"index_name": STORAGE_RECONCILIATION_INDEX},
        ) == [{"indexname": STORAGE_RECONCILIATION_INDEX}]
        exact = _strict_queue(postgres.runtime_url)
        exact.close()
        ledger = rows(
            postgres.queue_url,
            "SELECT version, applied_at, applying_code_identity "
            "FROM frisket_queue_schema_migrations ORDER BY version",
        )
        assert [row["version"] for row in ledger] == list(
            range(1, queue_migrations.QUEUE_SCHEMA_VERSION + 1)
        )
        assert all(row["applied_at"] is not None for row in ledger)
        assert all(
            row["applying_code_identity"] and row["applying_code_identity"] != "unknown"
            for row in ledger
        )

        require_ok(_migrate(postgres), what="idempotent queue schema migration")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            concurrent_procs = list(
                pool.map(lambda _index: _migrate(postgres), range(2))
            )
        for proc in concurrent_procs:
            require_ok(proc, what="concurrent advisory-locked migration")
        after = rows(
            postgres.queue_url,
            "SELECT version, count(*) AS count "
            "FROM frisket_queue_schema_migrations GROUP BY version ORDER BY version",
        )
        assert after == [
            {"version": version, "count": 1}
            for version in range(1, queue_migrations.QUEUE_SCHEMA_VERSION + 1)
        ]


def test_v9_adds_endpoint_id_to_an_a29_v8_postgres_queue() -> None:
    """The hosted migration must ALTER the real pre-``endpoint_id`` Postgres shape."""
    queue_migrations = _queue_migrations_module()
    assert queue_migrations.QUEUE_SCHEMA_VERSION >= 9

    with postgres_server() as postgres:
        _provision(postgres)
        require_ok(_migrate(postgres), what="initial queue schema migration")
        execute(
            postgres.queue_url,
            "INSERT INTO jobs "
            "(id, kind, payload, status, attempts, max_attempts, available_at, created_at) "
            "VALUES (901, 'model.pull', :payload, 'queued', 0, 3, :now, :now)",
            {
                "payload": json.dumps(
                    {
                        "pull_id": 902,
                        "workspace_root": "/a29-workspace",
                        "server_scoped": True,
                    }
                ),
                "now": datetime(2026, 8, 29, tzinfo=UTC),
            },
        )
        execute(
            postgres.queue_url,
            "INSERT INTO model_pulls "
            "(id, workspace_root, model_ref, status, job_id, created_at, "
            " endpoint_origin) "
            "VALUES (902, '/a29-workspace', 'smollm:135m', 'pending', 901, "
            " :now, 'http://127.0.0.1:11434')",
            {"now": datetime(2026, 8, 29, tzinfo=UTC)},
        )
        execute(postgres.queue_url, "ALTER TABLE model_pulls DROP COLUMN endpoint_id")
        execute(
            postgres.queue_url,
            "DELETE FROM frisket_queue_schema_migrations WHERE version >= 9",
        )
        assert (
            rows(
                postgres.queue_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='model_pulls' "
                "AND column_name='endpoint_id'",
            )
            == []
        )

        require_ok(_migrate(postgres), what="a29/v8 to v9 queue schema migration")
        assert rows(
            postgres.queue_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='model_pulls' "
            "AND column_name='endpoint_id'",
        ) == [{"column_name": "endpoint_id"}]
        assert rows(
            postgres.queue_url,
            "SELECT status, error_code FROM model_pulls WHERE id = 902",
        ) == [{"status": "cancelled", "error_code": "endpoint_identity_missing"}]
        assert rows(
            postgres.queue_url,
            "SELECT status FROM jobs WHERE id = 901",
        ) == [{"status": "cancelled"}]
        exact = _strict_queue(postgres.runtime_url)
        exact.close()
