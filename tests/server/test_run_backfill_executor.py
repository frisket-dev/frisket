from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.server.app import create_app
from http_test_helpers import (
    confirmation_hash_from_action_result,
    drain_queue,
    post_row_add_as_v1_action,
    post_v1_action_with_exact_confirmation,
)


class _StubAdapter:
    def __init__(self, reply: dict):
        self.reply = reply

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=25,
            tokens_out=10,
            cost=0.0,
            model=req.model,
        )


def _client(tmp_path: Path) -> TestClient:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _StubAdapter({"beat": "a"})  # noqa: SLF001
    return TestClient(create_app(tmp_path / "workspace", router=router))


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Backfill v1"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "stories.csv",
                'story\n"first story"\n"second story"\n',
                "text/csv",
            )
        },
    )
    assert response.status_code == 200, response.text
    return pid, int(response.json()["sheet_id"])


def _classify_spec(sheet_id: int) -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "source": ["story"],
            "fields": [{"name": "beat", "type": "category", "labels": ["a", "b"]}],
        },
        "idempotency_key": "backfill-source-classify",
    }


def _backfill_action(sheet_id: int, *, key: str = "backfill@sha256:test") -> dict:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "beat"},
        "output_names": {},
        "idempotency_key": key,
    }


def test_run_backfill_action_creates_fresh_generation_for_missing_cells(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    run_response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _classify_spec(sheet_id),
    )
    assert run_response.status_code == 200, run_response.text
    original_run_id = int(run_response.json()["run_id"])
    drain_queue(client)

    add_response = post_row_add_as_v1_action(
        client,
        pid,
        sheet_id,
        {"story": "late story"},
    )
    assert add_response.status_code == 200, add_response.text

    response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _backfill_action(sheet_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["action"]["kind"] == "run.backfill"
    assert body["status"] == "completed"
    assert body["run_id"] != original_run_id
    output = next(item for item in body["outputs"] if item["kind"] == "run_backfill")
    assert output["name"] == "beat"
    assert output["ref"]["filled"] == 1
    assert len(output["ref"]["filled_row_ids"]) == 1
    assert output["ref"]["requested_row_ids"] == output["ref"]["filled_row_ids"]

    status_response = client.get(
        f"/api/projects/{pid}/actions/runs/{body['run_id']}/status"
    )
    assert status_response.status_code == 200, status_response.text
    public_status = status_response.json()["run"]["public_status"]
    assert public_status["completed"] <= public_status["total"]

    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    beat = next(column for column in data["columns"] if column["name"] == "beat")
    assert data["rows"][2]["cells"][str(beat["id"])] == "a"

    replay = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _backfill_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == body["receipt_id"]
    assert replay.json()["outputs"][0]["ref"]["filled"] == 1


def _column_id(project, sheet_id: int, name: str) -> int:
    return int(
        next(
            row
            for row in project.columns(sheet_id, include_hidden=True)
            if row["name"] == name
        )["id"]
    )


def test_explicit_row_ids_creates_fresh_generation_for_exact_named_row(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    run_response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _classify_spec(sheet_id),
    )
    assert run_response.status_code == 200, run_response.text
    run_id = int(run_response.json()["run_id"])
    drain_queue(client)

    project = client.app.state.workspace.get(pid)
    beat_col = _column_id(project, sheet_id, "beat")
    first_row = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position LIMIT 1",
            (sheet_id,),
        ).fetchone()["id"]
    )
    action = _backfill_action(sheet_id, key="backfill-retry-row@sha256:test")
    action["scope"]["row_ids"] = [first_row]
    response = post_v1_action_with_exact_confirmation(client, pid, action)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["run_id"] != run_id
    output = next(item for item in body["outputs"] if item["kind"] == "run_backfill")
    assert output["ref"]["requested_row_ids"] == [first_row]
    assert output["ref"]["filled_row_ids"] == [first_row]
    assert output["ref"]["filled"] == 1
    original = project.db.execute(
        "SELECT outcome, value FROM results WHERE run_id=? AND row_id=? "
        "AND column_id=?",
        (run_id, first_row, beat_col),
    ).fetchone()
    row = project.db.execute(
        "SELECT outcome, value FROM results WHERE run_id=? AND row_id=? "
        "AND column_id=?",
        (body["run_id"], first_row, beat_col),
    ).fetchone()
    assert original["outcome"] == "ok"
    assert row["outcome"] == "ok"
    assert row["value"] is not None


