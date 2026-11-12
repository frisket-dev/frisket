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

    listed = client.get(f"/api/projects/{pid}/actions/jobs").json()["jobs"]
    job = next(item for item in listed if item["job_id"] == job_id)
    context = job["context"]
    assert context["sheet_id"] == sheet_id
    assert context["sheet_name"] == "calls"
    assert [column["name"] for column in context["output_columns"]] == ["phone"]
    detail = client.get(f"/api/projects/{pid}/actions/jobs/{job_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["context"] == context

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


def test_runless_job_retains_explicit_sheet_context_through_failure(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Queued context"}).json()["id"]
    workspace = client.app.state.workspace
    project = workspace.get(pid)
    sheet_id = project.add_sheet("Original")
    project.add_sheet("Other sheet")
    queue = workspace.queue
    job_id = queue.enqueue(
        "action.run",
        {
            "project_id": pid,
            "action_job": {
                "action_kind": "map.template",
                "action": {
                    "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                    "params": {"private_prompt": "Do not expose this"},
                },
            },
        },
    )
    expected = {"sheet_id": sheet_id, "sheet_name": "Original", "output_columns": []}

    for phase in ("queued", "running", "failed"):
        response = client.get(f"/api/projects/{pid}/actions/jobs/{job_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == phase
        assert response.json()["context"] == expected
        assert "private_prompt" not in response.text
        listed = client.get(f"/api/projects/{pid}/actions/jobs").json()["jobs"]
        assert (
            next(job for job in listed if job["job_id"] == job_id)["context"]
            == expected
        )
        if phase == "queued":
            assert queue.claim("context-test").id == job_id
        elif phase == "running":
            assert queue.fail(
                job_id, "context-test", "provider unavailable", retry=False
            )

    project.db.execute("UPDATE sheets SET name='Renamed' WHERE id=?", (sheet_id,))
    project.db.commit()
    expected["sheet_name"] = "Renamed"
    assert (
        client.get(f"/api/projects/{pid}/actions/jobs/{job_id}").json()["context"]
        == expected
    )
    project.delete_sheet(sheet_id)
    expected["sheet_name"] = None
    assert (
        client.get(f"/api/projects/{pid}/actions/jobs/{job_id}").json()["context"]
        == expected
    )
