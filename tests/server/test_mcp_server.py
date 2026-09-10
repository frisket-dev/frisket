"""MCP server transport and tool-contract tests.

Local binding: real stdio-style sessions via the SDK's in-process memory
transport (create_connected_server_and_client_session) against a temp
workspace — keyless, network-free, runs replay from a primed cache exactly
like tests/test_runner.py. Hosted binding: HostedBackend pointed at the
local FastAPI app over httpx.ASGITransport, so the thin-client mapping
(incl. the 402 cost gate -> needs_confirmation) is exercised without a
live server or network.
"""

import asyncio
import json
import sys

import httpx
import pytest
from pydantic import ValidationError

from frisket.actions.types import ActionRequest
from frisket.ai.llm import (
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from frisket.contracts.action import Receipt
from frisket.engine.jobs import Worker
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.execution_routes import RouteStore
from frisket.server.runtime_settings import save_workspace_cost_preapproval_usd
from frisket.server.mcp import HostedBackend, LocalBackend, create_mcp_server
from frisket.server.services.action_runs import ActionRunService
from frisket.server.mcp.server import ActionToolSpec, RegisteredActionToolSpec
from frisket.actions.classify import ClassifyParams, classify_prompt
from frisket.actions.types import Row

MODEL = "anthropic/claude-haiku-4-5"

TOOL_NAMES = {
    "list_projects",
    "list_sheets",
    "read_sheet",
    "search",
    "run_action",
    "backfill_run",
    "get_run_status",
}


def classify_action(sheet_id: int, **param_overrides) -> dict:
    row_ids = param_overrides.pop("row_ids", None)
    params = {
        "model": MODEL,
        "engine": "llm",
        "source": ["text"],
        "context": "Test rows.",
        "fields": [
            {
                "name": "relevance",
                "type": "score",
                "description": "0-10 relevance",
            }
        ],
    }
    params.update(param_overrides)
    return {
        "action_id": "map.classify",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            **({"row_ids": row_ids} if row_ids is not None else {}),
        },
        "params": params,
    }


def map_python_action(sheet_id: int, *, idempotency_key: str) -> dict:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["text"],
            "code": "result = {'value': row['text'].upper()}",
            "return_schema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
            },
            "output_routes": [
                {
                    "name": "value",
                    "path": "$.value",
                    "target": {
                        "kind": "column",
                        "type": "text",
                    },
                }
            ],
        },
        "output_names": {"value": "uppercase"},
        "idempotency_key": idempotency_key,
    }


def template_action(sheet_id: int, *, idempotency_key: str | None = None) -> dict:
    action = {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"template": {"text": "{{text}}!"}},
        "output_names": {"rendered": "rendered_text"},
    }
    if idempotency_key is not None:
        action["idempotency_key"] = idempotency_key
    return action


def seed_workspace(root, texts: list[str], name: str = "demo") -> int:
    """Create <root>/<name>.frisket with a 'data' sheet of one text column."""
    root.mkdir(parents=True, exist_ok=True)
    p = Project.create(root / f"{name}.frisket", name=name)
    sheet = p.add_sheet("data")
    cols = {"text": p.add_column(sheet, "text")}
    p.add_rows(sheet, [{"text": t} for t in texts], cols)
    p.close()
    return sheet


def prime_cache(cache: ResponseCache, action: dict, row_texts: list[str]) -> None:
    """Cache the EXACT requests the runner will make (test_runner.py trick)."""
    params = ClassifyParams.model_validate(action["params"])
    for text in row_texts:
        call = classify_prompt(params, Row({"text": text}))
        req = LLMRequest(
            model=MODEL,
            messages=list(call.messages),
            schema=dict(call.response_schema),
            max_tokens=call.max_tokens,
        )
        cache.put(
            request_key(req, "1"),
            LLMResponse(
                content=None,
                data={"relevance": 7},
                tokens_in=50,
                tokens_out=10,
                cost=0.0001,
                model=MODEL,
            ),
        )


def replay_router(tmp_path, action=None, texts=None) -> ModelRouter:
    cache = ResponseCache(tmp_path / "c.db")
    if action is not None:
        prime_cache(cache, action, texts or [])
    return ModelRouter(keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict")


def payload(result) -> dict:
    """Structured tool output (with text-content JSON as a fallback)."""
    assert not result.isError, result.content
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def session_for(backend):
    """In-process client session — the SDK's stdio-equivalent test transport."""
    from mcp.shared.memory import create_connected_server_and_client_session

    return create_connected_server_and_client_session(
        create_mcp_server(backend), raise_exceptions=False
    )


def call(backend, tool: str, args: dict | None = None) -> dict:
    async def go():
        async with session_for(backend) as session:
            return payload(await session.call_tool(tool, args or {}))

    return asyncio.run(go())


def test_action_tool_spec_preserves_canonical_row_scope() -> None:
    payload = {
        "schema_version": "frisket.action.v2",
        "kind": "map.ner",
        "params": {"input_columns": ["text"], "labels": ["person"]},
        "row_scope": {
            "sheet_id": 7,
            "selector": {
                "kind": "exact_membership",
                "membership": {"row_ids": [5, 3, 5]},
            },
        },
    }

    action = ActionToolSpec.model_validate(payload)

    assert action.model_dump(mode="json", exclude_unset=True)["row_scope"] == {
        "sheet_id": 7,
        "selector": {
            "kind": "exact_membership",
            "membership": {"row_ids": [3, 5]},
        },
    }


@pytest.mark.parametrize("kind", ["import.files", "import.pdf"])
def test_public_tool_materializes_typed_table_with_generic_sheet_name(tmp_path, kind):
    root = tmp_path / "ws"
    seed_workspace(root, ["existing"])
    if kind == "import.pdf":
        pypdf = pytest.importorskip("pypdf")
        source = tmp_path / "document.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with source.open("wb") as stream:
            writer.write(stream)
        params = {
            "source": {"kind": "file", "path": str(source)},
            "render_pages": False,
        }
    else:
        source = tmp_path / "attachment.txt"
        source.write_text("original attachment", encoding="utf-8")
        params = {"files": [{"path": str(source)}]}
    sheet_name = f"MCP {kind}"
    result = call(
        LocalBackend(root),
        "run_action",
        {
            "project_id": "demo",
            "action": {
                "action_id": kind,
                "scope": {"kind": "project"},
                "params": params,
                "sheet_name": f"  {sheet_name}  ",
                "idempotency_key": "mcp-table-import",
            },
        },
    )
    assert result["status"] == "completed", result
    assert result["done"] is True
    project = Project(root / "demo.frisket")
    try:
        sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name=? AND hidden=0", (sheet_name,)
        ).fetchone()
        assert sheet is not None
        assert project.row_count(sheet["id"]) == 1
        receipt = ReceiptStore(project).parsed_by_id(result["receipt_id"])
        assert receipt.status == "completed"
        assert receipt.action_kind == kind
        assert receipt.outputs[0].ref["sheet_id"] == sheet["id"]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    finally:
        project.close()


@pytest.mark.parametrize("name", [None, "  New sheet  ", "", "   ", 123, True])
def test_registered_tool_sheet_name_uses_action_request_validation(name):
    request = {
        "action_id": "import.files",
        "scope": {"kind": "project"},
        "params": {},
        "sheet_name": name,
        "idempotency_key": "validation",
    }
    try:
        expected = ActionRequest.model_validate(request)
    except ValidationError:
        with pytest.raises(ValidationError):
            RegisteredActionToolSpec.model_validate(request)
    else:
        assert (
            RegisteredActionToolSpec.model_validate(request).sheet_name
            == expected.sheet_name
        )


