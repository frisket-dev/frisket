from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)


ROOT = Path(__file__).resolve().parents[2]
CSV = "first,last\nAda,Lovelace\nGrace,Hopper\n"


def _seed_queued_action_run(client: TestClient) -> tuple[str, int, int]:
    project_id = client.post("/api/projects", json={"name": "Action cancel"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("people.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    spec = queued_python_run_spec(sheet_id, "first", "display")
    run = post_canonical_run_spec_as_v1_action(client, project_id, spec, confirmed=True)
    assert run.status_code == 200, run.text
    body = run.json()
    run_id = body["run_id"]
    job_id = body["job_id"]
    assert job_id is not None
    return project_id, run_id, job_id


def test_action_run_cancel_route_cancels_run_and_queue_job(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, run_id, job_id = _seed_queued_action_run(client)

    cancelled = client.post(f"/api/projects/{project_id}/actions/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["queue_job_id"] == job_id
    assert cancelled.json()["queue_cancelled"] is True

    job = client.app.state.workspace.queue.get(job_id)
    assert job is not None and job.status == "cancelled"

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/status"
    ).json()
    assert status["run"]["public_status"]["status"] == "cancelled"