def test_explicit_row_ids_rejects_rows_not_on_the_sheet(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    run_response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _classify_spec(sheet_id),
    )
    assert run_response.status_code == 200, run_response.text
    drain_queue(client)

    action = _backfill_action(sheet_id, key="backfill-bad-row@sha256:test")
    action["scope"]["row_ids"] = [999_999]
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "failed"
    error = body["errors"][0]
    assert error["code"] == "invalid_row_ref"
    assert error["field"] == "scope.row_ids"
    assert error["details"]["invalid_rows"] == [999_999]


def _unpriced_classify_spec(sheet_id: int) -> dict:
    spec = _classify_spec(sheet_id)
    # An unpriced model has no estimate, so the cost gate cannot certify the
    # backfill as "cheap" and must demand confirmation — exactly like a fresh
    # over-gate run (mirrors the map executor cost-gate idiom).
    spec["params"]["model"] = "anthropic/unpriced-test-model"
    return spec


def _counts(project) -> dict[str, int]:
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "results", "receipts")
    }


def test_run_backfill_over_gate_returns_needs_confirmation(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    # The original (confirmed) run pins the unpriced model into the stored spec.
    run_response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _unpriced_classify_spec(sheet_id),
    )
    assert run_response.status_code == 200, run_response.text
    drain_queue(client)
    add_response = post_row_add_as_v1_action(
        client,
        pid,
        sheet_id,
        {"story": "late story"},
    )
    assert add_response.status_code == 200, add_response.text

    project = client.app.state.workspace.get(pid)
    before = _counts(project)
    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_backfill_action(sheet_id, key="backfill-gate@sha256:test"),
    )

    assert response.status_code == 402, response.text
    body = response.json()
    assert body["status"] == "needs_confirmation"
    assert body["action"]["kind"] == "run.backfill"
    error = body["errors"][0]
    assert error["code"] == "model_cost_requires_confirmation"
    assert error["needs_confirmation"] is True
    assert error["field"] == "confirmation"
    # A gated backfill must not execute the unrun rows or write a receipt.
    assert _counts(project) == before


def test_run_backfill_confirmed_proceeds_over_gate(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    run_response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _unpriced_classify_spec(sheet_id),
    )
    assert run_response.status_code == 200, run_response.text
    drain_queue(client)
    add_response = post_row_add_as_v1_action(
        client,
        pid,
        sheet_id,
        {"story": "late story"},
    )
    assert add_response.status_code == 200, add_response.text

    action = _backfill_action(sheet_id, key="backfill-confirmed@sha256:test")
    challenge = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=action,
    )
    assert challenge.status_code == 402, challenge.text
    promise_set_hash = confirmation_hash_from_action_result(challenge.json())
    assert promise_set_hash is not None
    action["confirmation"] = promise_set_hash
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    output = next(item for item in body["outputs"] if item["kind"] == "run_backfill")
    assert output["ref"]["filled"] == 1


def test_run_backfill_estimate_scopes_missing_rows_without_mutation(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    run_response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _classify_spec(sheet_id),
    )
    assert run_response.status_code == 200, run_response.text
    drain_queue(client)
    add_response = post_row_add_as_v1_action(
        client,
        pid,
        sheet_id,
        {"story": "late story"},
    )
    assert add_response.status_code == 200, add_response.text

    project = client.app.state.workspace.get(pid)
    before_counts = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "results", "receipts")
    }
    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={
            "action": _backfill_action(sheet_id, key="backfill-estimate@sha256:test")
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.action_estimate_result.v1"
    assert body["action"]["kind"] == "run.backfill"
    assert body["estimate"]["rows"] == 1
    assert "cost" in body["estimate"]
    after_counts = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "results", "receipts")
    }
    assert after_counts == before_counts
