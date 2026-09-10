"""Queue-next: submitting a run while one is in flight enqueues it instead of
being refused, and the single worker executes it once the project frees.

The owner's scenario is an `extract` run in flight when they submit a second
`web research` run. Both kinds are declared QUEUED_PROJECT_RUN placement, so the
server admits the second submission and enqueues it (status="queued"); the
single worker then drains the two project.run jobs serially — the single-writer
SQLite bundle is only ever touched by one run at a time.

`map.python` stands in for both kinds here because it is a real
QUEUED_PROJECT_RUN kind that needs no model router, so the whole submit ->
queued -> worker-executes cycle runs deterministically.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.server.app import create_app
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)

CSV = "first,last\nAda,Lovelace\nGrace,Hopper\n"


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws"))


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Queue Next"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return pid, imported.json()["sheet_id"]


def _submit_queued_run(
    client: TestClient, pid: str, sheet_id: int, output_name: str
) -> dict[str, Any]:
    spec = queued_python_run_spec(sheet_id, "first", output_name)
    response = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    assert response.status_code == 200, response.text
    return response.json()


def _public_status(client: TestClient, pid: str, run_id: int) -> dict[str, Any]:
    response = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()["run"]["public_status"]


def _active_runs(client: TestClient, pid: str) -> dict[int, str]:
    response = client.get(f"/api/projects/{pid}/timing")
    assert response.status_code == 200, response.text
    return {
        int(entry["run_id"]): entry["status"]
        for entry in response.json()["active_runs"]
    }


def _run_row_status(client: TestClient, pid: str, run_id: int) -> str:
    project = client.app.state.workspace.get(pid)
    row = project.db.execute(
        "SELECT status, completed_rows, total_rows FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row is not None
    assert row["completed_rows"] == row["total_rows"]
    return str(row["status"])


def _column_names(client: TestClient, pid: str, sheet_id: int) -> set[str]:
    project = client.app.state.workspace.get(pid)
    rows = project.db.execute(
        "SELECT name FROM columns WHERE sheet_id=?", (sheet_id,)
    ).fetchall()
    return {str(r["name"]) for r in rows}


def test_submit_while_run_in_flight_queues_and_executes_after(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    ws = client.app.state.workspace

    first = _submit_queued_run(client, pid, sheet_id, "display_a")
    run_a = first["run_id"]
    job_a = first["job_id"]
    assert first["status"] == "queued"
    assert job_a is not None

    worker = Worker(
        ws.queue, ws.registry, worker_id="queue-next-worker", poll_interval=0.01
    )
    captured: dict[str, Any] = {}

    def on_transition(event: str, job) -> None:
        # Fired at "claimed" — run A is now in flight (the worker holds its
        # lease) but has not executed yet. Submitting the second run here
        # reproduces the owner's "extract in flight, start web research" moment.
        if event != "claimed" or job.id != job_a or "second" in captured:
            return
        second = _submit_queued_run(client, pid, sheet_id, "display_b")
        captured["second"] = second
        # In-flight snapshot: A running, B queued (not refused, not waiting on
        # the client). Both surface in the project's active_runs.
        captured["a_status_while_b_submitted"] = _public_status(client, pid, run_a)
        captured["b_status_on_submit"] = _public_status(client, pid, second["run_id"])
        captured["active_runs"] = _active_runs(client, pid)

    worker.set_transition_hook(on_transition)

    # run_once claims + executes exactly one job: A. The hook submits B while A
    # is in flight; A then runs to completion, B stays queued behind it.
    assert worker.run_once() is True

    second = captured["second"]
    run_b = second["run_id"]
    assert second["status"] == "queued"
    assert second["job_id"] is not None
    assert second["job_id"] != job_a
    assert captured["a_status_while_b_submitted"]["status"] == "running"
    assert captured["b_status_on_submit"]["status"] == "queued"
    assert captured["active_runs"][run_a] == "running"
    assert captured["active_runs"][run_b] == "queued"

    # A finished; B waited its turn — serialized execution, single-writer safe.
    assert _run_row_status(client, pid, run_a) == "completed"
    assert _public_status(client, pid, run_a)["status"] == "completed"
    assert _public_status(client, pid, run_b)["status"] == "queued"

    # The project frees, so the worker now drains B.
    assert worker.run_once() is True
    assert _run_row_status(client, pid, run_b) == "completed"
    assert _public_status(client, pid, run_b)["status"] == "completed"

    # Both queued runs produced their output columns; nothing was dropped.
    assert {"display_a", "display_b"} <= _column_names(client, pid, sheet_id)

    # Queue is drained; no third job lingers.
    assert worker.run_once() is False
