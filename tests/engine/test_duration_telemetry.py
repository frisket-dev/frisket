from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import RUN_PROJECT_KIND, Worker
from frisket.server.app import create_app
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)

CSV = "first,last\nAda,Lovelace\nGrace,Hopper\n"


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _seed_queued_action_run(client: TestClient) -> tuple[str, int, int, int]:
    pid = client.post("/api/projects", json={"name": "Timing"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    spec = queued_python_run_spec(sheet_id, "first", "display")
    run = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    assert run.status_code == 200, run.text
    body = run.json()
    run_id = body["run_id"]
    job_id = body["job_id"]
    assert job_id is not None
    return pid, sheet_id, run_id, job_id


def _set_run_clock(
    client: TestClient,
    pid: str,
    run_id: int,
    *,
    started_at: datetime,
    finished_at: datetime | None = None,
    status: str = "running",
    completed_rows: int = 0,
    failed_rows: int = 0,
    total_rows: int = 2,
) -> None:
    project = client.app.state.workspace.get(pid)
    project.db.execute(
        "UPDATE runs SET started_at=?, finished_at=?, status=?, total_rows=?, "
        "completed_rows=?, failed_rows=? WHERE id=?",
        (
            _iso(started_at),
            _iso(finished_at) if finished_at is not None else None,
            status,
            total_rows,
            completed_rows,
            failed_rows,
            run_id,
        ),
    )
    project.db.commit()


def _set_job_clock(
    client: TestClient,
    job_id: int,
    *,
    created_at: datetime,
    started_at: datetime | None,
    finished_at: datetime | None = None,
    status: str | None = None,
) -> None:
    from frisket.engine.jobs.queue import jobs_table

    queue = client.app.state.workspace.queue
    values: dict[str, object] = {
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    # COALESCE semantics: only overwrite these when a value is supplied.
    if started_at is not None:
        values["locked_at"] = started_at
    if started_at is not None and finished_at is None:
        values["lease_expires_at"] = datetime.now(UTC) + timedelta(minutes=10)
    if status is not None:
        values["status"] = status
    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update().where(jobs_table.c.id == job_id).values(**values)
        )


def _action_run_public_status(client: TestClient, pid: str, run_id: int) -> dict:
    response = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()["run"]["public_status"]


def test_run_status_exposes_elapsed_throughput_eta_and_queue_wait(tmp_path):
    client = _client(tmp_path)
    pid, _, run_id, job_id = _seed_queued_action_run(client)

    job = client.app.state.workspace.queue.claim("timing-worker", lease_seconds=600)
    assert job is not None and job.id == job_id

    base = datetime.now(UTC)
    _set_run_clock(
        client,
        pid,
        run_id,
        started_at=base - timedelta(seconds=120),
        total_rows=4,
        completed_rows=1,
    )
    _set_job_clock(
        client,
        job_id,
        created_at=base - timedelta(seconds=240),
        started_at=base - timedelta(seconds=180),
    )

    body = _action_run_public_status(client, pid, run_id)

    assert body["status"] == "running"
    timing = body["timing"]
    assert timing["processed_rows"] == 1
    assert timing["remaining_rows"] == 3
    assert timing["elapsed_seconds"] >= 115
    assert timing["processed_rows_per_second"] == pytest.approx(1 / 120, rel=0.15)
    assert timing["eta_seconds"] >= 300
    assert timing["estimated_finish_at"]
    assert timing["queue_wait_seconds"] == pytest.approx(60, abs=3)
    assert timing["job_elapsed_seconds"] >= 175


def test_project_timing_summarizes_run_and_job_history(tmp_path):
    client = _client(tmp_path)
    pid, _, run_id, job_id = _seed_queued_action_run(client)
    worker = Worker(
        client.app.state.workspace.queue,
        client.app.state.workspace.registry,
        worker_id="timing-worker",
    )
    assert worker.run_once()

    base = datetime.now(UTC)
    _set_run_clock(
        client,
        pid,
        run_id,
        started_at=base - timedelta(seconds=20),
        finished_at=base - timedelta(seconds=10),
        status="completed",
        total_rows=2,
        completed_rows=2,
    )
    _set_job_clock(
        client,
        job_id,
        created_at=base - timedelta(seconds=25),
        started_at=base - timedelta(seconds=22),
        finished_at=base - timedelta(seconds=12),
        status="done",
    )

    body = client.get(f"/api/projects/{pid}/timing").json()

    assert body["runs"]["count"] == 1
    assert body["runs"]["duration_seconds"]["p50_seconds"] == pytest.approx(10, abs=0.5)
    assert body["runs"]["rows_per_second"]["p50_seconds"] == pytest.approx(
        0.2, abs=0.02
    )
    assert "by_recipe" not in body["runs"]
    action = body["runs"]["by_action"]["map.python"]
    assert action["action_name"] == "Run trusted local Python"
    assert action["count"] == 1
    assert action["duration_seconds"]["p90_seconds"] == pytest.approx(10, abs=0.5)

    assert body["jobs"]["count"] == 1
    assert body["jobs"]["queue_wait_seconds"]["p50_seconds"] == pytest.approx(
        3, abs=0.5
    )
    assert body["jobs"]["duration_seconds"]["p50_seconds"] == pytest.approx(10, abs=0.5)
    kind = body["jobs"]["by_kind"][RUN_PROJECT_KIND]
    assert kind["count"] == 1
    assert kind["duration_seconds"]["p90_seconds"] == pytest.approx(10, abs=0.5)
    assert body["active_runs"] == []
