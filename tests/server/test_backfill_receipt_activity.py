from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.server.app import create_app
from http_test_helpers import (
    drain_queue,
    post_row_add_as_v1_action,
    post_v1_action_with_exact_confirmation,
)


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]):
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
    pid = client.post("/api/projects", json={"name": "Backfill activity"}).json()["id"]
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


def _classify_spec(sheet_id: int) -> dict[str, Any]:
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


def _backfill_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "beat"},
        "output_names": {},
        "idempotency_key": "backfill-activity@sha256:test",
    }


def test_run_backfill_writes_receipt_backed_activity_for_fresh_generation(
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
    before_ops = int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0])
    response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _backfill_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    action_body = response.json()
    after_ops = int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0])
    assert after_ops == before_ops + 1
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (action_body["receipt_id"],),
    ).fetchone()
    receipt_body = json.loads(receipt_row["body"])
    expected_column_id = int(receipt_body["outputs"][0]["ref"]["column_id"])

    activity = client.get(f"/api/projects/{pid}/activity/backfills")
    assert activity.status_code == 200, activity.text
    body = activity.json()
    assert body["schema_version"] == "frisket.backfill_activity_page.v1"
    assert body["page"] == {
        "schema_version": "frisket.backfill_activity_page.v1",
        "order": "desc",
        "offset": 0,
        "limit": 25,
        "total": 1,
        "has_more": False,
        "next_offset": None,
    }
    assert len(body["backfills"]) == 1
    item = body["backfills"][0]
    assert item == {
        "schema_version": "frisket.backfill_activity.v1",
        "receipt_id": action_body["receipt_id"],
        "status": "completed",
        "created_at": item["created_at"],
        "run_id": action_body["run_id"],
        "sheet_id": sheet_id,
        "column_id": expected_column_id,
        "column_name": "beat",
        "requested_count": 1,
        "filled_count": 1,
        "label": "Backfilled 1 cell in beat",
        "restorable": False,
    }
    assert isinstance(item["created_at"], str) and item["created_at"]

    replay = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _backfill_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == action_body["receipt_id"]
    replay_activity = client.get(f"/api/projects/{pid}/activity/backfills").json()
    assert replay_activity["page"]["total"] == 1
    assert [entry["receipt_id"] for entry in replay_activity["backfills"]] == [
        action_body["receipt_id"]
    ]

    filtered = client.get(
        f"/api/projects/{pid}/activity/backfills",
        params={"column_id": item["column_id"]},
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["page"]["total"] == 1

    empty = client.get(
        f"/api/projects/{pid}/activity/backfills",
        params={"column_id": item["column_id"] + 1000},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["backfills"] == []
    assert empty.json()["page"]["total"] == 0


def test_backfill_activity_skips_reserved_and_malformed_receipts(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, status, body) VALUES (?,?,?,?)",
        (
            "receipt_reserved_backfill",
            "run.backfill",
            "queued",
            json.dumps({"schema_version": "frisket.receipt.v1", "outputs": []}),
        ),
    )
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, status, body) VALUES (?,?,?,?)",
        (
            "receipt_malformed_backfill",
            "run.backfill",
            "completed",
            json.dumps(
                {
                    "schema_version": "frisket.receipt.v1",
                    "outputs": [{"name": "broken", "ref": {"kind": "run_backfill"}}],
                }
            ),
        ),
    )
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, status, body) VALUES (?,?,?,?)",
        (
            "receipt_zero_backfill",
            "run.backfill",
            "completed",
            json.dumps(
                {
                    "schema_version": "frisket.receipt.v1",
                    "outputs": [
                        {
                            "name": "beat",
                            "ref": {
                                "kind": "run_backfill",
                                "sheet_id": sheet_id,
                                "column_id": 1234,
                                "column_name": "beat",
                                "run_id": 99,
                                "requested_row_ids": [],
                                "filled_row_ids": [55],
                                "filled": 0,
                            },
                        }
                    ],
                }
            ),
        ),
    )
    project.db.commit()

    response = client.get(f"/api/projects/{pid}/activity/backfills")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"]["total"] == 1
    assert [item["receipt_id"] for item in body["backfills"]] == [
        "receipt_zero_backfill"
    ]
    assert body["backfills"][0]["filled_count"] == 0
    assert body["backfills"][0]["label"] == "Backfilled 0 cells in beat"


def test_backfill_activity_rejects_invalid_page_params(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    for params in ({"offset": -1}, {"limit": 0}, {"limit": 101}):
        response = client.get(f"/api/projects/{pid}/activity/backfills", params=params)
        assert response.status_code == 422