def test_hosted_tool_routes_canonical_action_through_action_run_route() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": "frisket.action_result.v1",
                "action": {"kind": "map.classify", "action_id": "act_mcp"},
                "status": "queued",
                "project_id": "demo",
                "run_id": 42,
            },
        )

    async def go() -> dict:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        try:
            backend = HostedBackend(client=client)
            async with session_for(backend) as session:
                return payload(
                    await session.call_tool(
                        "run_action",
                        {
                            "project_id": "demo",
                            "action": classify_action(7),
                            "confirmed": True,
                            "consented_promise_set_hash": "c" * 64,
                        },
                    )
                )
        finally:
            await client.aclose()

    out = asyncio.run(go())

    assert out == {"status": "started", "run_id": 42, "done": False}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/projects/demo/actions/v1/run"
    body = json.loads(request.content)
    assert body["action_id"] == "map.classify"
    assert body["scope"] == {"kind": "sheet_rows", "sheet_id": 7}
    assert body["confirmation"] == "c" * 64
    assert body["params"] == {
        "source": ["text"],
        "engine": "llm",
        "model": MODEL,
        "context": "Test rows.",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }
    assert body["idempotency_key"].startswith("mcp.run_action.map.classify@sha256:")


def test_hosted_tool_routes_typed_action_without_legacy_translation() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": "frisket.action_result.v1",
                "action": {"kind": "map.template", "action_id": "act_typed"},
                "status": "completed",
                "project_id": "demo",
                "run_id": 17,
            },
        )

    async def go() -> dict:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        try:
            return await HostedBackend(client=client).run_action(
                "demo",
                template_action(7),
                confirmed=True,
                consented_promise_set_hash="c" * 64,
            )
        finally:
            await client.aclose()

    assert asyncio.run(go()) == {"status": "started", "run_id": 17, "done": True}
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body == {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": 7},
        "params": {"template": {"text": "{{text}}!"}},
        "output_names": {"rendered": "rendered_text"},
        "idempotency_key": body["idempotency_key"],
        "confirmation": "c" * 64,
    }
    assert body["idempotency_key"].startswith("mcp.run_action.map.template@sha256:")


def test_hosted_backend_preserves_402_contract_and_forwards_exact_echo() -> None:
    requests: list[httpx.Request] = []
    first_estimate = {
        "cost": 1.25,
        "rows": 200,
        "cost_per_row": 0.00625,
        "requires_confirmation": True,
        "billed_cost": 1_500_000,
        "policy_id": "test.hosted.cost-plus.v1",
    }
    changed_estimate = {
        "cost": 1.75,
        "rows": 280,
        "cost_per_row": 0.00625,
        "requires_confirmation": True,
        "billed_cost": 2_100_000,
        "policy_id": "test.hosted.cost-plus.v1",
    }
    first_hash = "a" * 64
    changed_hash = "b" * 64

    def gated_result(estimate: dict, promise_hash: str) -> dict:
        return {
            "schema_version": "frisket.action_result.v1",
            "action": {"kind": "map.classify", "action_id": "act_mcp"},
            "status": "needs_confirmation",
            "project_id": "demo",
            "errors": [
                {
                    "code": "model_cost_requires_confirmation",
                    "message": "confirm the exact quoted run",
                    "details": {
                        "reason": "model_cost",
                        "estimate": estimate,
                        "claims": [{"field": "cost", "display": "Paid model call."}],
                        "promise_set_hash": promise_hash,
                    },
                }
            ],
        }

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(402, json=gated_result(first_estimate, first_hash))
        if len(requests) == 2:
            return httpx.Response(
                402, json=gated_result(changed_estimate, changed_hash)
            )
        return httpx.Response(
            200,
            json={
                "schema_version": "frisket.action_result.v1",
                "action": {"kind": "map.classify", "action_id": "act_mcp"},
                "status": "queued",
                "project_id": "demo",
                "run_id": 42,
            },
        )

    async def go() -> tuple[dict, dict, dict]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        backend = HostedBackend(client=client)
        try:
            first = await backend.run_action("demo", classify_action(7))
            changed = classify_action(7, row_ids=list(range(1, 281)))
            retry = await backend.run_action(
                "demo",
                changed,
                confirmed=True,
                consented_promise_set_hash=first_hash,
            )
            accepted = await backend.run_action(
                "demo",
                changed,
                confirmed=True,
                consented_promise_set_hash=changed_hash,
            )
            return first, retry, accepted
        finally:
            await client.aclose()

    first, retry, accepted = asyncio.run(go())

    assert first["status"] == "needs_confirmation"
    assert first["estimate"] == first_estimate["billed_cost"] / 1_000_000
    assert first["estimate"] != first_estimate["cost"]
    assert first["estimate_details"] == first_estimate
    assert first["promise_set_hash"] == first_hash
    assert first["details"]["claims"] == [
        {"field": "cost", "display": "Paid model call."}
    ]
    assert retry["status"] == "needs_confirmation"
    assert retry["promise_set_hash"] == changed_hash
    assert retry["promise_set_hash"] != first_hash
    assert retry["estimate"] == changed_estimate["billed_cost"] / 1_000_000
    assert retry["estimate_details"] == changed_estimate
    assert accepted == {"status": "started", "run_id": 42, "done": False}
    assert len(requests) == 3
    retry_body = json.loads(requests[1].content)
    assert retry_body["confirmation"] == first_hash
    accepted_body = json.loads(requests[2].content)
    assert accepted_body["confirmation"] == changed_hash
    # Confirmation-flow fields are not action identity: the exact changed
    # action keeps one idempotency key across its challenge and acceptance.
    assert (
        accepted_body["idempotency_key"]
        == json.loads(requests[1].content)["idempotency_key"]
    )


def _completed_backfill_result(requested: list[int], filled: list[int]) -> dict:
    return {
        "schema_version": "frisket.action_result.v1",
        "action": {"kind": "run.backfill", "action_id": "act_bf"},
        "status": "completed",
        "project_id": "demo",
        "run_id": 7,
        "receipt_id": "rcpt_bf",
        "outputs": [
            {
                "kind": "run_backfill",
                "name": "beat",
                "sheet_id": 1,
                "column_id": 2,
                "row_ids": filled,
                "ref": {
                    "kind": "run_backfill",
                    "sheet_id": 1,
                    "column_id": 2,
                    "column_name": "beat",
                    "run_id": 7,
                    "requested_row_ids": requested,
                    "filled_row_ids": filled,
                    "filled": len(filled),
                },
            }
        ],
    }


def test_hosted_backfill_posts_v1_action_with_a_fresh_key_per_call() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completed_backfill_result([4, 5], [4, 5]))

    async def go() -> tuple[dict, dict]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        backend = HostedBackend(client=client)
        try:
            first = await backend.backfill_run("demo", 1, "beat")
            second = await backend.backfill_run("demo", 1, "beat")
            return first, second
        finally:
            await client.aclose()

    first, second = asyncio.run(go())

    assert first["status"] == "completed"
    assert first["run_id"] == 7 and first["receipt_id"] == "rcpt_bf"
    assert first["requested_row_ids"] == [4, 5] and first["filled"] == 2
    assert "not re-bought" in first["message"]
    assert second == first
    assert [r.url.path for r in requests] == [
        "/api/projects/demo/actions/v1/run",
        "/api/projects/demo/actions/v1/run",
    ]
    bodies = [json.loads(r.content) for r in requests]
    for body in bodies:
        assert body["action_id"] == "run.backfill"
        assert body["scope"] == {"kind": "sheet_rows", "sheet_id": 1}
        assert body["params"] == {"column": "beat"}
        assert body["output_names"] == {}
        assert (
            not {"schema_version", "kind", "capabilities", "confirmation"} & body.keys()
        )
        assert body["idempotency_key"].startswith("mcp.backfill_run:")
    # UNLIKE run_action's content-hash key: the same params later are a NEW
    # scoped generation (the target-row scope moved) and must never replay the first
    # invocation's receipt as a silent no-op.
    assert bodies[0]["idempotency_key"] != bodies[1]["idempotency_key"]


