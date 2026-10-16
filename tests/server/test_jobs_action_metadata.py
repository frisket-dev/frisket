from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.actions.system import root_action_catalog
from frisket.server.app import create_app
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)

CSV = "text\nCall 212-555-0123\nNo phone\n"


def _catalog_title(kind: str) -> str:
    for action in root_action_catalog().actions:
        if action.kind == kind:
            return action.title
    raise AssertionError(f"missing catalog action: {kind}")


def test_job_backed_run_status_and_timing_expose_action_metadata(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    pid = client.post("/api/projects", json={"name": "Job Actions"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    spec = queued_python_run_spec(sheet_id, "text", "phone")

    run = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]
    action_title = _catalog_title("map.python")

    # Local TestClient apps do not spawn the worker; the job remains queued
    # until a test explicitly drains it with Worker.
    response = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    status = response.json()["run"]["public_status"]
    job_id = status["queue"]["job_id"]
    assert status["status"] == "queued"
    assert status["action_kind"] == "map.python"
    assert status["action_name"] == action_title
    assert status["queue"]["job_id"] == job_id
    assert status["queue"]["action_kind"] == "map.python"
    assert status["queue"]["action_name"] == action_title

    timing = client.get(f"/api/projects/{pid}/timing").json()
    action_bucket = timing["runs"]["by_action"]["map.python"]
    assert action_bucket["count"] == 1
    assert action_bucket["action_name"] == action_title
    assert timing["active_runs"] == [
        {
            "run_id": run_id,
            "action_kind": "map.python",
            "action_name": action_title,
            "status": "queued",
            "timing": timing["active_runs"][0]["timing"],
        }
    ]


def test_search_preparation_jobs_have_a_cosmetic_origin_label(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    pid = client.post("/api/projects", json={"name": "Prepared jobs"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    spec = queued_python_run_spec(sheet_id, "text", "phone")

    normal = post_canonical_run_spec_as_v1_action(
        client,
        pid,
        spec,
        confirmed=True,
        idempotency_key="user-action@sha256:stable",
    )
    assert normal.status_code == 200, normal.text
    prepared = post_canonical_run_spec_as_v1_action(
        client,
        pid,
        {**spec, "output_name": "prepared_phone"},
        confirmed=True,
        idempotency_key="search-preparation:batch-1:ocr:stable",
    )
    assert prepared.status_code == 200, prepared.text

    response = client.get(f"/api/projects/{pid}/actions/jobs")
    assert response.status_code == 200, response.text
    names_by_run = {
        job["run_id"]: job["action_name"] for job in response.json()["jobs"]
    }
    title = _catalog_title("map.python")
    assert names_by_run[normal.json()["run_id"]] == title
    assert names_by_run[prepared.json()["run_id"]] == f"{title} (search preparation)"
