from __future__ import annotations

import asyncio
from typing import Any

import httpx

from action_test_helpers import run_typed_map_request, typed_map_request
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram
from frisket.engine.store import Project
from frisket.engine.executor.actions import run_action_spec


def run_action_with_exact_confirmation(project, body, **kwargs):
    result = run_action_spec(project, body, **kwargs)
    if result.status == "needs_confirmation":
        result = run_action_spec(
            project,
            {**body, "confirmation": result.errors[0].details["promise_set_hash"]},
            **kwargs,
        )
    return result


def test_api_pipeline_completed_replays_keep_outputs_without_reexecution(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "api-pipeline-replay.frisket")
    client: httpx.AsyncClient | None = None
    try:
        sheet_id = project.add_sheet("data")
        columns = {"id": project.add_column(sheet_id, "id", type="text")}
        project.add_rows(sheet_id, [{"id": "1"}, {"id": "2"}], columns)

        request_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_calls.append(request.url.path)
            return httpx.Response(
                200,
                json={"city": f"City {request.url.path.rsplit('/', 1)[-1]}"},
            )

        monkeypatch.setattr(
            "frisket.ops.netguard.safe_pinned_addresses",
            lambda url, **kwargs: ["93.184.216.34"],
        )
        router = ModelRouter(cache=None, cache_mode="off")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router._client = client
        api_action: dict[str, Any] = {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "request": {
                    "method": "GET",
                    "url": "https://api.test.example/items/{{id}}",
                }
            },
            "output_names": {"api_result": "api_result"},
            "idempotency_key": "map_api_call@sha256:replay-output",
        }

        first_api = run_action_with_exact_confirmation(
            project, api_action, project_id="api-replay", router=router
        )
        assert first_api.status == "completed", first_api.errors
        assert [(out.kind, out.name) for out in first_api.outputs] == [
            ("column", "api_result")
        ]
        assert request_calls == ["/items/1", "/items/2"]

        replay_api = run_action_with_exact_confirmation(
            project, api_action, project_id="api-replay", router=router
        )
        assert replay_api.status == "completed", replay_api.errors
        assert replay_api.receipt_id == first_api.receipt_id
        assert [(out.kind, out.name) for out in replay_api.outputs] == [
            ("column", "api_result")
        ]
        assert replay_api.outputs[0].ref == first_api.outputs[0].ref
        assert request_calls == ["/items/1", "/items/2"]

        extraction_calls = 0
        original_execute = _TypedMapRowsProgram.execute

        async def counted_execute(self, row_values, spec, ctx):
            nonlocal extraction_calls
            extraction_calls += 1
            return await original_execute(self, row_values, spec, ctx)

        monkeypatch.setattr(_TypedMapRowsProgram, "execute", counted_execute)
        extract_action = typed_map_request(
            "map.columns_from_json",
            sheet_id,
            params={
                "source_column": "api_result",
                "routes": [{"name": "city", "path": "$.city"}],
            },
            output_names={},
            idempotency_key="map_columns_from_json@sha256:replay-output",
        )

        first_extract = run_typed_map_request(
            project, extract_action, project_id="api-replay"
        )
        assert first_extract.status == "completed", first_extract.errors
        assert [(out.kind, out.name) for out in first_extract.outputs] == [
            ("column", "city")
        ]
        assert extraction_calls == 2

        replay_extract = run_typed_map_request(
            project, extract_action, project_id="api-replay"
        )
        assert replay_extract.status == "completed", replay_extract.errors
        assert replay_extract.receipt_id == first_extract.receipt_id
        assert [(out.kind, out.name) for out in replay_extract.outputs] == [
            ("column", "city")
        ]
        assert replay_extract.outputs[0].ref == first_extract.outputs[0].ref
        assert extraction_calls == 2
    finally:
        if client is not None:
            asyncio.run(client.aclose())
        project.close()
