"""A queued run must resolve org BYOK
credentials from the TRUSTED queue org column, never the mutable JSON payload.

The physical project is already selected from the trusted storage-identity
columns (``claimed_project_location`` beats ``payload['project_id']``). The
credential org must come from the same trust boundary: the first-class
``org_id`` queue column written at enqueue and immutable in the row. A forged,
stale, or mis-migrated durable payload that names a DIFFERENT org than the
trusted column must never resolve that other org's keys against this org's
project (cross-tenant credential confusion, mis-billing, prompt exposure).

The worker loop is the same code over both backends; these run on a hosted
SQLite queue (``hosted=True`` declares the tenancy posture — see
queue-hosted-posture-explicit-v1) without needing Postgres.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sqlalchemy as sa

from frisket.engine.jobs import default_registry
from frisket.engine.jobs.queue import SqliteJobQueue, jobs_table
from frisket.engine.jobs.ports import JobHandlerContext, WorkerPorts
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.ai.llm import ModelRouter, ResponseCache
from frisket.engine.store import Project
from frisket.execution.provider import open_execution_composition
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec

TRUSTED_ORG = 7001  # the org that owns the stored project (trusted columns)
ATTACKER_ORG = 7002  # the org named by a forged/stale/mis-migrated payload


class _RecordingCredentialPort:
    """Records exactly which org each credential resolution asked for.

    Returns no keys (the template recipe needs none) so the run's success or
    failure is decided by the trust check, not by key availability.
    """

    def __init__(self) -> None:
        self.requested_org_ids: list[int] = []

    def provider_keys(self, *, org_id: int, control_database_url: str | None):
        self.requested_org_ids.append(int(org_id))
        return {}


class _TrackingResponseCache(ResponseCache):
    def __init__(self, path: Path, *, fail_close: bool = False) -> None:
        super().__init__(path)
        self.close_calls = 0
        self.fail_close = fail_close

    def close(self) -> None:
        self.close_calls += 1
        super().close()
        if self.fail_close:
            raise RuntimeError("cache close failed")


def _make_regex_project(project_dir: Path, *, slug: str) -> int:
    """Seed a project without creating the legacy claimless run."""

    project_dir.mkdir(parents=True, exist_ok=True)
    project = Project.create(project_dir / f"{slug}.frisket", name=slug)
    sheet = project.add_sheet("people")
    columns = {
        "first": project.add_column(sheet, "first"),
        "last": project.add_column(sheet, "last"),
    }
    project.add_rows(
        sheet,
        [{"first": "Ada", "last": "Lovelace"}],
        columns,
    )
    project.close()
    return sheet


def _enqueue_canonical_regex_run(
    workspace_root: Path,
    queue: SqliteJobQueue,
    *,
    project_id: str,
    sheet_id: int,
    queue_payload_extra: dict[str, int] | None = None,
) -> tuple[int, int]:
    """Reserve receipt/claim/attempt before publishing project.run."""

    workspace = Workspace(
        workspace_root,
        queue=queue,
        registry=HandlerRegistry(),
        queue_payload_extra=queue_payload_extra,
        enable_local_model_pull=False,
    )
    response = ActionRunService(workspace).run_action(
        project_id,
        v1_action_from_canonical_run_spec(
            queued_python_run_spec(sheet_id, "last", "display"),
            idempotency_key=f"{project_id}/worker-org-trust@sha256:stable",
        ),
    )
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "queued", response.payload
    return int(response.payload["run_id"]), int(response.payload["job_id"])


def _forge_payload_org(queue: SqliteJobQueue, job_id: int, org_id: int) -> None:
    """Simulate a forged/mis-migrated durable payload: rewrite the row's JSON
    ``org_id`` to a different org while leaving the trusted columns untouched."""
    with queue.engine.begin() as cx:
        raw = cx.execute(
            sa.text("SELECT payload FROM jobs WHERE id=:id"), {"id": job_id}
        ).scalar_one()
        body = json.loads(raw)
        body["org_id"] = org_id
        cx.execute(
            sa.text("UPDATE jobs SET payload=:p WHERE id=:id"),
            {"p": json.dumps(body), "id": job_id},
        )


def test_forged_payload_org_never_resolves_credentials_and_fails_closed(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "data"
    sheet = _make_regex_project(ws / str(TRUSTED_ORG), slug="queued")

    queue = SqliteJobQueue(tmp_path / "run.queue.db", hosted=True)
    port = _RecordingCredentialPort()
    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=None, cache_mode="off"),
        control_database_url=f"sqlite:///{tmp_path / 'control.db'}",
        require_storage_identity=True,
        worker_ports=WorkerPorts(credential_port=port),
    )

    _run_id, job_id = _enqueue_canonical_regex_run(
        ws / str(TRUSTED_ORG),
        queue,
        project_id="queued",
        sheet_id=sheet,
        queue_payload_extra={
            "org_id": TRUSTED_ORG,
            "storage_org_id": TRUSTED_ORG,
        },
    )
    # The trusted columns still name org A; only the mutable JSON is forged.
    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update().where(jobs_table.c.id == job_id).values(max_attempts=3)
        )
    _forge_payload_org(queue, job_id, ATTACKER_ORG)

    Worker(queue, registry, worker_id="h2-trust").run_once()

    # The attacker's org keys were NEVER resolved for org A's stored project.
    assert ATTACKER_ORG not in port.requested_org_ids, port.requested_org_ids
    # Fail closed: a payload/column org divergence terminal-fails, not retries.
    job = queue.get(job_id)
    assert job.status == "failed", job.status
    assert "org" in (job.error or "").lower(), job.error
    assert job.attempts == 1, job.attempts  # no retry — it will not self-heal
    queue.close()


def test_agreeing_payload_org_resolves_trusted_org_and_runs(tmp_path: Path) -> None:
    ws = tmp_path / "data"
    sheet = _make_regex_project(ws / str(TRUSTED_ORG), slug="queued")

    queue = SqliteJobQueue(tmp_path / "run.queue.db", hosted=True)
    port = _RecordingCredentialPort()
    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=None, cache_mode="off"),
        control_database_url=f"sqlite:///{tmp_path / 'control.db'}",
        require_storage_identity=True,
        worker_ports=WorkerPorts(credential_port=port),
    )

    run_id, job_id = _enqueue_canonical_regex_run(
        ws / str(TRUSTED_ORG),
        queue,
        project_id="queued",
        sheet_id=sheet,
        queue_payload_extra={
            "org_id": TRUSTED_ORG,
            "storage_org_id": TRUSTED_ORG,
        },
    )

    assert Worker(queue, registry, worker_id="h2-normal").run_once()

    job = queue.get(job_id)
    assert job.status == "done", job.error
    assert port.requested_org_ids == [TRUSTED_ORG], port.requested_org_ids
    p = Project(ws / str(TRUSTED_ORG) / "queued.frisket")
    run = p.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "completed", dict(run)
    p.close()
    queue.close()


def test_local_tier_without_org_is_unaffected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    sheet = _make_regex_project(ws, slug="local")

    queue = SqliteJobQueue(tmp_path / ".queue.db")  # local posture, no org
    port = _RecordingCredentialPort()
    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=None, cache_mode="off"),
        worker_ports=WorkerPorts(credential_port=port),
    )

    run_id, job_id = _enqueue_canonical_regex_run(
        ws,
        queue,
        project_id="local",
        sheet_id=sheet,
    )

    assert Worker(queue, registry, worker_id="h2-local").run_once()

    job = queue.get(job_id)
    assert job.status == "done", job.error
    assert port.requested_org_ids == [], port.requested_org_ids  # no BYOK resolution
    p = Project(ws / "local.frisket")
    run = p.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "completed", dict(run)
    p.close()
    queue.close()


def test_queued_cache_factory_uses_trusted_context_and_owns_cache(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "data"
    sheet = _make_regex_project(ws / str(TRUSTED_ORG), slug="queued")
    queue = SqliteJobQueue(tmp_path / "run.queue.db", hosted=True)
    _run_id, job_id = _enqueue_canonical_regex_run(
        ws / str(TRUSTED_ORG),
        queue,
        project_id="queued",
        sheet_id=sheet,
        queue_payload_extra={"org_id": TRUSTED_ORG, "storage_org_id": TRUSTED_ORG},
    )
    cache = _TrackingResponseCache(tmp_path / "tenant.cache.db", fail_close=True)
    contexts: list[JobHandlerContext] = []
    routed_caches: list[ResponseCache | None] = []

    def cache_for(context: JobHandlerContext) -> ResponseCache:
        contexts.append(context)
        return cache

    def compose(project, router, context):
        routed_caches.append(router.cache)
        return open_execution_composition(project, router, context)

    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=None, cache_mode="off"),
        require_storage_identity=True,
        worker_ports=WorkerPorts(credential_port=_RecordingCredentialPort()),
        response_cache_factory=cache_for,
        execution_composition_factory=compose,
    )

    assert Worker(queue, registry, worker_id="cache-owner").run_once()
    assert queue.get(job_id).status == "done"
    assert contexts == [JobHandlerContext.from_claimed_job(trusted_org_id=TRUSTED_ORG)]
    assert routed_caches == [cache]
    assert cache.close_calls == 1
    queue.close()


def test_queued_cache_factory_closes_cache_when_composition_fails(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "data"
    sheet = _make_regex_project(ws / str(TRUSTED_ORG), slug="queued")
    queue = SqliteJobQueue(tmp_path / "run.queue.db", hosted=True)
    _run_id, job_id = _enqueue_canonical_regex_run(
        ws / str(TRUSTED_ORG),
        queue,
        project_id="queued",
        sheet_id=sheet,
        queue_payload_extra={"org_id": TRUSTED_ORG, "storage_org_id": TRUSTED_ORG},
    )
    cache = _TrackingResponseCache(tmp_path / "tenant.cache.db")

    def fail_composition(*_args):
        raise RuntimeError("composition failed")

    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=None, cache_mode="off"),
        require_storage_identity=True,
        worker_ports=WorkerPorts(credential_port=_RecordingCredentialPort()),
        response_cache_factory=lambda _context: cache,
        execution_composition_factory=fail_composition,
    )

    assert Worker(queue, registry, worker_id="cache-failure").run_once()
    assert queue.get(job_id).status != "done"
    assert cache.close_calls == 1
    queue.close()


def test_queued_cache_factory_none_explicitly_disables_template_cache(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "data"
    sheet = _make_regex_project(ws / str(TRUSTED_ORG), slug="queued")
    queue = SqliteJobQueue(tmp_path / "run.queue.db", hosted=True)
    _run_id, job_id = _enqueue_canonical_regex_run(
        ws / str(TRUSTED_ORG),
        queue,
        project_id="queued",
        sheet_id=sheet,
        queue_payload_extra={"org_id": TRUSTED_ORG, "storage_org_id": TRUSTED_ORG},
    )
    template_cache = ResponseCache(tmp_path / "template.cache.db")
    routed_caches: list[ResponseCache | None] = []

    def compose(project, router, context):
        routed_caches.append(router.cache)
        return open_execution_composition(project, router, context)

    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=template_cache, cache_mode="replay"),
        require_storage_identity=True,
        worker_ports=WorkerPorts(credential_port=_RecordingCredentialPort()),
        response_cache_factory=lambda _context: None,
        execution_composition_factory=compose,
    )

    assert Worker(queue, registry, worker_id="cacheless").run_once()
    assert queue.get(job_id).status == "done"
    assert routed_caches == [None]
    assert template_cache.count() == 0
    template_cache.close()
    queue.close()


def test_missing_handler_registry_import_smoke() -> None:
    # Guards the import surface the red test relies on stays public.
    assert HandlerRegistry is not None


# ---------------------------------------------------------------------------
# A regression (low finding): the malformed/missing identity matrix.
# _normalize_queue_org_id is the fail-closed coercion the reconciliation runs
# on BOTH the trusted column and the payload copy before comparing them.
# ---------------------------------------------------------------------------


class TestNormalizeQueueOrgIdMatrix:
    def test_absent_forms_are_the_local_tier(self) -> None:
        from frisket.engine.jobs.worker import _normalize_queue_org_id

        # Only None and the empty string are "absent" (local tier); a
        # whitespace-only value fail-closes as malformed (below), safer than a
        # silent downgrade to unkeyed.
        for absent in (None, ""):
            assert _normalize_queue_org_id(absent) is None

    def test_valid_positive_int_and_numeric_string(self) -> None:
        from frisket.engine.jobs.worker import _normalize_queue_org_id

        # the queue column is declared String, so a numeric string is the
        # normal stored shape and must normalize to the int identity.
        assert _normalize_queue_org_id(7001) == 7001
        assert _normalize_queue_org_id("7001") == 7001

    @pytest.mark.parametrize(
        "bad",
        [
            "   ",  # whitespace-only: malformed, not absent
            "notanumber",
            "7001x",
            0,
            "0",
            -1,
            "-5",
            1.5,
            True,  # bool is not a valid org identity
            object(),
            [7001],
        ],
    )
    def test_malformed_identities_fail_closed(self, bad: object) -> None:
        from frisket.engine.jobs.worker import _normalize_queue_org_id

        # a malformed identity must RAISE, never normalize into some tenant's
        # keys — the reconciliation turns the raise into a terminal job failure.
        with pytest.raises((TypeError, ValueError)):
            _normalize_queue_org_id(bad)


def _drop_payload_org(queue: SqliteJobQueue, job_id: int) -> None:
    """Tamper: remove org_id from the durable payload, leaving the trusted
    column intact — the missing-side of a payload/column divergence."""
    with queue.engine.begin() as cx:
        raw = cx.execute(
            sa.text("SELECT payload FROM jobs WHERE id=:id"), {"id": job_id}
        ).scalar_one()
        body = json.loads(raw)
        body.pop("org_id", None)
        cx.execute(
            sa.text("UPDATE jobs SET payload=:p WHERE id=:id"),
            {"p": json.dumps(body), "id": job_id},
        )


def test_column_present_payload_missing_org_fails_closed(tmp_path: Path) -> None:
    """Missing-side divergence (A regression): a job whose trusted column
    names an org but whose (tampered) payload dropped org_id must fail closed —
    a well-formed hosted job carries both; a one-sided identity is a mismatch,
    not a silent downgrade to local/unkeyed or to the column's org."""
    ws = tmp_path / "data"
    sheet = _make_regex_project(ws / str(TRUSTED_ORG), slug="queued")

    queue = SqliteJobQueue(tmp_path / "run.queue.db", hosted=True)
    port = _RecordingCredentialPort()
    registry = default_registry()
    register_project_run_handler(
        registry,
        workspace_root=ws,
        router=ModelRouter(cache=None, cache_mode="off"),
        control_database_url=f"sqlite:///{tmp_path / 'control.db'}",
        require_storage_identity=True,
        worker_ports=WorkerPorts(credential_port=port),
    )
    _run_id, job_id = _enqueue_canonical_regex_run(
        ws / str(TRUSTED_ORG),
        queue,
        project_id="queued",
        sheet_id=sheet,
        queue_payload_extra={
            "org_id": TRUSTED_ORG,
            "storage_org_id": TRUSTED_ORG,
        },
    )
    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update().where(jobs_table.c.id == job_id).values(max_attempts=3)
        )
    _drop_payload_org(queue, job_id)

    Worker(queue, registry, worker_id="h2-missing").run_once()

    assert port.requested_org_ids == [], port.requested_org_ids
    job = queue.get(job_id)
    assert job.status == "failed", job.status
    assert job.attempts == 1, job.attempts  # terminal, no retry
    queue.close()
