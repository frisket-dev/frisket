from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from frisket.contracts.action import ActionResult
from frisket.engine.jobs.queue import SqliteJobQueue, jobs_table
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.server import provider_config
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec


CSV = "note\nCall 212-555-0123\nNo phone\n"


def _seed_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "Queued terminal receipts"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _queued_action(
    sheet_id: int,
    *,
    output_name: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return v1_action_from_canonical_run_spec(
        queued_python_run_spec(sheet_id, "note", output_name),
        idempotency_key=idempotency_key,
    )


def _queue_action(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    *,
    output_name: str,
    idempotency_key: str,
) -> tuple[ActionResult, dict[str, Any]]:
    action = _queued_action(
        sheet_id,
        output_name=output_name,
        idempotency_key=idempotency_key,
    )
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=action,
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    return result, action


def _receipt(project: Any, receipt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = project.db.execute(
        "SELECT run_id, status, body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    data = dict(row)
    body = json.loads(row["body"])
    return data, body


def _active_output_claim_count(project: Any, output_name: str) -> int:
    return int(
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims "
            "WHERE output_name=? AND status='active'",
            (output_name,),
        ).fetchone()[0]
    )


def _paths_with_key(value: Any, key: str, path: str = "$root") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            child_path = f"{path}.{child_key}"
            if child_key == key:
                found.append(child_path)
            found.extend(_paths_with_key(child, key, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_paths_with_key(child, key, f"{path}[{index}]"))
    return found


def _post_same_action(
    client: TestClient,
    project_id: str,
    action: dict[str, Any],
) -> tuple[int, ActionResult]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=action,
    )
    return response.status_code, ActionResult.model_validate(response.json())


def test_project_run_queue_rejects_identity_metadata_before_reservation(
    tmp_path: Path,
) -> None:
    """Authenticated queue metadata cannot replace the prepared project id.

    A spread-last override used to leave the receipt under project A while the
    queue indexed and routed the job as project B.  Reject the programmer error
    before reserving an action lifecycle or enqueueing work.
    """
    client = TestClient(
        create_app(
            tmp_path / "ws",
            queue_payload_extra={"project_id": "different-project"},
        )
    )
    project_id, sheet_id = _seed_project(client)
    project = client.app.state.workspace.get(project_id)
    before_receipts = project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    before_runs = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    action = _queued_action(
        sheet_id,
        output_name="identity_collision",
        idempotency_key="queued-identity-collision@sha256:stable",
    )

    with pytest.raises(ValueError, match="project_id"):
        client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=action,
        )

    assert (
        project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        == before_receipts
    )
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before_runs


def test_cancelled_queued_run_terminalizes_receipt_and_replay(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client)
    result, action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name="phone_cancel",
        idempotency_key="queued-terminal-cancel@sha256:stable",
    )

    cancelled = client.post(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["queue_cancelled"] is True

    project = client.app.state.workspace.get(project_id)
    row, body = _receipt(project, result.receipt_id)
    assert row["run_id"] == result.run_id
    assert row["status"] == "cancelled"
    assert body["run_id"] == result.run_id
    assert body["status"] == "cancelled"
    assert _active_output_claim_count(project, "phone_cancel") == 0

    status_code, replay = _post_same_action(client, project_id, action)
    assert status_code == 200, replay.model_dump_json()
    assert replay.status == "cancelled"
    assert replay.run_id == result.run_id
    assert replay.job_id == result.job_id
    assert replay.receipt_id == result.receipt_id


def test_successful_queued_run_releases_output_claim(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client)
    result, action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name="phone_done",
        idempotency_key="queued-terminal-done@sha256:stable",
    )

    project = client.app.state.workspace.get(project_id)
    assert _active_output_claim_count(project, "phone_done") == 1
    queue = client.app.state.workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    job = queue.get(result.job_id)
    assert job is not None
    assert _paths_with_key(job.payload, "recipe") == []
    assert job.payload["action_kind"] == "map.python"
    assert job.payload["spec"]["action_kind"] == "map.python"
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(cache=None, cache_mode="off"),
    )
    worker = Worker(queue, registry, worker_id="queued-terminal-success")
    assert worker.run_once() is True

    row, body = _receipt(project, result.receipt_id)
    assert row["run_id"] == result.run_id
    assert row["status"] == "completed"
    assert body["run_id"] == result.run_id
    assert body["status"] == "completed"
    assert _active_output_claim_count(project, "phone_done") == 0

    status_code, replay = _post_same_action(client, project_id, action)
    assert status_code == 200
    assert replay.status == "completed"
    assert replay.run_id == result.run_id
    assert replay.receipt_id == result.receipt_id