@pytest.mark.parametrize("row_ids", [None, [], [5, 4]])
def test_hosted_backfill_preserves_omitted_and_explicit_rows(row_ids) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if row_ids == []:
            return httpx.Response(
                400,
                json={
                    "schema_version": "frisket.action_result.v1",
                    "action": {"kind": "run.backfill", "action_id": "act_bf"},
                    "status": "failed",
                    "project_id": "demo",
                    "errors": [
                        {
                            "code": "invalid_params",
                            "message": "Explicit rows must not be empty",
                        }
                    ],
                },
            )
        return httpx.Response(200, json=_completed_backfill_result([4, 5], [4, 5]))

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://frisket.test"
        ) as client:
            return await HostedBackend(client=client).backfill_run(
                "demo", 1, "beat", row_ids
            )

    result = asyncio.run(go())
    body = json.loads(requests[0].content)
    assert body["scope"] == {
        "kind": "sheet_rows",
        "sheet_id": 1,
        **({"row_ids": row_ids} if row_ids is not None else {}),
    }
    assert body["params"] == {"column": "beat"}
    assert result["status"] == ("failed" if row_ids == [] else "completed")
    if row_ids == []:
        assert result["errors"][0]["code"] == "invalid_params"


def test_hosted_backfill_402_challenges_then_binds_the_echoed_hash() -> None:
    requests: list[httpx.Request] = []
    estimate = {
        "cost": 2.5,
        "rows": 40,
        "cost_per_row": 0.0625,
        "requires_confirmation": True,
        "billed_cost": 3_000_000,
        "policy_id": "test.hosted.cost-plus.v1",
    }
    gate_hash = "d" * 64

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                402,
                json={
                    "schema_version": "frisket.action_result.v1",
                    "action": {"kind": "run.backfill", "action_id": "act_bf"},
                    "status": "needs_confirmation",
                    "project_id": "demo",
                    "errors": [
                        {
                            "code": "model_cost_requires_confirmation",
                            "message": "confirm the exact quoted resume",
                            "details": {
                                "reason": "model_cost",
                                "estimate": estimate,
                                "promise_set_hash": gate_hash,
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json=_completed_backfill_result([4, 5], [4, 5]))

    async def go() -> tuple[dict, dict]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        backend = HostedBackend(client=client)
        try:
            challenge = await backend.backfill_run("demo", 1, "beat")
            accepted = await backend.backfill_run(
                "demo",
                1,
                "beat",
                confirmed=True,
                consented_promise_set_hash=gate_hash,
            )
            return challenge, accepted
        finally:
            await client.aclose()

    challenge, accepted = asyncio.run(go())

    assert challenge["status"] == "needs_confirmation"
    assert challenge["estimate"] == estimate["billed_cost"] / 1_000_000
    assert challenge["estimate"] != estimate["cost"]
    assert challenge["estimate_details"] == estimate
    assert challenge["promise_set_hash"] == gate_hash
    # the retry hint names THIS tool, not run_action
    assert "backfill_run" in challenge["hint"]
    assert accepted["status"] == "completed"
    confirm_request = json.loads(requests[1].content)
    assert confirm_request["params"] == {"column": "beat"}
    assert confirm_request["confirmation"] == gate_hash
    assert "confirmation" not in json.loads(requests[0].content)


def test_hosted_backfill_does_not_echo_a_quote_without_approval() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completed_backfill_result([4], [4]))

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://frisket.test"
        ) as client:
            return await HostedBackend(client=client).backfill_run(
                "demo",
                1,
                "beat",
                [4],
                confirmed=False,
                consented_promise_set_hash="not-approved",
            )

    asyncio.run(go())
    body = json.loads(requests[0].content)
    assert "confirmation" not in body
    assert body["scope"] == {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [4]}
    assert body["params"] == {"column": "beat"}


def test_hosted_backfill_named_refusal_is_structured_not_an_exception() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "schema_version": "frisket.action_result.v1",
                "action": {"kind": "run.backfill", "action_id": "act_bf"},
                "status": "failed",
                "project_id": "demo",
                "errors": [
                    {
                        "code": "mixed_origin_column_unsupported",
                        "message": (
                            "run.backfill requires rows from one source generation; "
                            "choose an exact row scope from a single generation"
                        ),
                        "field": "params.column",
                    }
                ],
            },
        )

    async def go() -> dict:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        try:
            return await HostedBackend(client=client).backfill_run(
                "demo", 1, "entities"
            )
        finally:
            await client.aclose()

    out = asyncio.run(go())

    assert out["status"] == "failed"
    assert out["errors"][0]["code"] == "mixed_origin_column_unsupported"
    assert "one source generation" in out["message"]


