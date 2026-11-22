from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.engine.jobs import RUN_PROJECT_KIND
from frisket.engine.jobs.queue import PostgresJobQueue, SqliteJobQueue, jobs_table
from frisket.server.app import create_app
from tests.http_test_helpers import v1_action_from_canonical_run_spec


# The project-jobs listing endpoint moved out of the app.py monolith into the
# extracted action-run service (routes/action_runs.py -> services/action_runs.py);
# the indexed-query pin follows the referent to its new home.

CSV = "note\nCall 212-555-0123\nNo phone\n"


def _seed_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "Indexed jobs"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _queued_action(sheet_id: int) -> dict[str, Any]:
    return v1_action_from_canonical_run_spec(
        {
            "action_kind": "map.python",
            "sheet_id": sheet_id,
            "input_columns": ["note"],
            "code": "result = row['note']",
            "output_name": "copied_note",
        },
        idempotency_key="indexed-project-run-job@sha256:stable",
    )


def _insert_newer_unrelated_jobs(queue: SqliteJobQueue, *, count: int) -> None:
    now = "2026-06-19T12:00:00.000000+00:00"
    payloads = [
        (
            RUN_PROJECT_KIND,
            json.dumps(
                {
                    "project_id": f"unrelated-{idx}",
                    "run_id": idx + 1000,
                    "action_kind": "map.python",
                }
            ),
            1,
            now,
            now,
        )
        for idx in range(count)
    ]
    with queue.engine.begin() as cx:  # local TestClient apps use the SQLite queue.
        cx.exec_driver_sql(
            "INSERT INTO jobs (kind, payload, max_attempts, available_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            payloads,
        )


def test_project_run_status_jobs_and_timing_use_indexed_queue_refs(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_queued_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None

    workspace = client.app.state.workspace
    queue = workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    _insert_newer_unrelated_jobs(queue, count=5001)

    # Simulate a process restart: product status paths must recover from the
    # durable queue, not from the in-memory run->job map or live progress.
    workspace.run_jobs.clear()
    workspace.active_runs.clear()

    status_response = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status_response.status_code == 200, status_response.text
    public_status = status_response.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["action_kind"] == "map.python"
    assert "recipe" not in json.dumps(public_status)

    listed = client.get(f"/api/projects/{project_id}/actions/jobs")
    assert listed.status_code == 200, listed.text
    jobs = listed.json()["jobs"]
    assert [job["job_id"] for job in jobs] == [result.job_id]
    assert jobs[0]["run_id"] == result.run_id
    assert "payload" not in jobs[0]
    assert "recipe" not in json.dumps(jobs[0])

    timing = client.get(f"/api/projects/{project_id}/timing")
    assert timing.status_code == 200, timing.text
    timing_body = timing.json()
    assert timing_body["jobs"]["by_kind"][RUN_PROJECT_KIND]["count"] == 1
    assert timing_body["active_runs"][0]["run_id"] == result.run_id
    assert timing_body["active_runs"][0]["status"] == "queued"


def test_queue_schema_and_product_paths_have_structured_ref_boundary(
    tmp_path: Path,
) -> None:
    queue = SqliteJobQueue(tmp_path / ".queue.db")
    introspect = sqlite3.connect(queue.engine.url.database)
    introspect.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in introspect.execute("PRAGMA table_info(jobs)")}
        indexes = {row["name"] for row in introspect.execute("PRAGMA index_list(jobs)")}
    finally:
        introspect.close()
    assert {
        "project_id",
        "run_id",
        "feed_id",
        "sheet_id",
        "row_id",
        "receipt_id",
        "action_kind",
    } <= columns
    assert {
        "idx_jobs_project_run",
        "idx_jobs_project_status_id",
    } <= indexes
    assert {
        "project_id",
        "run_id",
        "feed_id",
        "sheet_id",
        "row_id",
        "receipt_id",
        "action_kind",
    } <= set(jobs_table.c.keys())

    assert hasattr(queue, "get_project_run_job")
    assert hasattr(queue, "list_project_jobs")
    assert hasattr(PostgresJobQueue, "get_project_run_job")
    assert hasattr(PostgresJobQueue, "list_project_jobs")
