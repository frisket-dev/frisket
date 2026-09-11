"""Every run surfaces in the jobs listing, regardless of execution path.

Long-running media acquisition must be observable as a normal job.
``map.template`` is a real, deterministic, offline INLINE
-placement action (executor/action_specs.py's _QUEUED_PROJECT_RUN_KINDS does
not include it) — the same fixture history-panel/run-detail-full-params
specs already use for a $0, model-free run.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from action_test_helpers import typed_map_request
from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
CSV = "snippet\nfirst\nsecond\n"


def _seed_project(client: TestClient, name: str) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("stories.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _template_action(sheet_id: int) -> dict:
    return typed_map_request(
        "map.template",
        sheet_id,
        params={"template": {"text": "note: {{snippet}}"}},
        output_names={"rendered": "note"},
        idempotency_key="runs-are-jobs-parity@sha256:stable",
    )


def test_inline_run_surfaces_in_the_jobs_listing_with_progress_and_terminal_state(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client, "Parity project")

    # Before the run: nothing in the jobs listing for this project.
    before = client.get(f"/api/projects/{project_id}/actions/jobs")
    assert before.status_code == 200, before.text
    assert before.json()["jobs"] == []

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_template_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    run_id = result["run_id"]
    assert run_id is not None
    # media.ytdlp_download-style INLINE actions never mint a queue job id.
    assert result.get("job_id") is None

    listed = client.get(f"/api/projects/{project_id}/actions/jobs")
    assert listed.status_code == 200, listed.text
    jobs = listed.json()["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["schema_version"] == "frisket.job.v1"
    assert job["kind"] == "run.inline"
    assert job["run_id"] == run_id
    # negative sentinel — never collides with a real (positive) queue job id.
    assert job["job_id"] == -run_id
    assert job["payload_ref"] == {"kind": "run", "run_id": run_id}
    assert job["status"] == "done"
    assert job["action_kind"] == "map.template"
    # One jobs response carries the same renderable run progress that formerly
    # required a follow-up request for every run.
    assert job["progress"]["run_id"] == run_id
    assert job["progress"]["sheet_id"] == sheet_id
    assert job["progress"]["status"] == "completed"
    assert job["progress"]["total"] == 2
    assert job["progress"]["completed"] == 2
    assert job["progress"]["failed"] == 0
    assert job["result_summary"]["total"] == 2
    assert job["result_summary"]["completed"] == 2
    assert job["result_summary"]["failed"] == 0
    assert job["timing"]["started_at"]
    assert job["timing"]["finished_at"]

    detail = client.get(f"/api/projects/{project_id}/actions/jobs/{job['job_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json() == job

    # A run already covered by a real project.run queue job is never
    # double-projected; dedupe is by run_id.
    filtered = client.get(f"/api/projects/{project_id}/actions/jobs?status=queued")
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["jobs"] == []