class TestToolSurface:
    def test_tools_with_typed_schemas(self, tmp_path):
        backend = LocalBackend(tmp_path / "ws")

        async def go():
            async with session_for(backend) as session:
                return (await session.list_tools()).tools

        tools = asyncio.run(go())
        assert {t.name for t in tools} == TOOL_NAMES
        by_name = {t.name: t for t in tools}
        rs = by_name["read_sheet"].inputSchema["properties"]
        assert rs["sheet_id"]["type"] == "integer"
        assert rs["offset"]["default"] == 0 and rs["limit"]["type"] == "integer"
        action_tool = by_name["run_action"].inputSchema
        assert action_tool["properties"]["confirmed"]["type"] == "boolean"
        echo_schema = action_tool["properties"]["consented_promise_set_hash"]
        assert {part["type"] for part in echo_schema["anyOf"]} == {
            "string",
            "null",
        }
        assert set(action_tool["required"]) == {"project_id", "action"}
        action_refs = {
            variant["$ref"].rsplit("/", 1)[-1]
            for variant in action_tool["properties"]["action"]["anyOf"]
        }
        action_schemas = [action_tool["$defs"][ref] for ref in action_refs]
        legacy_schema = next(
            schema
            for schema in action_schemas
            if "schema_version" in schema["properties"]
        )
        registered_schema = next(
            schema for schema in action_schemas if "action_id" in schema["properties"]
        )
        assert set(legacy_schema["required"]) == {
            "schema_version",
            "kind",
            "params",
        }
        assert legacy_schema["properties"]["schema_version"] == {
            "const": "frisket.action.v2",
            "title": "Schema Version",
            "type": "string",
        }
        assert set(registered_schema["required"]) == {
            "action_id",
            "scope",
            "params",
        }
        assert "idempotency_key" not in registered_schema["required"]
        assert "sheet_name" in registered_schema["properties"]
        assert "sheet_name" not in registered_schema["required"]
        assert "confirmation" not in registered_schema["properties"]
        assert registered_schema["additionalProperties"] is False
        assert '"recipe"' not in json.dumps(action_tool)
        # the cost-gate contract is part of the tool's advertised behavior
        assert "needs_confirmation" in (by_name["run_action"].description or "")
        assert "promise_set_hash" in (by_name["run_action"].description or "")
        assert 'status: "completed"' in (by_name["run_action"].description or "")
        assert "receipt_id, outputs" in (by_name["run_action"].description or "")
        # A retry is a resume: ``run_action`` must route a half-failed
        # run to the resume tool instead of a second full purchase
        assert "backfill_run" in (by_name["run_action"].description or "")
        bf = by_name["backfill_run"].inputSchema
        assert set(bf["required"]) == {"project_id", "sheet_id", "column"}
        assert bf["properties"]["confirmed"]["type"] == "boolean"
        bf_echo = bf["properties"]["consented_promise_set_hash"]
        assert {part["type"] for part in bf_echo["anyOf"]} == {"string", "null"}
        assert "needs_confirmation" in (by_name["backfill_run"].description or "")
        assert "free" in (by_name["backfill_run"].description or "")

    @pytest.mark.parametrize(
        "action",
        [
            {"kind": "map.classify", "params": {}},
            {
                "schema_version": "frisket.action.v999",
                "kind": "map.classify",
                "params": {},
            },
        ],
        ids=("missing-version", "unknown-version"),
    )
    def test_action_wire_refuses_missing_or_unknown_version_before_backend_request(
        self, action
    ) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500, json={"detail": "must not be called"})

        async def go():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://frisket.test",
            )
            try:
                async with session_for(HostedBackend(client=client)) as session:
                    return await session.call_tool(
                        "run_action",
                        {"project_id": "demo", "action": action},
                    )
            finally:
                await client.aclose()

        result = asyncio.run(go())

        assert result.isError
        assert "schema_version" in result.content[0].text
        assert requests == []

    @pytest.mark.parametrize("replace_existing", ["yes", "true", 1, 0])
    def test_registered_action_wire_refuses_coercive_replace_existing(
        self, replace_existing
    ) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500, json={"detail": "must not be called"})

        action = template_action(7)
        action["replace_existing"] = replace_existing

        async def go():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://frisket.test",
            )
            try:
                async with session_for(HostedBackend(client=client)) as session:
                    return await session.call_tool(
                        "run_action",
                        {"project_id": "demo", "action": action},
                    )
            finally:
                await client.aclose()

        result = asyncio.run(go())

        assert result.isError
        assert "replace_existing" in result.content[0].text
        assert requests == []

    def test_list_projects(self, tmp_path):
        # Order is NOT pinned here: onboard2-picker-day2-badges-v1
        # (src/frisket/server/workspace.py Workspace.list()) intentionally
        # made project order recency-first (bundle mtime, most-recent
        # first) for the ProjectPicker UI, superseding the old
        # alphabetical-by-id glob order this test originally asserted.
        # The recency-sort property itself is pinned by
        # test_project_list_sorts_by_recency
        # (tests/test_project_list_metadata.py); this MCP-surface test only
        # needs to prove list_projects exposes every seeded project by id.
        seed_workspace(tmp_path / "ws", ["a"], name="alpha")
        seed_workspace(tmp_path / "ws", ["b"], name="beta")
        out = call(LocalBackend(tmp_path / "ws"), "list_projects")
        assert {p["id"] for p in out["projects"]} == {"alpha", "beta"}

    def test_list_sheets_discovers_readable_ids_and_omits_deleted(self, tmp_path):
        sheet_id = seed_workspace(tmp_path / "ws", ["company"])
        backend = LocalBackend(tmp_path / "ws")
        project = backend.ws.get("demo")
        empty_id = project.add_sheet("empty")
        deleted_id = project.add_sheet("deleted")
        project.delete_sheet(deleted_id)

        result = call(backend, "list_sheets", {"project_id": "demo"})

        assert {(s["id"], s["name"], s["rows"]) for s in result["sheets"]} == {
            (sheet_id, "data", 1),
            (empty_id, "empty", 0),
        }
        discovered = next(s for s in result["sheets"] if s["name"] == "data")
        page = call(
            backend, "read_sheet", {"project_id": "demo", "sheet_id": discovered["id"]}
        )
        assert page["rows"][0]["cells"]["text"] == "company"

    def test_hosted_list_sheets_uses_existing_project_endpoint(self):
        sheets = [{"id": 17, "name": "companies", "rows": 3}]

        async def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/projects/demo/sheets"
            return httpx.Response(200, json=sheets)

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://frisket.test"
            ) as client:
                async with session_for(HostedBackend(client=client)) as session:
                    return payload(
                        await session.call_tool("list_sheets", {"project_id": "demo"})
                    )

        assert asyncio.run(go()) == {"sheets": sheets}


class TestReadSheet:
    def test_paginates_and_keys_cells_by_column_name(self, tmp_path):
        sheet = seed_workspace(tmp_path / "ws", [f"row {i}" for i in range(7)])
        backend = LocalBackend(tmp_path / "ws")
        page = call(
            backend,
            "read_sheet",
            {"project_id": "demo", "sheet_id": sheet, "offset": 2, "limit": 3},
        )
        assert page["total"] == 7 and page["offset"] == 2 and page["limit"] == 3
        assert [r["cells"]["text"] for r in page["rows"]] == ["row 2", "row 3", "row 4"]
        assert [c["name"] for c in page["columns"]] == ["text"]

    def test_limit_capped_server_side(self, tmp_path):
        sheet = seed_workspace(tmp_path / "ws", ["a"])
        page = call(
            LocalBackend(tmp_path / "ws"),
            "read_sheet",
            {"project_id": "demo", "sheet_id": sheet, "limit": 100_000},
        )
        assert page["limit"] == 1000  # 100k-row sheets never return wholesale

    def test_unknown_sheet_is_tool_error(self, tmp_path):
        seed_workspace(tmp_path / "ws", ["a"])

        async def go():
            async with session_for(LocalBackend(tmp_path / "ws")) as session:
                return await session.call_tool(
                    "read_sheet", {"project_id": "demo", "sheet_id": 999}
                )

        res = asyncio.run(go())
        assert res.isError and "999" in res.content[0].text


class TestSearch:
    def test_finds_seeded_row(self, tmp_path):
        seed_workspace(
            tmp_path / "ws", ["the mayor awarded a paving contract", "light rail"]
        )
        out = call(
            LocalBackend(tmp_path / "ws"),
            "search",
            {"project_id": "demo", "query": "paving"},
        )
        assert out["query"] == "paving"
        hits = out["results"]
        assert hits and all(
            {"sheet_id", "row_id", "column_name", "snip"} <= set(h) for h in hits
        )
        assert any("paving" in h["snip"] for h in hits)