@pytest.mark.parametrize(
    "corruption",
    [
        "top_level_recipe",
        "bare_spec_kind",
        "outer_kind_conflict",
        "action_kind_conflict",
        "marker_kind_conflict",
    ],
)
def test_queued_identity_corruption_refuses_before_recipe_or_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client)
    result, _action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name=f"phone_{corruption}",
        idempotency_key=f"queued-identity-{corruption}@sha256:stable",
    )
    queue = client.app.state.workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    job = queue.get(result.job_id)
    assert job is not None
    payload = copy.deepcopy(job.payload)
    if corruption == "top_level_recipe":
        payload["spec"]["recipe"] = "python"
    elif corruption == "bare_spec_kind":
        payload["spec"]["action_kind"] = "python"
    elif corruption == "outer_kind_conflict":
        payload["action_kind"] = "map.extract"
    elif corruption == "action_kind_conflict":
        payload["v1_action"]["kind"] = "map.extract"
    else:
        payload["spec"]["_frisket_queued_action_run"]["action_kind"] = "map.extract"
    with queue.engine.begin() as connection:
        connection.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job.id)
            .values(payload=json.dumps(payload))
        )

    from frisket.engine.runner import validation

    def unexpected_lookup(_spec: dict[str, Any]) -> Any:
        raise AssertionError("identity refusal must happen before recipe lookup")

    monkeypatch.setattr(validation, "recipe_for_spec", unexpected_lookup)
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(cache=None, cache_mode="off"),
    )
    assert Worker(queue, registry, worker_id="invalid-identity").run_once() is True

    project = client.app.state.workspace.get(project_id)
    row, body = _receipt(project, result.receipt_id)
    assert row["status"] == "failed"
    assert body["errors"][0]["code"] == "invalid_queued_action_identity"
    assert _active_output_claim_count(project, f"phone_{corruption}") == 0


def test_deleted_bound_local_endpoint_terminalizes_before_dispatch(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    endpoint = provider_config.create_local_endpoint(
        client.app.state.workspace.root,
        name="Disposable",
        url="http://127.0.0.1:11434",
    )
    project_id, sheet_id = _seed_project(client)
    result, _action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name="phone_endpoint_deleted",
        idempotency_key="queued-endpoint-deleted@sha256:stable",
    )
    queue = client.app.state.workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    job = queue.get(result.job_id)
    assert job is not None
    payload = {
        **job.payload,
        "v1_local_endpoint_bindings": [
            {"endpoint_id": endpoint.endpoint_id, "origin": endpoint.origin}
        ],
    }
    with queue.engine.begin() as connection:
        connection.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job.id)
            .values(payload=json.dumps(payload))
        )
    assert provider_config.delete_local_endpoint(
        client.app.state.workspace.root, endpoint.endpoint_id
    )

    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(cache=None, cache_mode="off"),
    )
    worker = Worker(queue, registry, worker_id="deleted-endpoint")
    assert worker.run_once() is True

    project = client.app.state.workspace.get(project_id)
    row, body = _receipt(project, result.receipt_id)
    assert row["status"] == "failed"
    assert body["errors"][0]["code"] == "local_endpoint_unavailable"
    assert body["errors"][0]["details"] == {"endpoint_id": endpoint.endpoint_id}
    assert _active_output_claim_count(project, "phone_endpoint_deleted") == 0
    assert queue.get(job.id).status == "done"


def test_terminal_run_duplicate_does_not_require_deleted_endpoint(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    endpoint = provider_config.create_local_endpoint(
        client.app.state.workspace.root,
        name="Disposable",
        url="http://127.0.0.1:11434",
    )
    project_id, sheet_id = _seed_project(client)
    result, _action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name="phone_terminal_deleted",
        idempotency_key="queued-terminal-endpoint-deleted@sha256:stable",
    )
    queue = client.app.state.workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    original = queue.get(result.job_id)
    assert original is not None
    payload = {
        **original.payload,
        "v1_local_endpoint_bindings": [
            {"endpoint_id": endpoint.endpoint_id, "origin": endpoint.origin}
        ],
    }
    with queue.engine.begin() as connection:
        connection.execute(
            jobs_table.update()
            .where(jobs_table.c.id == original.id)
            .values(payload=json.dumps(payload))
        )

    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(cache=None, cache_mode="off"),
    )
    worker = Worker(queue, registry, worker_id="terminal-binding")
    assert worker.run_once() is True
    assert provider_config.delete_local_endpoint(
        client.app.state.workspace.root, endpoint.endpoint_id
    )

    duplicate_id = queue.enqueue("project.run", payload)
    assert worker.run_once() is True
    duplicate = queue.get(duplicate_id)
    assert duplicate is not None and duplicate.status == "done"
    project = client.app.state.workspace.get(project_id)
    row, body = _receipt(project, result.receipt_id)
    assert row["status"] == "completed"
    assert body["status"] == "completed"


