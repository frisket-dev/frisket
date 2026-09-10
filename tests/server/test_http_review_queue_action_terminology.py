from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from frisket.authoring.action_metadata import action_metadata_for_action_kind
from frisket.contracts.action import ActionResult
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.server.app import create_app
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation


CSV = "story\nMayor met a lobbyist before the vote.\n"


class StubAdapter:
    async def complete(self, req: LLMRequest, client: Any) -> LLMResponse:  # noqa: ANN401
        data = {
            "beat": "civic",
            "beat_confidence": 0.41,
            "beat_justification": "mentions city government",
        }
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=10,
            tokens_out=10,
            cost=0.0,
            model=req.model,
        )


def _stub_router() -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = StubAdapter()  # noqa: SLF001
    return router


def test_action_metadata_uses_stable_unknown_fallback() -> None:
    assert action_metadata_for_action_kind(None) == {
        "action_kind": "unknown",
        "action_name": "Unknown action",
    }


def test_http_review_queue_and_bundles_use_action_metadata(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws", router=_stub_router()))
    project_id = client.post("/api/projects", json={"name": "Review v1"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("stories.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    run = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        {
            "action_id": "map.classify",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["story"],
                "engine": "llm",
                "model": "anthropic/claude-haiku-4-5",
                "context": "One-line local news items.",
                "fields": [
                    {
                        "name": "beat",
                        "type": "category",
                        "labels": ["civic", "private"],
                    }
                ],
                "include_confidence": True,
                "include_justification": True,
            },
            "idempotency_key": "review-queue-v1-classify@sha256:stable",
        },
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    queue_response = client.get(f"/api/projects/{project_id}/review/queue")
    assert queue_response.status_code == 200, queue_response.text
    queue = queue_response.json()
    assert "recipe" not in json.dumps(queue).lower()
    assert queue == [
        {
            "bundle_id": "1:1",
            "run_id": result.run_id,
            "row_id": 1,
            "column_id": queue[0]["column_id"],
            "column_name": "beat",
            "sheet_id": sheet_id,
            "value": "civic",
            "confidence": 0.41,
            "justification": "mentions city government",
            "review_decision": None,
            "review_note": None,
            "action_kind": "map.classify",
            "action_name": "Classify rows",
            "model": "anthropic/claude-haiku-4-5",
            "source": {"story": "Mayor met a lobbyist before the vote."},
        }
    ]

    bundles_response = client.get(f"/api/projects/{project_id}/review/bundles")
    assert bundles_response.status_code == 200, bundles_response.text
    bundles_body = bundles_response.json()
    assert "recipe" not in json.dumps(bundles_body).lower()
    bundles = bundles_body["bundles"]
    bundle = bundles[0]
    assert bundle["action_kind"] == "map.classify"
    assert bundle["action_name"] == "Classify rows"
    assert bundle["model"] == "anthropic/claude-haiku-4-5"
    assert bundle["sheet_name"] == "stories"
    assert bundle["source"] == {"story": "Mayor met a lobbyist before the vote."}
    assert bundle["fields"] == [
        {
            "run_id": result.run_id,
            "row_id": 1,
            "column_id": queue[0]["column_id"],
            "column_name": "beat",
            "column_type": "category",
            "sheet_id": sheet_id,
            "value": "civic",
            "confidence": 0.41,
            "justification": "mentions city government",
            "error": None,
            "review_state": "unreviewed",
            "review_decision": None,
            "review_note": None,
            "role": "field",
            "chore": True,
        }
    ]
    assert {item["column_name"] for item in bundle["evidence"]} == {
        "beat_confidence",
        "beat_justification",
    }
    assert all(item["role"] == "evidence" for item in bundle["evidence"])
    assert all(item["chore"] is False for item in bundle["evidence"])
    assert {item["column_name"] for item in bundle["items"]} == {
        "beat",
        "beat_confidence",
        "beat_justification",
    }
