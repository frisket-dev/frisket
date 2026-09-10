from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from frisket.contracts.action import ActionResult, Receipt
from frisket.engine.executor.queued_actions import queued_v1_payload_envelope
from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram
from frisket.engine.jobs.runs import RUN_PROJECT_KIND
from frisket.engine.runner import OutputColumnExists
from frisket.server.app import create_app
from http_test_helpers import drain_queue


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace", run_status_grace_seconds=3600))


def _seed_regex(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": "Typed queue"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    columns = {"note": project.add_column(sheet_id, "note", type="text")}
    row_ids = project.add_rows(
        sheet_id,
        [{"note": "Call 212-555-0123"}, {"note": "No phone"}],
        columns,
    )
    return project_id, sheet_id, row_ids


def _regex_request(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
        },
        "output_names": {"extracted": "phone"},
        "idempotency_key": "typed-regex-queue@1",
    }


def test_typed_regex_queues_canonical_request_and_worker_finishes(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids = _seed_regex(client)
    body = _regex_request(sheet_id)

    first_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=body
    )
    assert first_response.status_code == 200, first_response.text
    first = ActionResult.model_validate(first_response.json())
    assert first.status == "queued"
    assert first.run_id is not None
    assert first.job_id is not None
    assert first.receipt_id is not None

    job = client.app.state.workspace.queue.get(first.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["v1_action"] == body
    assert "kind" not in job.payload["v1_action"]
    assert job.payload["action_kind"] == "map.regex_extract"
    assert set(job.payload["v1_input_column_ids"]) == {"note"}
    assert job.payload["v1_input_column_types"] == {"note": "text"}
    assert queued_v1_payload_envelope(job.payload) is not None

    replay = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )
    assert (replay.run_id, replay.job_id, replay.receipt_id) == (
        first.run_id,
        first.job_id,
        first.receipt_id,
    )

    drain_queue(client)

    project = client.app.state.workspace.get(project_id)
    phone = next(
        column for column in project.columns(sheet_id) if column["name"] == "phone"
    )
    assert project.get_values(sheet_id, int(phone["id"]), row_ids=row_ids) == {
        row_ids[0]: "212-555-0123",
        row_ids[1]: None,
    }
    stored = project.db.execute(
        "SELECT status,body FROM receipts WHERE id=?", (first.receipt_id,)
    ).fetchone()
    assert stored is not None
    assert stored["status"] == "completed"
    receipt = Receipt.model_validate(json.loads(stored["body"]))
    assert receipt.action_kind == "map.regex_extract"
    assert receipt.run_id == first.run_id


def test_typed_queue_rejects_hash_spec_and_source_drift(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    body = _regex_request(sheet_id)
    result = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )
    assert result.job_id is not None
    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None

    bad_hash = copy.deepcopy(job.payload)
    bad_hash["v1_params_hash"] = "sha256:tampered"
    bad_hash["spec"]["_frisket_queued_action_run"]["params_hash"] = "sha256:tampered"
    assert queued_v1_payload_envelope(bad_hash) is None

    bad_spec = copy.deepcopy(job.payload)
    bad_spec["spec"]["sheet_id"] = sheet_id + 1
    assert queued_v1_payload_envelope(bad_spec) is None

    project = client.app.state.workspace.get(project_id)
    project.db.execute(
        "UPDATE columns SET name='renamed_note' WHERE sheet_id=? AND name='note'",
        (sheet_id,),
    )
    project.db.commit()
    drain_queue(client)

    stored = project.db.execute(
        "SELECT status,body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert stored is not None
    assert stored["status"] == "failed"
    receipt = Receipt.model_validate(json.loads(stored["body"]))
    assert receipt.errors[0].code == "stale_input"


def test_typed_geo_point_uses_the_same_project_run_adapter(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Typed geo"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    columns = {
        "lat": project.add_column(sheet_id, "lat", type="number"),
        "lon": project.add_column(sheet_id, "lon", type="number"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [{"lat": 40.7128, "lon": -74.006}],
        columns,
    )
    body = {
        "action_id": "map.to_geo_point",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"latitude_column": "lat", "longitude_column": "lon"},
        "output_names": {"geo_point": "location"},
        "idempotency_key": "typed-geo-queue@1",
    }

    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.job_id is not None
    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.payload["v1_action"] == body
    assert set(job.payload["v1_input_column_ids"]) == {"lat", "lon"}

    drain_queue(client)

    location = next(
        column for column in project.columns(sheet_id) if column["name"] == "location"
    )
    assert project.get_values(sheet_id, int(location["id"]), row_ids=row_ids) == {
        row_ids[0]: {"lat": 40.7128, "lon": -74.006}
    }


def test_typed_worker_refuses_persisted_run_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    result = ActionResult.model_validate(
        client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_regex_request(sheet_id),
        ).json()
    )
    project = client.app.state.workspace.get(project_id)
    run = project.db.execute(
        "SELECT params FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    persisted = json.loads(run["params"])
    persisted["sheet_id"] = sheet_id + 1
    project.db.execute(
        "UPDATE runs SET params=? WHERE id=?",
        (json.dumps(persisted), result.run_id),
    )
    project.db.commit()

    async def unexpected_execute(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("typed handler executed despite durable identity drift")

    monkeypatch.setattr(_TypedMapRowsProgram, "execute", unexpected_execute)
    drain_queue(client)

    assert (
        project.db.execute(
            "SELECT status FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()["status"]
        == "failed"
    )
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt["status"] == "failed"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE receipt_id=? AND status='active'",
            (result.receipt_id,),
        ).fetchone()[0]
        == 0
    )


def test_typed_queue_claim_conflict_points_to_output_names(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    first = _regex_request(sheet_id)
    assert (
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=first).json()[
            "status"
        ]
        == "queued"
    )
    project = client.app.state.workspace.get(project_id)
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='phone'", (sheet_id,)
    )
    project.db.commit()
    second = {**first, "idempotency_key": "typed-regex-queue@2"}

    result = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=second).json()
    )

    assert result.status == "failed"
    assert result.errors[0].code == "output_column_busy"
    assert result.errors[0].field == "output_names"