class TestRunAction:
    def test_local_typed_operation_undo_returns_terminal_result(self, tmp_path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        project = Project.create(root / "demo.frisket", name="demo")
        op_id = project.append_op("fixture", label="fixture operation")
        project.close()

        result = asyncio.run(
            LocalBackend(root).run_action(
                "demo",
                {
                    "action_id": "operation.undo",
                    "scope": {"kind": "project"},
                    "params": {"expected_op_id": op_id},
                    "idempotency_key": "mcp-operation-undo@sha256:stable",
                },
            )
        )

        assert result["status"] == "completed"
        assert result["done"] is True
        assert result["receipt_id"]
        [output] = result["outputs"]
        assert output["kind"] == "operation"
        assert output["ref"]["direction"] == "undo"
        assert output["ref"]["op_id"] == op_id

    def test_local_typed_row_add_returns_terminal_result(self, tmp_path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        project = Project.create(root / "demo.frisket", name="demo")
        sheet_id = project.add_sheet("People")
        project.add_column(sheet_id, "name", type="text")
        project.close()

        result = asyncio.run(
            LocalBackend(root).run_action(
                "demo",
                {
                    "action_id": "row.add",
                    "scope": {"kind": "project"},
                    "params": {"sheet_id": sheet_id, "cells": {"name": "Ada"}},
                    "idempotency_key": "mcp-row-add@sha256:stable",
                },
            )
        )

        assert result["status"] == "completed"
        assert result["done"] is True
        assert result["receipt_id"]
        [output] = result["outputs"]
        assert output["kind"] == "rows"
        assert output["ref"]["kind"] == "added_rows"
        assert output["ref"]["row_ids"]

    def test_local_typed_source_create_returns_terminal_result(self, tmp_path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        Project.create(root / "demo.frisket", name="demo").close()
        backend = LocalBackend(root)

        result = asyncio.run(
            backend.run_action(
                "demo",
                {
                    "action_id": "source.create",
                    "scope": {"kind": "project"},
                    "params": {
                        "name": "Court feed",
                        "kind": "courtlistener.custom",
                        "config": {"query": {"court": "ca9", "precedential": True}},
                    },
                    "idempotency_key": "mcp-source-create@sha256:stable",
                },
            )
        )

        assert result["status"] == "completed"
        assert result["done"] is True
        assert result["receipt_id"]
        [output] = result["outputs"]
        assert output["kind"] == "source"
        assert output["ref"]["source_kind"] == "courtlistener.custom"
        assert output["ref"]["config"] == {
            "query": {"court": "ca9", "precedential": True}
        }
        assert output["ref"]["source_id"] > 0

    def test_local_typed_source_check_returns_terminal_result(self, tmp_path) -> None:
        from frisket.engine.store.sources import SourceStore

        root = tmp_path / "ws"
        root.mkdir()
        project = Project.create(root / "demo.frisket", name="demo")
        source_id = SourceStore(project).add_source(
            name="Court feed",
            kind="courtlistener.custom",
            config={"query": {"court": "ca9"}},
        )
        project.close()

        result = asyncio.run(
            LocalBackend(root).run_action(
                "demo",
                {
                    "action_id": "source.check",
                    "scope": {"kind": "project"},
                    "params": {
                        "source_id": source_id,
                        "new_rows": 4,
                        "status": "ok",
                        "cursor": "page-2",
                    },
                    "idempotency_key": "mcp-source-check@sha256:stable",
                },
            )
        )

        assert result["status"] == "completed"
        assert result["done"] is True
        assert result["receipt_id"]
        assert [output["kind"] for output in result["outputs"]] == [
            "source",
            "source_run",
        ]
        assert result["outputs"][1]["ref"]["new_rows"] == 4

    def test_public_tool_executes_registered_action_without_legacy_envelope(
        self, tmp_path
    ) -> None:
        root = tmp_path / "ws"
        sheet = seed_workspace(root, ["alpha", "beta"])

        result = call(
            LocalBackend(root),
            "run_action",
            {"project_id": "demo", "action": template_action(sheet)},
        )

        assert result["status"] == "started"
        assert result["done"] is True
        project = Project(root / "demo.frisket")
        try:
            output = next(
                column
                for column in project.columns(sheet)
                if column["name"] == "rendered_text"
            )
            assert list(project.get_values(sheet, output["id"]).values()) == [
                "alpha!",
                "beta!",
            ]
        finally:
            project.close()

    def test_local_typed_action_executes_through_the_canonical_registry(
        self, tmp_path
    ) -> None:
        root = tmp_path / "ws"
        sheet = seed_workspace(root, ["alpha", "beta"])
        backend = LocalBackend(root)

        result = asyncio.run(
            backend.run_action(
                "demo",
                template_action(sheet, idempotency_key="mcp-template@sha256:stable"),
            )
        )

        assert result["status"] == "started"
        assert result["done"] is True
        project = backend.ws.get("demo")
        output = next(
            column
            for column in project.columns(sheet)
            if column["name"] == "rendered_text"
        )
        assert list(project.get_values(sheet, output["id"]).values()) == [
            "alpha!",
            "beta!",
        ]

    def test_network_off_rejects_canonical_api_call_before_run_creation(
        self, tmp_path
    ) -> None:
        sheet = seed_workspace(tmp_path / "ws", ["1"])
        backend = LocalBackend(tmp_path / "ws")
        project = backend.ws.get("demo")
        project.set_network_policy(mode="off")
        action = {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "request": {
                    "method": "GET",
                    "url": "https://api.example.test/items/{{text}}",
                },
            },
            "output_names": {"api_result": "api_result"},
            "idempotency_key": "mcp-api-call-network-off@sha256:stable",
        }

        with pytest.raises(ValueError, match="network_disabled"):
            asyncio.run(backend.run_action("demo", action, confirmed=True))

        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "path",
        [None, "  "],
        ids=("missing-path", "blank-path"),
    )
    def test_canonical_columns_from_json_rejects_invalid_path_before_writes(
        self, tmp_path, path
    ) -> None:
        root = tmp_path / "ws"
        root.mkdir(parents=True)
        project = Project.create(root / "demo.frisket", name="demo")
        sheet = project.add_sheet("data")
        source_id = project.add_column(sheet, "api_result", type="json")
        project.add_rows(
            sheet,
            [{"api_result": {"city": "Paris"}}],
            {"api_result": source_id},
        )
        seeded_ops = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        project.close()
        backend = LocalBackend(root)
        route = {"name": "city"}
        if path is not None:
            route["path"] = path
        action = {
            "action_id": "map.columns_from_json",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source_column": "api_result",
                "routes": [route],
            },
            "idempotency_key": "mcp-columns-json-invalid@sha256:stable",
        }

        with pytest.raises(ValueError, match="invalid_action_request"):
            asyncio.run(backend.run_action("demo", action, confirmed=True))

        stored = backend.ws.get("demo")
        assert [column["name"] for column in stored.columns(sheet)] == ["api_result"]
        assert stored.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert stored.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == seeded_ops

    def test_local_metadata_uses_typed_action_lifecycle(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir(parents=True)
        project = Project.create(root / "demo.frisket", name="demo")
        sheet_id = project.add_sheet("assets")
        input_id = project.add_column(sheet_id, "asset", type="image")
        row_id = project.add_rows(
            sheet_id,
            [{"asset": None}],
            {"asset": input_id},
        )[0]
        project.close()
        backend = LocalBackend(root)
        spec = {
            "recipe": "extract_metadata",
            "action_kind": "media.extract_metadata",
            "sheet_id": sheet_id,
            "input_column": "asset",
            "input_columns": ["asset"],
            "row_ids": [row_id],
            "output_mode": "object",
            "output_prefix": "meta",
            "refresh": False,
        }

        with pytest.raises(
            ValueError,
            match="invalid_action_spec",
        ):
            asyncio.run(backend.run_action("demo", spec, confirmed=True))

        stored = backend.ws.get("demo")
        assert [column["name"] for column in stored.columns(sheet_id)] == ["asset"]
        assert stored.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert stored.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

        action = {
            "action_id": "media.extract_metadata",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_id],
            },
            "params": {
                "source": "asset",
                "output_mode": "object",
                "refresh": False,
            },
            "output_names": {"details": "meta"},
            "idempotency_key": "mcp-typed-metadata",
        }

        async def go():
            async with session_for(backend) as session:
                started = payload(
                    await session.call_tool(
                        "run_action", {"project_id": "demo", "action": action}
                    )
                )
                assert started["status"] == "started"
                for _ in range(400):
                    status = payload(
                        await session.call_tool(
                            "get_run_status",
                            {"project_id": "demo", "run_id": started["run_id"]},
                        )
                    )
                    if status["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("metadata run never finished")
                for _ in range(400):
                    replay = payload(
                        await session.call_tool(
                            "run_action", {"project_id": "demo", "action": action}
                        )
                    )
                    if replay["done"]:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("metadata receipt never finalized")
                assert replay["run_id"] == started["run_id"]
                assert replay["done"] is True
                return status

        status = asyncio.run(go())
        assert status["status"] == "completed"
        assert status["completed"] == status["total"] == 1
        assert status["failed"] == 0
        assert stored.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert stored.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        columns = {column["name"]: column for column in stored.columns(sheet_id)}
        assert set(columns) == {"asset", "meta"}
        assert columns["meta"]["type"] == "json"
        assert stored.get_values(sheet_id, columns["meta"]["id"]) == {row_id: None}

    def test_run_completes_and_status_reports_cost(self, tmp_path):
        texts = [f"text {i}" for i in range(5)]
        sheet = seed_workspace(tmp_path / "ws", texts)
        action = classify_action(sheet)
        backend = LocalBackend(
            tmp_path / "ws", router=replay_router(tmp_path, action, texts)
        )

        async def go():
            async with session_for(backend) as session:
                out = payload(
                    await session.call_tool(
                        "run_action", {"project_id": "demo", "action": action}
                    )
                )
                assert out["status"] == "started" and out["run_id"] >= 1
                for _ in range(400):  # poll, same loop so the run task lives
                    st = payload(
                        await session.call_tool(
                            "get_run_status",
                            {"project_id": "demo", "run_id": out["run_id"]},
                        )
                    )
                    if st["status"] != "running":
                        return st
                    await asyncio.sleep(0.01)
                raise AssertionError("run never finished")

        st = asyncio.run(go())
        assert st["status"] == "completed"
        assert st["completed"] == 5 and st["failed"] == 0 and st["total"] == 5
        assert st["cost"] == 0.0 and st["live"] is False  # cache hits bill zero

    def test_queued_run_keeps_request_preapproval_at_worker(self, tmp_path):
        sheet = seed_workspace(tmp_path / "ws", ["small paid request"])
        project = Project(tmp_path / "ws" / "demo.frisket")
        try:
            row_id = int(
                project.db.execute(
                    "SELECT id FROM rows WHERE sheet_id=?", (sheet,)
                ).fetchone()["id"]
            )
        finally:
            project.close()
        action = classify_action(sheet, row_ids=[row_id])
        adapter = _FlakyAdapter(fail_marker=None)
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        backend = LocalBackend(tmp_path / "ws", router=router)

        async def go():
            async with session_for(backend) as session:
                out = payload(
                    await session.call_tool(
                        "run_action", {"project_id": "demo", "action": action}
                    )
                )
                assert out["status"] == "started"
                for _ in range(400):
                    status = payload(
                        await session.call_tool(
                            "get_run_status",
                            {"project_id": "demo", "run_id": out["run_id"]},
                        )
                    )
                    if status["status"] != "running":
                        return status
                    await asyncio.sleep(0.01)
                raise AssertionError("run never finished")

        status = asyncio.run(go())
        assert status["status"] == "completed"
        assert adapter.calls and status["cost"] == pytest.approx(0.0001)

    def test_queued_run_refuses_quote_drift_below_preapproval(
        self, tmp_path, monkeypatch
    ):
        from frisket.ai.llm import pricing as llm_pricing

        root = tmp_path / "ws"
        sheet = seed_workspace(root, ["small paid request"])
        project = Project(root / "demo.frisket")
        try:
            row_id = int(
                project.db.execute(
                    "SELECT id FROM rows WHERE sheet_id=?", (sheet,)
                ).fetchone()["id"]
            )
        finally:
            project.close()
        action = classify_action(sheet, row_ids=[row_id])
        action["idempotency_key"] = "mcp-quote-drift@sha256:stable"
        adapter = _FlakyAdapter(fail_marker=None)
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        backend = LocalBackend(root, router=router)

        response = ActionRunService(backend.ws).run_action("demo", action)
        assert response.payload["status"] == "queued"
        run_id = response.payload["run_id"]
        stored = backend.ws.get("demo")
        consent_count = len(RouteStore.for_run(stored, run_id).consents())
        assert consent_count == 1

        pricing_key = llm_pricing.model_pricing(MODEL).pricing_key
        assert pricing_key is not None
        catalog_key = pricing_key.removeprefix("anthropic/").removesuffix(".tokens")
        original = llm_pricing.PRICES[catalog_key]
        monkeypatch.setitem(
            llm_pricing.PRICES,
            catalog_key,
            (original[0] * 3, original[1] * 3),
        )

        Worker(backend.ws.queue, backend.ws.registry).run_forever(drain=True)

        status = backend.get_run_status("demo", run_id)
        assert status["status"] == "failed"
        assert adapter.calls == []
        assert len(RouteStore.for_run(stored, run_id).consents()) == consent_count

    def test_cost_gate_is_structured_not_an_exception(self, tmp_path):
        sheet = seed_workspace(tmp_path / "ws", ["long text " * 200] * 300)
        action = classify_action(sheet, model="anthropic/claude-opus-4-8")
        backend = LocalBackend(
            tmp_path / "ws",
            router=ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off"),
        )
        out = call(backend, "run_action", {"project_id": "demo", "action": action})
        assert out["status"] == "needs_confirmation" and out["run_id"] is None
        assert out["estimate_known"] is True and out["estimate"] > 1.0
        # The message asks for confirmation in words a person reads, not the
        # name of the wire field that carries it.
        assert "confirm to run it anyway" in out["message"]
        assert "confirmed=True" not in out["message"]

    def test_changed_scope_retry_requires_the_new_402_hash(self, tmp_path):
        root = tmp_path / "ws"
        sheet = seed_workspace(root, ["long text " * 200] * 300)
        save_workspace_cost_preapproval_usd(root, "0")
        backend = LocalBackend(
            root,
            router=ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off"),
        )
        project = backend.ws.get("demo")
        row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet,)
            ).fetchall()
        ]
        first_action = classify_action(
            sheet,
            model="anthropic/claude-opus-4-8",
            row_ids=row_ids[:200],
        )

        first = call(
            backend,
            "run_action",
            {"project_id": "demo", "action": first_action},
        )
        assert first["status"] == "needs_confirmation"
        assert first["estimate"] > 1.0
        assert first["estimate_details"]["cost"] == first["estimate"]
        assert first["estimate_details"]["rows"] == 200
        first_hash = first["promise_set_hash"]
        assert first_hash == first["details"]["promise_set_hash"]

        # Expand the scope after the human saw the first challenge. The old
        # hash is not blanket consent for the larger run: this must challenge
        # again before creating a run or recording a provider call.
        changed_action = {
            **first_action,
            "scope": first_action["scope"] | {"row_ids": row_ids},
        }
        retry = call(
            backend,
            "run_action",
            {
                "project_id": "demo",
                "action": changed_action,
                "confirmed": True,
                "consented_promise_set_hash": first_hash,
            },
        )
        assert retry["status"] == "needs_confirmation"
        assert retry["promise_set_hash"] != first_hash
        assert retry["estimate_details"]["rows"] == 300
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0

    def test_unknown_price_gates_with_null_estimate(self, tmp_path):
        """estimate=None means UNKNOWN price (unpriced model) — surfaced as
        null + estimate_known=False, never zero and never an exception."""
        sheet = seed_workspace(tmp_path / "ws", ["a"])
        action = classify_action(sheet, model="anthropic/totally-unpriced-model")
        backend = LocalBackend(
            tmp_path / "ws",
            router=ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off"),
        )
        out = call(backend, "run_action", {"project_id": "demo", "action": action})
        assert out["status"] == "needs_confirmation"
        assert out["estimate"] is None and out["estimate_known"] is False
        assert "unknown" in out["message"]

    def test_local_mcp_run_action_uses_canonical_action_claim_and_fence(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        root = tmp_path / "ws"
        sheet = seed_workspace(root, ["packet ten"])
        backend = LocalBackend(root)
        action = map_python_action(
            sheet,
            idempotency_key="mcp-canonical-authority@sha256:stable",
        )
        writes: list[dict[str, object]] = []
        original_write_results = RunResultStore.write_results

        def record_authority(
            store,
            run_id,
            batch,
            **kwargs,
        ):
            writes.append(
                {
                    "run_id": run_id,
                    "writer_attempt_id": kwargs.get("writer_attempt_id"),
                    "claim_token": kwargs.get("claim_token"),
                }
            )
            return original_write_results(store, run_id, batch, **kwargs)

        monkeypatch.setattr(RunResultStore, "write_results", record_authority)

        def prepared_refs(run_id: int) -> list[dict]:
            body = json.loads(
                backend.ws.get("demo")
                .db.execute(
                    "SELECT body FROM receipts WHERE run_id=?",
                    (run_id,),
                )
                .fetchone()["body"]
            )
            return [
                evidence["ref"]
                for evidence in body["evidence"]
                if evidence["ref"].get("kind") == "queued_action_run_prepared"
            ]

        async def go() -> tuple[dict, list[dict]]:
            started = await backend.run_action("demo", action, confirmed=True)
            # The prepared marker is the QUEUED receipt's lifecycle evidence,
            # so read it while the receipt is still queued: finalization
            # replaces the body's evidence with the op's own provenance, and
            # the durable authority moves to execution_attempts /
            # output_column_claims, both asserted below.
            prepared = prepared_refs(started["run_id"])
            while backend._tasks:  # noqa: SLF001 - await the owned worker
                await asyncio.gather(*tuple(backend._tasks))  # noqa: SLF001
            return started, prepared

        started, prepared = asyncio.run(go())
        project = backend.ws.get("demo")
        receipt_row = project.db.execute(
            "SELECT * FROM receipts WHERE run_id=?",
            (started["run_id"],),
        ).fetchone()
        assert receipt_row is not None
        # One canonical project.run publication, bound to this run id.
        assert len(prepared) == 1
        assert prepared[0]["queue_kind"] == "project.run"
        assert int(prepared[0]["run_id"]) == int(started["run_id"])
        claim_token = f"output-claim:{receipt_row['id']}"
        claims = project.db.execute(
            "SELECT * FROM output_column_claims WHERE run_id=?",
            (started["run_id"],),
        ).fetchall()
        assert claims
        assert {claim["claim_token"] for claim in claims} == {claim_token}
        assert {int(claim["run_id"]) for claim in claims} == {int(started["run_id"])}
        assert {claim["status"] for claim in claims} == {"released"}
        # map.python consumes no resolution, so its writer attempt is admitted
        # at dispatch rather than at publication (see the atomic_publication
        # branch in server/action_enqueue.py); the attempt id therefore comes
        # from the write authority, and there must be exactly one of it.
        assert writes
        assert {write["claim_token"] for write in writes} == {claim_token}
        assert {int(write["run_id"]) for write in writes} == {int(started["run_id"])}
        attempt_ids = {write["writer_attempt_id"] for write in writes}
        assert len(attempt_ids) == 1
        attempt_id = attempt_ids.pop()
        assert isinstance(attempt_id, str) and attempt_id
        attempt = project.db.execute(
            "SELECT run_id, seq, state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        assert attempt is not None
        assert int(attempt["run_id"]) == int(started["run_id"])
        assert int(attempt["seq"]) == 0
        assert attempt["state"] == "effected"

    def test_local_mcp_run_action_claim_conflict_refuses_without_partial_tuple(
        self,
        tmp_path,
    ) -> None:
        root = tmp_path / "ws"
        sheet = seed_workspace(root, ["packet ten"])
        backend = LocalBackend(root)
        project = backend.ws.get("demo")
        ReceiptStore(project).insert(
            Receipt(
                receipt_id="receipt_competing_mcp",
                project_id="demo",
                action_id="action_competing_mcp",
                action_kind="map.python",
                status="running",
            )
        )
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet,
            output_names=["uppercase"],
            action_kind="map.python",
            receipt_id="receipt_competing_mcp",
            claim_token="output-claim:receipt_competing_mcp",
        )
        assert conflict is None
        assert len(claims) == 1
        baseline = {
            "receipts": project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[
                0
            ],
            "claims": project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims"
            ).fetchone()[0],
            "columns": project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[0],
            "ops": project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0],
        }
        action = map_python_action(
            sheet,
            idempotency_key="mcp-competing-authority@sha256:stable",
        )

        with pytest.raises(ValueError, match="output_column_busy"):
            asyncio.run(backend.run_action("demo", action, confirmed=True))

        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 0
        assert (
            project.db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0]
            == 0
        )
        assert {
            "receipts": project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[
                0
            ],
            "claims": project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims"
            ).fetchone()[0],
            "columns": project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[0],
            "ops": project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0],
        } == baseline
        active_claim = project.db.execute(
            "SELECT claim_token, run_id, status FROM output_column_claims"
        ).fetchone()
        assert active_claim is not None
        assert tuple(active_claim) == (
            "output-claim:receipt_competing_mcp",
            None,
            "active",
        )

    def test_local_mcp_missing_version_refuses_invalid_action_spec(
        self,
        tmp_path,
    ) -> None:
        sheet = seed_workspace(tmp_path / "ws", ["packet ten"])
        backend = LocalBackend(tmp_path / "ws")

        with pytest.raises(ValueError, match="^invalid_action_spec:"):
            asyncio.run(
                backend.run_action(
                    "demo",
                    {
                        "kind": "media.transcribe",
                        "sheet_id": sheet,
                        "input_columns": ["text"],
                    },
                    confirmed=True,
                )
            )

    def test_unknown_run_is_tool_error(self, tmp_path):
        seed_workspace(tmp_path / "ws", ["a"])

        async def go():
            async with session_for(LocalBackend(tmp_path / "ws")) as session:
                return await session.call_tool(
                    "get_run_status", {"project_id": "demo", "run_id": 12345}
                )

        res = asyncio.run(go())
        assert res.isError and "12345" in res.content[0].text


