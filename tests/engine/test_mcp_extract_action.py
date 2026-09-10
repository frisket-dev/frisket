"""Runtime boundaries for the reserved Solo ``map.mcp_extract`` action.

The selected-server collection below is an injected fake.  These tests exercise
the recipe/MapRunner contract and deliberately do not implement stdio or MCP
JSON-RPC framing, which belongs to the runtime-client boundary.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

from fastapi.testclient import TestClient

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project
from frisket.ops.base import RecipeInvocationHalt


TOOLS = [
    {
        "server_id": "crm",
        "name": "lookup_company",
        "description": "Look up one company in the local CRM.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "server_id": "taxonomy",
        "name": "risk_label",
        "description": "Resolve an internal risk label.",
        "input_schema": {
            "type": "object",
            "properties": {"company": {"type": "string"}},
            "required": ["company"],
        },
    },
]


class _SelectedSessions:
    """One run-shared fake spanning every selected server."""

    def __init__(self, tools: Iterable[dict[str, Any]] = TOOLS) -> None:
        self.selected_server_ids: tuple[str, ...] = ()
        self._tools = list(tools)
        self.entries = 0
        self.exits = 0
        self.calls: list[dict[str, Any]] = []
        self.ambiguous = False

    async def __aenter__(self):
        self.entries += 1
        return self

    async def __aexit__(self, *_exc):
        self.exits += 1

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def list_tools(self) -> list[dict[str, Any]]:
        return self.tools

    async def discover_tools(self) -> list[dict[str, Any]]:
        return self.tools

    async def call_tool(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # The recipe may call with (server, tool, arguments) or with a
        # namespaced tool plus arguments.  Preserve the observable routing
        # facts without making transport call shape part of this test.
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.ambiguous:
            raise RecipeInvocationHalt(
                "external_effect_reconciliation_required",
                "The MCP tool was dispatched but its response was lost.",
            )
        return {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": f"run-shared lookup number {len(self.calls)}",
                }
            ],
            "structuredContent": {"ordinal": len(self.calls)},
        }


class _SessionFactory:
    def __init__(self, sessions: _SelectedSessions) -> None:
        self.sessions = sessions
        self.selections: list[tuple[str, ...]] = []

    def __call__(self, server_ids: Iterable[str], *_args: Any, **_kwargs: Any):
        selected = tuple(server_ids)
        self.selections.append(selected)
        self.sessions.selected_server_ids = selected
        return self.sessions


class _TwoPhaseAdapter:
    """Tool phase -> candidate, then tool-disabled strict/repair phase."""

    def __init__(self, *, fail_second_row: bool = False) -> None:
        self.requests: list[LLMRequest] = []
        self.tool_requests = 0
        self.structured_by_row = {"alpha": 0, "beta": 0}
        self.fail_second_row = fail_second_row

    @staticmethod
    def _wire_text(req: LLMRequest) -> str:
        return json.dumps(req.messages, sort_keys=True, default=str).lower()

    async def complete(self, req: LLMRequest, _client: Any) -> LLMResponse:
        self.requests.append(req)
        text = self._wire_text(req)
        row = "beta" if "beta" in text else "alpha"
        if req.tools:
            self.tool_requests += 1
            # Each fresh row first calls the first current tool, then emits an
            # untrusted candidate for the separate structured phase.
            row_tool_turn = sum(
                1
                for prior in self.requests
                if prior.tools and row in self._wire_text(prior)
            )
            if row_tool_turn == 1:
                return _response(
                    req,
                    content=None,
                    tool_calls=[
                        {
                            "name": req.tools[0]["name"],
                            "args": {"query": row},
                            "id": f"tool-{row}",
                        }
                    ],
                )
            return _response(
                req,
                content=json.dumps({"company_name": row.title(), "risk": 7}),
            )

        assert req.schema is not None
        assert req.tools is None
        self.structured_by_row[row] += 1
        ordinal = self.structured_by_row[row]
        if ordinal == 1 or (row == "beta" and self.fail_second_row):
            # Missing required risk: never materializable.  One retry is
            # allowed, and it must remain tool-disabled.
            return _response(
                req,
                content='{"company_name": "unvalidated"}',
                data={"company_name": "unvalidated"},
            )
        return _response(
            req,
            content=json.dumps({"company_name": row.title(), "risk": 7}),
            data={"company_name": row.title(), "risk": 7},
        )


class _EndlessToolsAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, _client: Any) -> LLMResponse:
        self.requests.append(req)
        assert req.tools
        return _response(
            req,
            content=None,
            tool_calls=[
                {
                    "name": req.tools[0]["name"],
                    "args": {"query": "again"},
                    "id": f"tool-{len(self.requests)}",
                }
            ],
        )


def _response(
    req: LLMRequest,
    *,
    content: str | None,
    data: dict[str, Any] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        data=data,
        tool_calls=tool_calls,
        tokens_in=10,
        tokens_out=5,
        cost=0.001,
        model=req.model,
    )


def _router(adapter: Any) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test"}, cache=None, cache_mode="off", max_retries=0
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router


def _seed(tmp_path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "mcp-extract.frisket")
    sheet_id = project.add_sheet("Companies")
    columns = {"company": project.add_column(sheet_id, "company")}
    project.add_rows(
        sheet_id,
        [{"company": "alpha"}, {"company": "beta"}],
        columns,
    )
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    return project, sheet_id, row_ids


def _spec(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.mcp_extract",
        "idempotency_key": "mcp-extract-test",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["company"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Resolve the company and assess risk.",
            "fields": [
                {"name": "company_name", "type": "text"},
                {"name": "risk", "type": "score"},
            ],
            "mcp_server_ids": ["crm", "taxonomy"],
        },
    }


def _install_recipe(monkeypatch: pytest.MonkeyPatch, sessions: _SelectedSessions):
    factory = _SessionFactory(sessions)
    monkeypatch.setattr(
        "frisket.ops.integrations.mcp_project.ProjectMcpSessionCollection",
        lambda project, selected: factory(selected),
    )
    return factory


def _execute(project, spec, adapter):
    from frisket.engine.executor.actions import run_action_spec

    router = _router(adapter)
    result = run_action_spec(project, spec, project_id="mcp-test", router=router)
    if result.status == "needs_confirmation":
        spec = {**spec, "confirmation": result.errors[0].details["promise_set_hash"]}
        result = run_action_spec(project, spec, project_id="mcp-test", router=router)
    return result


@pytest.mark.parametrize("invalid_final", [False, True])
def test_domain_extract_uses_actual_inputs_and_keeps_both_phases_wire_facts(
    invalid_final: bool,
) -> None:
    from frisket.ai.llm.types import SchemaViolation
    from frisket.ops.mcp_extract import open_mcp_extractor

    sessions = _SelectedSessions()
    adapter = _TwoPhaseAdapter(fail_second_row=invalid_final)
    row = "beta" if invalid_final else "alpha"
    input_content = [{"type": "text", "text": row}]
    schema = {
        "type": "object",
        "properties": {
            "company_name": {"type": "string"},
            "risk": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["company_name", "risk"],
    }

    async def run():
        async with open_mcp_extractor(sessions, ("crm",)) as extractor:
            kwargs = dict(
                instruction="Apply the caller's extraction task.",
                model="anthropic/claude-haiku-4-5",
                schema=schema,
                first_output="company_name",
                recipe_version="independent-domain-caller",
                router=_router(adapter),
            )
            if invalid_final:
                with pytest.raises(SchemaViolation) as raised:
                    await extractor.extract(input_content, **kwargs)
                calls = raised.value.wire_calls
            else:
                output, calls = await extractor.extract(input_content, **kwargs)
                assert output == {"company_name": "Alpha", "risk": 7}
        with pytest.raises(RuntimeError, match="outside its execution scope"):
            await extractor.extract(input_content, **kwargs)
        return calls

    calls = asyncio.run(run())
    assert sessions.entries == sessions.exits == 1
    assert len(sessions.calls) == 1
    assert len(calls) == len(adapter.requests) == 4
    assert sum(call.cost for call in calls) == pytest.approx(0.004)
    assert sum(call.tokens_in for call in calls) == 40
    assert sum(call.tokens_out for call in calls) == 20
    assert input_content == [{"type": "text", "text": row}]
    assert all(
        [tool["name"] for tool in request.tools] == ["crm__lookup_company"]
        for request in adapter.requests
        if request.tools
    )
    assert all(request.tools is None for request in adapter.requests if request.schema)
    assert all(
        "Apply the caller's extraction task." in json.dumps(request.messages)
        for request in adapter.requests
    )


def test_domain_cancellation_closes_sessions_without_observing_or_retrying_it():
    from frisket.ops.mcp_extract import open_mcp_extractor

    class CancelledSessions(_SelectedSessions):
        async def call_tool(self, *args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            raise asyncio.CancelledError()

    sessions = CancelledSessions()
    adapter = _TwoPhaseAdapter()

    async def run():
        async with open_mcp_extractor(sessions, ("crm",)) as extractor:
            await extractor.extract(
                [{"type": "text", "text": "alpha"}],
                instruction="Extract the company.",
                model="anthropic/claude-haiku-4-5",
                schema={"type": "object"},
                first_output="company_name",
                recipe_version="cancelled-domain-caller",
                router=_router(adapter),
            )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert sessions.entries == sessions.exits == 1
    assert len(sessions.calls) == len(adapter.requests) == 1


def test_rows_are_serial_and_isolated_while_sessions_are_run_shared(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _SelectedSessions()
    factory = _install_recipe(monkeypatch, sessions)
    adapter = _TwoPhaseAdapter(fail_second_row=True)
    project, sheet_id, row_ids = _seed(tmp_path)
    try:
        result = _execute(project, _spec(sheet_id), adapter)
        assert result.run_id is not None, result
        progress = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert factory.selections == [("crm", "taxonomy")]
        assert sessions.entries == sessions.exits == 1
        assert len(sessions.calls) == 2
        # Every selected server contributes all currently discovered tools.
        first_tool_request = next(req for req in adapter.requests if req.tools)
        assert len(first_tool_request.tools or []) == len(TOOLS)

        first_turns = []
        for row in ("alpha", "beta"):
            first_turns.append(
                next(
                    req
                    for req in adapter.requests
                    if req.tools and row in adapter._wire_text(req)
                )
            )
        assert "beta" not in adapter._wire_text(first_turns[0])
        assert "alpha" not in adapter._wire_text(first_turns[1])

        # Alpha required one strict repair and committed. Beta remained
        # invalid after its sole repair; none of its partial candidate landed.
        assert adapter.structured_by_row == {"alpha": 2, "beta": 2}
        assert all(req.tools is None for req in adapter.requests if req.schema)
        assert progress["completed_rows"] == 2
        assert progress["failed_rows"] == 1
        assert progress["status"] == "completed"
        values = {
            str(row["name"]): project.get_values(
                sheet_id, int(row["id"]), row_ids=row_ids
            )
            for row in project.db.execute(
                "SELECT id, name FROM columns WHERE sheet_id=?", (sheet_id,)
            )
        }
        assert values["company_name"] == {row_ids[0]: "Alpha", row_ids[1]: None}
        assert values["risk"] == {row_ids[0]: 7, row_ids[1]: None}
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM evidence_links WHERE run_id=?", (result.run_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_agent_tool_loop_is_bounded(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = _SelectedSessions()
    _install_recipe(monkeypatch, sessions)
    adapter = _EndlessToolsAdapter()
    project, sheet_id, row_ids = _seed(tmp_path)
    try:
        spec = _spec(sheet_id)
        spec["scope"]["row_ids"] = row_ids[:1]
        result = _execute(project, spec, adapter)
        assert result.run_id is not None, result
        progress = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert progress["failed_rows"] == 1
        assert 1 <= len(adapter.requests) <= 10
        assert 1 <= len(sessions.calls) < len(adapter.requests)
    finally:
        project.close()


def test_ambiguous_tool_dispatch_is_not_automatically_retried(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _SelectedSessions()
    sessions.ambiguous = True
    _install_recipe(monkeypatch, sessions)
    adapter = _TwoPhaseAdapter()
    project, sheet_id, row_ids = _seed(tmp_path)
    try:
        spec = _spec(sheet_id)
        spec["scope"]["row_ids"] = row_ids[:1]
        result = _execute(project, spec, adapter)
        progress = project.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert result.status == "failed", result
        assert progress["halted_code"] == "external_effect_reconciliation_required"
        assert progress["completed_rows"] == 0
        assert len(sessions.calls) == 1
        checkpoint = project.db.execute(
            "SELECT state FROM effect_checkpoints WHERE family='row_effect' "
            "AND group_key=CAST(? AS TEXT)",
            (progress["id"],),
        ).fetchone()
        assert checkpoint is not None
        assert checkpoint["state"] == "reserved"
    finally:
        project.close()


def test_catalog_exposes_mcp_extract_only_in_solo(tmp_path) -> None:
    from frisket.server.app import create_app

    solo = TestClient(create_app(tmp_path / "solo", edition="solo", serve_spa=False))
    team = TestClient(create_app(tmp_path / "team", edition="team", serve_spa=False))

    solo_kinds = {
        item["kind"] for item in solo.get("/api/actions/v1/catalog").json()["actions"]
    }
    team_kinds = {
        item["kind"] for item in team.get("/api/actions/v1/catalog").json()["actions"]
    }
    assert "map.mcp_extract" in solo_kinds
    assert "map.mcp_extract" not in team_kinds
    assert "map.mcp_extract" in json.dumps(team.get("/openapi.json").json())


def test_team_refuses_crafted_mcp_extract_requests_and_stale_worker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.authoring.action_metadata import PRODUCT_EDITION_SNAPSHOT_KEY
    from frisket.engine.jobs.runs import _queued_action_edition_error
    from frisket.server.app import create_app

    team = TestClient(create_app(tmp_path / "team", edition="team", serve_spa=False))
    body = {"action_id": "map.mcp_extract", "params": {}}

    run = team.post("/api/projects/missing/actions/v1/run", json=body)
    assert run.status_code == 400
    assert run.json()["errors"][0]["code"] == "action_unavailable_in_edition"

    preview = team.post("/api/projects/missing/actions/v1/preview", json=body)
    assert preview.status_code == 400
    assert preview.json()["error"]["code"] == "action_unavailable_in_edition"

    for suffix in ("estimate", "validate-params"):
        response = team.post(
            f"/api/projects/missing/actions/v1/{suffix}",
            json={"action": body},
        )
        assert response.status_code == 400
        assert "available only in the Solo edition" in response.json()["detail"]

    stale_error = _queued_action_edition_error(
        "map.mcp_extract",
        json.dumps({PRODUCT_EDITION_SNAPSHOT_KEY: "team"}),
    )
    assert stale_error is not None
    assert stale_error.code == "action_unavailable_in_edition"

    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    solo = TestClient(create_app(tmp_path / "solo", edition="solo", serve_spa=False))
    disabled = solo.post("/api/projects/missing/actions/v1/run", json=body)
    assert disabled.status_code == 400
    assert disabled.json()["errors"][0]["code"] == "code_action_disabled"