def test_queue_terminal_failure_terminalizes_receipt_and_replay(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client)
    result, action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name="phone_failed",
        idempotency_key="queued-terminal-failed@sha256:stable",
    )
    project = client.app.state.workspace.get(project_id)
    project.db.execute(
        "UPDATE output_column_claims SET claim_token=? WHERE receipt_id=?",
        ("claim:custom-terminal-token", result.receipt_id),
    )
    project.db.commit()
    assert _active_output_claim_count(project, "phone_failed") == 1

    queue = client.app.state.workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    job = queue.claim("test-worker")
    assert job is not None and job.id == result.job_id
    assert queue.fail(
        job.id,
        "test-worker",
        "worker lease expired (attempts exhausted)",
        retry=False,
    )

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    assert status.json()["run"]["public_status"]["status"] == "failed"

    row, body = _receipt(project, result.receipt_id)
    assert row["run_id"] == result.run_id
    assert row["status"] == "failed"
    assert body["run_id"] == result.run_id
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "queue_job_failed"
    assert _active_output_claim_count(project, "phone_failed") == 0

    status_code, replay = _post_same_action(client, project_id, action)
    assert status_code == 400
    assert replay.status == "failed"
    assert replay.run_id == result.run_id
    assert replay.receipt_id == result.receipt_id
    assert replay.errors[0].code == "queue_job_failed"


def test_missing_queue_job_terminalizes_receipt_without_hiding_orphan_status(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=0.0))
    project_id, sheet_id = _seed_project(client)
    result, action = _queue_action(
        client,
        project_id,
        sheet_id,
        output_name="phone_missing",
        idempotency_key="queued-terminal-missing@sha256:stable",
    )
    queue = client.app.state.workspace.queue
    assert isinstance(queue, SqliteJobQueue)
    with queue.engine.begin() as cx:
        cx.execute(jobs_table.delete().where(jobs_table.c.id == result.job_id))
    client.app.state.workspace.active_runs.clear()

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "orphaned"
    assert public_status["stalled_reason"] == "queue_job_missing"

    project = client.app.state.workspace.get(project_id)
    row, body = _receipt(project, result.receipt_id)
    assert row["run_id"] == result.run_id
    assert row["status"] == "failed"
    assert body["run_id"] == result.run_id
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "queue_job_missing"
    assert _active_output_claim_count(project, "phone_missing") == 0

    before_repeat = (
        tuple(
            project.db.execute(
                "SELECT status, finished_at FROM runs WHERE id=?",
                (result.run_id,),
            ).fetchone()
        ),
        tuple(
            project.db.execute(
                "SELECT status, body FROM receipts WHERE id=?",
                (result.receipt_id,),
            ).fetchone()
        ),
        [
            tuple(item)
            for item in project.db.execute(
                "SELECT status, released_at FROM output_column_claims "
                "WHERE run_id=? ORDER BY id",
                (result.run_id,),
            ).fetchall()
        ],
    )
    repeated_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert repeated_status.status_code == 200, repeated_status.text
    repeated_public = repeated_status.json()["run"]["public_status"]
    assert repeated_public["status"] == "orphaned"
    assert repeated_public["stalled_reason"] == "queue_job_missing"
    after_repeat = (
        tuple(
            project.db.execute(
                "SELECT status, finished_at FROM runs WHERE id=?",
                (result.run_id,),
            ).fetchone()
        ),
        tuple(
            project.db.execute(
                "SELECT status, body FROM receipts WHERE id=?",
                (result.receipt_id,),
            ).fetchone()
        ),
        [
            tuple(item)
            for item in project.db.execute(
                "SELECT status, released_at FROM output_column_claims "
                "WHERE run_id=? ORDER BY id",
                (result.run_id,),
            ).fetchall()
        ],
    )
    assert after_repeat == before_repeat

    status_code, replay = _post_same_action(client, project_id, action)
    assert status_code == 400
    assert replay.status == "failed"
    assert replay.run_id == result.run_id
    assert replay.receipt_id == result.receipt_id
    assert replay.errors[0].code == "queue_job_missing"