class _FlakyAdapter:
    """Counts provider calls; fails any row whose prompt carries the marker."""

    def __init__(self, fail_marker: str | None):
        self.fail_marker = fail_marker
        self.calls: list[str] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        prompt = str(req.messages)
        self.calls.append(prompt)
        if self.fail_marker and self.fail_marker in prompt:
            raise RuntimeError("provider blew up mid-run")
        return LLMResponse(
            content=None,
            data={"relevance": 7},
            tokens_in=50,
            tokens_out=10,
            cost=0.0001,
            model=req.model,
        )


class TestBackfillRun:
    def test_empty_scope_refuses_without_becoming_automatic_sweep(self, tmp_path):
        sheet = seed_workspace(tmp_path / "ws", ["first"])
        backend = LocalBackend(tmp_path / "ws")
        project = backend.ws.get("demo")

        from frisket.engine.executor import run_action_spec

        setup_action = map_python_action(
            sheet,
            idempotency_key="mcp-empty-backfill-setup@sha256:stable",
        )
        setup_action["output_names"]["value"] = "copy"
        initial = run_action_spec(
            project,
            setup_action,
            project_id="demo",
        )
        assert initial.status == "completed"
        text_column = next(
            int(column["id"])
            for column in project.columns(sheet)
            if column["name"] == "text"
        )
        [late_row] = project.add_rows(
            sheet,
            [{"text": "late"}],
            {"text": text_column},
        )

        def mutation_counts() -> dict[str, int]:
            return {
                table: int(
                    project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "ops",
                    "runs",
                    "run_scopes",
                    "run_rows",
                    "results",
                    "receipts",
                    "run_output_generations",
                    "cell_result_heads",
                )
            }

        before = mutation_counts()

        async def exercise():
            async with session_for(backend) as session:
                refused = payload(
                    await session.call_tool(
                        "backfill_run",
                        {
                            "project_id": "demo",
                            "sheet_id": sheet,
                            "column": "copy",
                            "row_ids": [],
                        },
                    )
                )
                after_refusal = mutation_counts()
                automatic = payload(
                    await session.call_tool(
                        "backfill_run",
                        {
                            "project_id": "demo",
                            "sheet_id": sheet,
                            "column": "copy",
                        },
                    )
                )
                return refused, after_refusal, automatic

        refused, after_refusal, automatic = asyncio.run(exercise())

        assert refused["status"] == "failed"
        assert refused["errors"][0]["code"] == "invalid_action_request"
        assert "row ids must not be empty" in refused["message"]
        assert after_refusal == before
        assert automatic["status"] == "completed"
        assert automatic["requested_row_ids"] == [late_row]
        assert automatic["filled_row_ids"] == [late_row]

    def test_fresh_backfill_of_half_failed_run_preserves_completed_heads(
        self, tmp_path
    ):
        """A retry is a fresh scoped generation. Three durable heads stay put;
        only the two never-completed rows run and can incur provider cost."""
        texts = ["ok zero", "ok one", "ok two", "boom three", "boom four"]
        root = tmp_path / "ws"
        sheet = seed_workspace(root, texts)
        save_workspace_cost_preapproval_usd(root, "0")
        adapter = _FlakyAdapter(fail_marker="boom")
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        backend = LocalBackend(root, router=router)
        action = classify_action(sheet)

        async def launch():
            async with session_for(backend) as session:
                # remote spend always challenges; approve the exact quote
                challenge = payload(
                    await session.call_tool(
                        "run_action", {"project_id": "demo", "action": action}
                    )
                )
                assert challenge["status"] == "needs_confirmation"
                out = payload(
                    await session.call_tool(
                        "run_action",
                        {
                            "project_id": "demo",
                            "action": action,
                            "confirmed": True,
                            "consented_promise_set_hash": challenge["promise_set_hash"],
                        },
                    )
                )
                assert out["status"] == "started"
                run_id = out["run_id"]
                for _ in range(400):
                    st = payload(
                        await session.call_tool(
                            "get_run_status",
                            {"project_id": "demo", "run_id": run_id},
                        )
                    )
                    if st["status"] != "running":
                        return run_id, st
                    await asyncio.sleep(0.01)
                raise AssertionError("run never finished")

        run_id, st = asyncio.run(launch())
        assert st["failed"] == 2 and st["total"] == 5
        assert len(adapter.calls) == 5

        page = call(backend, "read_sheet", {"project_id": "demo", "sheet_id": sheet})
        boom_rows = [
            row["id"] for row in page["rows"] if "boom" in (row["cells"]["text"] or "")
        ]
        assert len(boom_rows) == 2
        # Provider recovers; the successor buys only the never-completed rows.
        adapter.fail_marker = None
        calls_before = len(adapter.calls)

        # The scoped successor is remote spend too, so it challenges with the SAME
        # 402/echo contract as run_action — and buys nothing while gated
        gate = call(
            backend,
            "backfill_run",
            {"project_id": "demo", "sheet_id": sheet, "column": "relevance"},
        )
        assert gate["status"] == "needs_confirmation"
        assert gate["estimate_details"]["rows"] == 2  # quoted the DELTA only
        assert "backfill_run" in gate["hint"]
        assert len(adapter.calls) == calls_before

        out = call(
            backend,
            "backfill_run",
            {
                "project_id": "demo",
                "sheet_id": sheet,
                "column": "relevance",
                "confirmed": True,
                "consented_promise_set_hash": gate["promise_set_hash"],
            },
        )

        assert out["status"] == "completed"
        assert out["run_id"] != run_id
        assert sorted(out["requested_row_ids"]) == sorted(boom_rows)
        assert sorted(out["filled_row_ids"]) == sorted(boom_rows)
        assert out["filled"] == 2
        assert "not re-bought" in out["message"]
        backfill_calls = adapter.calls[calls_before:]
        assert len(backfill_calls) == 2  # the whole column was NOT re-bought
        assert all("boom" in prompt for prompt in backfill_calls)
        assert not any("ok" in prompt for prompt in backfill_calls)

        original_status = call(
            backend, "get_run_status", {"project_id": "demo", "run_id": run_id}
        )
        assert {
            key: original_status[key]
            for key in ("status", "total", "completed", "failed")
        } == {key: st[key] for key in ("status", "total", "completed", "failed")}
        successor_status = call(
            backend,
            "get_run_status",
            {"project_id": "demo", "run_id": out["run_id"]},
        )
        assert successor_status["completed"] == successor_status["total"] == 2
        assert successor_status["failed"] == 0

    def test_map_ner_backfill_uses_the_same_fresh_generation_path(
        self, tmp_path, monkeypatch
    ):
        """Universal generation publication leaves no map.ner exception."""
        monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
        monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
        sheet = seed_workspace(tmp_path / "ws", ["Ada Lovelace wrote notes."])

        class _SidecarResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {
                    "results": [
                        [
                            {
                                "text": "Ada Lovelace",
                                "label": "person",
                                "start": 0,
                                "end": 12,
                                "score": 0.98,
                            }
                        ]
                    ]
                }

        class _SidecarHttp:
            is_closed = False

            async def post(self, url, **kwargs):
                return _SidecarResponse()

        router = ModelRouter(cache=None, cache_mode="off")
        router._client = _SidecarHttp()  # noqa: SLF001
        backend = LocalBackend(tmp_path / "ws", router=router)

        from frisket.engine.executor import run_action_spec

        ner = run_action_spec(
            backend.ws.get("demo"),
            {
                "action_id": "map.ner",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {
                    "source": ["text"],
                    "labels": ["person"],
                    "threshold": 0.5,
                    "engine": "gliner",
                },
                "idempotency_key": "mcp_ner_setup@sha256:stable",
            },
            project_id="demo",
            router=router,
        )
        assert ner.status == "completed", ner.errors

        out = call(
            backend,
            "backfill_run",
            {"project_id": "demo", "sheet_id": sheet, "column": "entities"},
        )

        assert out["status"] == "completed"
        assert out["run_id"] != ner.run_id
        assert out["requested_row_ids"] == [] and out["filled"] == 0


class TestServerConstruction:
    def test_cli_dispatches_mcp_before_workspace_parse(self, monkeypatch, tmp_path):
        from frisket import cli
        from frisket.server.mcp import server

        calls = {}

        def fake_main(argv):
            calls["argv"] = argv
            return 0

        monkeypatch.setattr(server, "main", fake_main)
        monkeypatch.setattr(
            sys, "argv", ["frisket", "mcp", str(tmp_path / "ws"), "--hosted"]
        )

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert calls["argv"] == [str(tmp_path / "ws"), "--hosted"]

    def test_smoke_local_main_wiring(self, tmp_path):
        """create_mcp_server over a LocalBackend builds without a server
        process; the stdio entrypoint exists for the `frisket mcp` hookup."""
        from frisket.server.mcp import main, server

        s = create_mcp_server(LocalBackend(tmp_path / "ws"))
        assert s.name == "frisket"
        assert callable(main) and callable(server.main)
