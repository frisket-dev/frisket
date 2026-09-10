from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

import frisket.engine.jobs.worker as worker_module
from frisket.engine.jobs import queue as queue_module
from frisket.engine.jobs.queue import SqliteJobQueue
from frisket.engine.jobs.worker import ECHO_KIND, HandlerRegistry, Worker


@pytest.fixture
def queue(tmp_path):
    q = SqliteJobQueue(tmp_path / "q.db")
    yield q
    q.close()


def test_cross_release_claim_can_complete_the_enqueued_job(queue, monkeypatch):
    """Version stamps explain who did the work; they do not fence completion."""
    monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
    job_id = queue.enqueue("record", {"value": 7})

    monkeypatch.setattr(queue_module, "code_version", lambda: "commit-B")
    claimed = queue.claim("worker-B")
    assert claimed is not None
    assert claimed.code_version == "commit-A"
    assert claimed.claimed_code_version == "commit-B"

    assert queue.complete(job_id, "worker-B", {"completed_by": "B"}) is True
    finished = queue.get(job_id)
    assert finished.status == "done"
    assert finished.result == {"completed_by": "B"}
    assert finished.code_version == "commit-A"
    assert finished.claimed_code_version == "commit-B"


def test_starting_b_does_not_invalidate_a_live_lease(queue, monkeypatch):
    """A new worker cannot reclaim a live old-worker lease just by starting."""
    monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
    job_id = queue.enqueue(ECHO_KIND, {"value": 7})
    claimed = queue.claim("worker-A", lease_seconds=600)
    assert claimed is not None
    assert claimed.claimed_code_version == "commit-A"

    monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
    stop = threading.Event()
    stop.set()
    Worker(queue, worker_id="worker-B").run_forever(stop=stop)

    still_live = queue.get(job_id)
    assert still_live.status == "running"
    assert still_live.locked_by == "worker-A"
    assert still_live.claimed_code_version == "commit-A"


def test_expired_a_lease_recovers_and_finishes_under_b(tmp_path, monkeypatch):
    """A stopped worker recovers through the ordinary lease-expiry path."""
    now = [datetime(2026, 8, 28, tzinfo=UTC)]
    queue = SqliteJobQueue(tmp_path / "q.db", clock=lambda: now[0])
    try:
        monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
        job_id = queue.enqueue(ECHO_KIND, {"value": 7}, max_attempts=3)
        claimed = queue.claim("worker-A", lease_seconds=1)
        assert claimed is not None

        now[0] += timedelta(seconds=2)
        assert queue.recover_expired() == 1
        assert queue.get(job_id).status == "queued"

        monkeypatch.setattr(queue_module, "code_version", lambda: "commit-B")
        monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
        assert Worker(queue, worker_id="worker-B").run_once() is True

        finished = queue.get(job_id)
        assert finished.status == "done"
        assert finished.attempts == 2
        assert finished.code_version == "commit-A"
        assert finished.claimed_code_version == "commit-B"
    finally:
        queue.close()


def test_worker_heartbeat_keeps_its_release_identity(queue, monkeypatch):
    monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
    Worker(queue, worker_id="worker-B").record_liveness(force=True)
    heartbeat = {item.worker_id: item for item in queue.list_worker_heartbeats()}[
        "worker-B"
    ]
    assert heartbeat.worker_version == "commit-B"


def test_cross_release_handler_runs_once_and_completes(queue, monkeypatch):
    monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
    job_id = queue.enqueue("record", {"value": 7})
    monkeypatch.setattr(queue_module, "code_version", lambda: "commit-B")
    monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
    calls: list[dict] = []
    registry = HandlerRegistry()

    def record(payload, _context):
        calls.append(payload)
        return {"calls": len(calls)}

    registry.register("record", record)
    assert Worker(queue, registry, worker_id="worker-B").run_once() is True
    assert calls == [{"value": 7, "job_id": job_id, "job_final_attempt": False}]
    finished = queue.get(job_id)
    assert finished.status == "done"
    assert finished.code_version == "commit-A"
    assert finished.claimed_code_version == "commit-B"
