"""Queue health: worker heartbeat, queued-job staleness surfacing, a loud
queue timeout, and live admin job remediation (onboard-queue-health-v1).

Layers exercised here:

- Heartbeat + liveness primitives on BOTH JobQueue backends (SqliteJobQueue and
  PostgresJobQueue-on-SQLite, mirroring tests/test_job_queue.py).
- The queue-health projection helper both tiers publish.
- The worker loop recording its own liveness.
- run-status reconciliation surfacing "queued with no live worker" and a
  configurable timeout that terminalizes a wedged queued job loudly.
- The hosted admin Jobs retry/cancel/recover service + routes, with admin authz.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import (
    PostgresJobQueue,
    SqliteJobQueue,
    Worker,
    default_registry,
)
from frisket.engine.jobs.queue_health import (
    evaluate_run_queue_deploy_readiness,
    queue_health_payload,
)
from frisket.server.app import create_app
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)

PG_URL = os.environ.get("FRISKET_PG_TEST_URL")
CSV = "first,last\nAda,Lovelace\nGrace,Hopper\n"


@pytest.fixture(params=["sqlite", "pg-on-sqlite"])
def queue(request, tmp_path):
    if request.param == "sqlite":
        q = SqliteJobQueue(tmp_path / "q.db")
    else:
        q = PostgresJobQueue(f"sqlite:///{tmp_path}/q-pg.db")
    yield q
    q.close()


# --------------------------------------------------------------------------
# Heartbeat + liveness primitives (both backends)
# --------------------------------------------------------------------------


class TestWorkerHeartbeat:
    def test_record_and_count_live_workers(self, queue):
        now = datetime.now(UTC)
        queue.record_worker_heartbeat("w1", now=now)
        assert queue.count_live_workers(within_seconds=60, now=now) == 1

    def test_stale_heartbeat_is_not_live(self, queue):
        now = datetime.now(UTC)
        queue.record_worker_heartbeat("w1", now=now - timedelta(seconds=300))
        assert queue.count_live_workers(within_seconds=60, now=now) == 0

    def test_heartbeat_upserts_per_worker(self, queue):
        now = datetime.now(UTC)
        queue.record_worker_heartbeat("w1", now=now - timedelta(seconds=10))
        queue.record_worker_heartbeat("w1", now=now)
        queue.record_worker_heartbeat("w2", now=now)
        assert queue.count_live_workers(within_seconds=60, now=now) == 2
        beats = {hb.worker_id: hb for hb in queue.list_worker_heartbeats()}
        assert set(beats) == {"w1", "w2"}
        assert beats["w1"].last_heartbeat_at is not None

    def test_heartbeat_records_kinds_and_keeps_them_on_a_kindless_upsert(self, queue):
        """A worker advertises its registered handler kinds on every
        heartbeat; a LATER
        heartbeat that omits ``kinds`` (None, matching the existing ``queue``/
        ``worker_version`` "unknown, don't clobber" convention) must not
        erase the previously-recorded value."""
        now = datetime.now(UTC)
        queue.record_worker_heartbeat("w1", now=now, kinds="echo,model.pull")
        beats = {hb.worker_id: hb for hb in queue.list_worker_heartbeats()}
        assert beats["w1"].kinds == "echo,model.pull"

        queue.record_worker_heartbeat("w1", now=now + timedelta(seconds=1))
        beats = {hb.worker_id: hb for hb in queue.list_worker_heartbeats()}
        assert beats["w1"].kinds == "echo,model.pull"


class TestFailQueued:
    def test_fail_queued_terminalizes_loudly(self, queue):
        jid = queue.enqueue("echo", {}, max_attempts=1)
        assert queue.fail_queued(jid, "queue timeout: no worker") is True
        job = queue.get(jid)
        assert job.status == "failed"
        assert "queue timeout" in (job.error or "")
        assert job.finished_at is not None

    def test_fail_queued_ignores_running_job(self, queue):
        jid = queue.enqueue("echo", {})
        queue.claim("w1", lease_seconds=60)
        assert queue.fail_queued(jid, "nope") is False
        assert queue.get(jid).status == "running"


def _force_lease_expired(queue, job_id, when: datetime) -> None:
    from frisket.engine.jobs.queue import jobs_table

    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(lease_expires_at=when)
        )


# --------------------------------------------------------------------------
# Queue-health projection helper
# --------------------------------------------------------------------------


class TestQueueHealthPayload:
    def test_empty_queue_has_no_oldest_age(self, queue):
        now = datetime.now(UTC)
        payload = queue_health_payload(
            queue,
            now=now,
            liveness_window_seconds=60,
            timeout_seconds=120,
        )

        assert queue.oldest_queued_created_at() is None
        assert payload["queued"]["oldest_age_seconds"] is None
        assert payload["queued"]["stale"] is False
        assert payload["queued"]["timed_out"] is False

    def test_no_live_worker_when_queued_without_heartbeat(self, queue):
        now = datetime.now(UTC)
        queue.enqueue("echo", {})
        queue.enqueue("echo", {})
        payload = queue_health_payload(queue, now=now, liveness_window_seconds=60)
        assert payload["queued"]["count"] == 2
        assert payload["queued"]["no_live_worker"] is True
        assert payload["workers"]["live"] == 0

    def test_live_worker_clears_no_worker_flag(self, queue):
        now = datetime.now(UTC)
        queue.enqueue("echo", {})
        queue.record_worker_heartbeat("w1", now=now)
        payload = queue_health_payload(queue, now=now, liveness_window_seconds=60)
        assert payload["workers"]["live"] == 1
        assert payload["queued"]["no_live_worker"] is False

    def test_model_pull_workers_counts_only_live_workers_advertising_it(self, queue):
        """Alongside the existing enablement boolean, the health payload
        reports how many
        LIVE workers actually advertise ``model.pull`` -- an enabled route
        sitting in front of a capability-less worker fleet must be visible
        as a fact, not an inexplicably stuck job."""
        now = datetime.now(UTC)
        queue.record_worker_heartbeat("w1", now=now, kinds="echo,model.pull")
        queue.record_worker_heartbeat("w2", now=now, kinds="echo")
        # stale -- outside the liveness window, must not count even though
        # it once advertised model.pull.
        queue.record_worker_heartbeat(
            "w3", now=now - timedelta(seconds=300), kinds="echo,model.pull"
        )
        payload = queue_health_payload(
            queue, now=now, liveness_window_seconds=60, model_pull_enabled=True
        )
        assert payload["model_pull_enabled"] is True
        assert payload["model_pull_workers"] == 1

    def test_model_pull_workers_is_zero_when_no_worker_advertises_it(self, queue):
        now = datetime.now(UTC)
        queue.record_worker_heartbeat("w1", now=now)  # predates the kinds column
        payload = queue_health_payload(queue, now=now, liveness_window_seconds=60)
        assert payload["model_pull_workers"] == 0

    def test_oldest_age_scans_all_201_queued_rows(self, queue):
        """The oldest row must not disappear behind the former newest-200 cap."""
        from frisket.engine.jobs.queue import jobs_table

        now = datetime.now(UTC).replace(microsecond=0)
        ids = [queue.enqueue("echo", {}) for _ in range(201)]
        oldest = now - timedelta(days=2)
        fresh = now - timedelta(seconds=3)
        with queue.engine.begin() as cx:
            cx.execute(
                jobs_table.update()
                .where(jobs_table.c.id == ids[0])
                .values(created_at=oldest, available_at=oldest)
            )
            cx.execute(
                jobs_table.update()
                .where(jobs_table.c.id.in_(ids[1:]))
                .values(created_at=fresh, available_at=fresh)
            )

        payload = queue_health_payload(
            queue,
            now=now,
            liveness_window_seconds=60,
            timeout_seconds=3600,
        )

        assert payload["queued"] == {
            "count": 201,
            "oldest_age_seconds": 172800.0,
            "no_live_worker": True,
            "stale": True,
            "timed_out": True,
            "timeout_seconds": 3600,
        }


class TestDeployReadiness:
    def test_fresh_target_heartbeat_is_not_blocked_by_an_old_live_claim(self, queue):
        """Cutover waits for the old worker to stop, not for its live lease.

        The new worker's post-cutover heartbeat proves the target service
        started. If a pre-cutover job needs retrying, ordinary lease recovery
        handles it after the old process is gone.
        """
        from frisket.engine.jobs.queue import jobs_table

        target = "target-version"
        now = datetime.now(UTC).replace(microsecond=0)
        ids = [queue.enqueue("echo", {}) for _ in range(2)]
        with queue.engine.begin() as cx:
            cx.execute(
                jobs_table.update()
                .where(jobs_table.c.id.in_(ids))
                .values(status="running", claimed_code_version=target)
            )
            cx.execute(
                jobs_table.update()
                .where(jobs_table.c.id == ids[0])
                .values(claimed_code_version="old-version")
            )
        queue.record_worker_heartbeat("target-worker", worker_version=target, now=now)

        report = evaluate_run_queue_deploy_readiness(
            queue=queue,
            app_run_queue_locator="same-locator",
            worker_run_queue_locator="same-locator",
            observed_schema_version=1,
            target_schema_version=1,
            target_code_version=target,
            cutover_started_at=now - timedelta(seconds=1),
            now=now,
        )

        assert report.ok is True
        assert report.failures == ()


# --------------------------------------------------------------------------
# Worker loop records its own liveness
# --------------------------------------------------------------------------


def test_worker_run_once_records_liveness(tmp_path):
    queue = SqliteJobQueue(tmp_path / "q.db")
    try:
        worker = Worker(queue, default_registry(), lease_seconds=60)
        # even an idle poll records liveness so "no worker" is distinguishable
        # from "worker busy".
        assert worker.run_once() is False
        assert queue.count_live_workers(within_seconds=300) >= 1
        assert any(
            hb.worker_id == worker.worker_id for hb in queue.list_worker_heartbeats()
        )
    finally:
        queue.close()


def test_worker_heartbeat_advertises_its_registered_kinds(tmp_path):
    """The worker writes a comma-joined, SORTED list of its registered handler
    kinds on every
    heartbeat -- ``HandlerRegistry.kinds()`` already sorts, so this is a
    straight pass-through, pinned end to end through the real ``Worker``."""
    from frisket.engine.jobs import HandlerRegistry

    queue = SqliteJobQueue(tmp_path / "q.db")
    try:
        registry = HandlerRegistry()
        registry.register("model.pull", lambda payload, _context: {})
        registry.register("echo", lambda payload, _context: dict(payload))
        worker = Worker(queue, registry, lease_seconds=60)
        worker.record_liveness(force=True)
        hb = next(
            h for h in queue.list_worker_heartbeats() if h.worker_id == worker.worker_id
        )
        assert hb.kinds == "echo,model.pull"
    finally:
        queue.close()


# --------------------------------------------------------------------------
# run-status reconciliation: no-live-worker staleness + loud timeout
# --------------------------------------------------------------------------


def _client(tmp_path, **kwargs) -> TestClient:
    kwargs.setdefault("run_status_grace_seconds", 3600.0)
    return TestClient(create_app(tmp_path / "ws", **kwargs))


def _seed_queued_run(client: TestClient) -> tuple[str, int, int]:
    pid = client.post("/api/projects", json={"name": "Queue Health"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", CSV, "text/csv")},
    )
    sheet_id = imported.json()["sheet_id"]
    spec = queued_python_run_spec(sheet_id, "first", "display")
    run = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    body = run.json()
    return pid, body["run_id"], body["job_id"]


def _age_job(client: TestClient, job_id: int) -> None:
    from frisket.engine.jobs.queue import jobs_table

    queue = client.app.state.workspace.queue
    stale = datetime.now(UTC) - timedelta(minutes=10)
    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(created_at=stale, available_at=stale)
        )


def _status(client: TestClient, pid: str, run_id: int) -> dict:
    resp = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert resp.status_code == 200, resp.text
    return resp.json()["run"]["public_status"]


def test_queued_without_live_worker_surfaces_no_live_worker(tmp_path):
    client = _client(tmp_path, worker_liveness_window_seconds=1)
    pid, run_id, job_id = _seed_queued_run(client)
    _age_job(client, job_id)

    status = _status(client, pid, run_id)
    assert status["status"] == "stalled"
    assert status["stalled_reason"] == "no_live_worker"
    assert status["queue"]["no_live_worker"] is True


def test_live_worker_keeps_queued_run_queued(tmp_path):
    client = _client(tmp_path, worker_liveness_window_seconds=60)
    pid, run_id, job_id = _seed_queued_run(client)
    _age_job(client, job_id)
    client.app.state.workspace.queue.record_worker_heartbeat("busy-worker")

    status = _status(client, pid, run_id)
    assert status["status"] == "queued"
    assert status["queue"]["no_live_worker"] is False


def test_queue_timeout_fails_wedged_job_loudly(tmp_path):
    client = _client(
        tmp_path, worker_liveness_window_seconds=1, queue_timeout_seconds=1
    )
    pid, run_id, job_id = _seed_queued_run(client)
    _age_job(client, job_id)

    status = _status(client, pid, run_id)
    assert status["status"] == "failed"
    assert "timeout" in (status.get("error") or "").lower()

    job = client.app.state.workspace.queue.get(job_id)
    assert job.status == "failed"


def test_fresh_queued_job_is_not_prematurely_stalled(tmp_path):
    client = _client(tmp_path, worker_liveness_window_seconds=60)
    pid, run_id, job_id = _seed_queued_run(client)

    status = _status(client, pid, run_id)
    assert status["status"] == "queued"


# --------------------------------------------------------------------------
# Local health endpoint exposes queue health
# --------------------------------------------------------------------------


def test_local_health_endpoint_exposes_queue_health(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "queue" in body
    assert "queued" in body["queue"]
    assert "workers" in body["queue"]


# --------------------------------------------------------------------------
# Hosted admin Jobs remediation service + routes (with authz)
# --------------------------------------------------------------------------


class _FakeQueue:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self._job = _FakeJob()

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return True

    def get(self, job_id):
        return self._job

    def list_jobs(self, limit=200):
        return [self._job]

    def counts(self):
        return {"queued": 1, "running": 0, "failed": 0, "done": 0, "cancelled": 0}

    def count_live_workers(self, *, within_seconds, now=None):
        return 0

    def list_worker_heartbeats(self):
        return []


class _FakeJob:
    id = 7
    kind = "project.run"
    payload: dict = {}
    status = "failed"
    attempts = 1
    max_attempts = 1
    locked_by = None
    locked_at = None
    lease_expires_at = None
    available_at = datetime.now(UTC)
    created_at = datetime.now(UTC)
    started_at = None
    finished_at = None
    result: dict | None = None
    error = "boom"
    org_id = None
    project_id = "p1"
    run_id = 1
    source_id = None
    sheet_id = None
    trace_id = None
    action_kind = "map.template"
