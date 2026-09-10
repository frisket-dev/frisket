from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.server.app import create_app
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation


CSV = (
    "story\n"
    "Mayor Linda Reyes met lobbyist Tom Quayle before the vote.\n"
    "Transit riders protested a weekend bus cut.\n"
)


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


class StubAdapter:
    async def complete(self, req: LLMRequest, client: Any) -> LLMResponse:  # noqa: ANN401
        data = {"beat": "civic"}
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


def test_action_run_trace_uses_action_metadata_without_recipe_bridge(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws", router=_stub_router()))
    project_id = client.post("/api/projects", json={"name": "Trace v1"}).json()["id"]
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
                "context": "Classify one-line local news items.",
                "fields": [
                    {
                        "name": "beat",
                        "type": "category",
                        "labels": ["civic", "transportation"],
                    }
                ],
            },
            "idempotency_key": "action-trace-v1-classify@sha256:stable",
        },
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/trace"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # This route still uses the older programmatic action envelope; the v1
    # cutover in this slice is the route/payload boundary consumed by the UI.
    assert body["schema_version"] == "frisket.actions.v1"
    assert body["action"] == "run_trace"
    assert body["project_id"] == project_id
    assert body["run_id"] == result.run_id
    assert body["recorded"] is True
    _assert_no_recipe_keys(body)

    trace = body["trace"]
    assert trace["run_id"] == result.run_id
    assert trace["trace_id"]
    assert trace["action_kind"] == "map.classify"
    assert trace["action_kind"] == "map.classify"
    assert trace["action_name"] == "Classify rows"
    assert trace["model"] == "anthropic/claude-haiku-4-5"
    assert len(trace["rows"]) == 2
    assert {row["trace_id"] for row in trace["rows"]} == {trace["trace_id"]}
    assert any("Linda Reyes" in json.dumps(row["prompt"]) for row in trace["rows"])
    assert all(
        json.loads(row["raw_response"]) == {"beat": "civic"} for row in trace["rows"]
    )