def test_typed_queue_prepare_collision_points_to_output_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    body = _regex_request(sheet_id)

    def collide(*_args: Any, **_kwargs: Any) -> Any:
        raise OutputColumnExists(["phone"])

    monkeypatch.setattr(
        "frisket.server.action_enqueue.MapRunner.prepare_run",
        collide,
    )
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=body,
    )

    assert response.status_code == 409, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.errors[0].code == "output_column_exists"
    assert result.errors[0].field == "output_names"
    project = client.app.state.workspace.get(project_id)
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
            (body["idempotency_key"],),
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )


def _interrupt_typed_enqueue(
    client: TestClient,
    project_id: str,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str]:
    queue = client.app.state.workspace.queue
    enqueue = queue.enqueue

    def interrupted(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("simulated crash before enqueue")

    monkeypatch.setattr(queue, "enqueue", interrupted)
    with pytest.raises(RuntimeError, match="before enqueue"):
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    monkeypatch.setattr(queue, "enqueue", enqueue)
    project = client.app.state.workspace.get(project_id)
    row = project.db.execute(
        "SELECT id,run_id FROM receipts WHERE idempotency_key=?",
        (body["idempotency_key"],),
    ).fetchone()
    return int(row["run_id"]), str(row["id"])


def test_typed_prepared_before_enqueue_recovers_with_original_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    body = _regex_request(sheet_id)
    prepared_run_id, receipt_id = _interrupt_typed_enqueue(
        client, project_id, body, monkeypatch
    )

    recovered = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )

    assert recovered.status == "queued"
    assert recovered.run_id == prepared_run_id
    assert recovered.receipt_id == receipt_id
    drain_queue(client)
    project = client.app.state.workspace.get(project_id)
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()["status"]
        == "completed"
    )


def test_typed_prepared_recovery_rejects_recreated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    body = _regex_request(sheet_id)
    prepared_run_id, receipt_id = _interrupt_typed_enqueue(
        client, project_id, body, monkeypatch
    )
    project = client.app.state.workspace.get(project_id)
    source_id = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "note"
    )
    project.db.execute("DELETE FROM columns WHERE id=?", (source_id,))
    project.db.commit()
    replacement_id = project.add_column(sheet_id, "note", type="text")
    assert replacement_id != source_id

    refused = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )

    assert refused.status == "failed"
    assert refused.errors[0].code == "stale_input"
    assert (
        project.db.execute(
            "SELECT status FROM runs WHERE id=?", (prepared_run_id,)
        ).fetchone()["status"]
        == "failed"
    )
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()["status"]
        == "failed"
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE receipt_id=? AND status='active'",
            (receipt_id,),
        ).fetchone()[0]
        == 0
    )


def test_typed_queued_cancel_terminalizes_receipt_and_claim(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _seed_regex(client)
    body = _regex_request(sheet_id)
    queued = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )

    response = client.post(
        f"/api/projects/{project_id}/actions/runs/{queued.run_id}/cancel"
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    project = client.app.state.workspace.get(project_id)
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (queued.receipt_id,)
        ).fetchone()["status"]
        == "cancelled"
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE receipt_id=? AND status='active'",
            (queued.receipt_id,),
        ).fetchone()[0]
        == 0
    )
