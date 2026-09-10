"""Public runtime contract for persisted local MCP Extract servers.

The recipe registry, saved project settings, stdio fixture, and MapRunner are
all real here.  Only the model transport is faked, so this remains a bounded
contract for the missing settings-to-runtime bridge rather than a duplicate of
the transport-client test suite.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project
from frisket.project_settings import write_project_mcp_servers
from frisket.team.security.secrets import encrypt_secret
from executor_harness import run_action_with_confirmation


FIXTURE_SERVER = (
    Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"
)
_SECRET = "bridge-secret-plaintext"


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wait_for_process_stop(pid: int) -> None:
    deadline = time.monotonic() + 2  # realtime: bound real MCP process teardown
    while time.monotonic() < deadline:  # realtime: poll real OS process state
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)  # realtime: yield while real OS process exits
    raise AssertionError(f"MCP fixture process {pid} remained alive")


def _server(
    tmp_path: Path,
    *,
    server_id: str,
    events: Path,
    enabled: bool = True,
    command: str | None = None,
    marker: str = "persisted-current-definition",
    scenario: str = "normal",
) -> dict[str, Any]:
    return {
        "id": server_id,
        "name": server_id,
        # subprocess-boundary: MCP stdio launch and teardown are the behavior under test
        "command": command or sys.executable,
        "args": [
            str(FIXTURE_SERVER),
            "--scenario",
            scenario,
            "--events",
            str(events),
            "--argv-marker",
            marker,
        ],
        "cwd": str(tmp_path),
        "env": {
            "MCP_EXPLICIT": {"value": "literal-from-settings"},
            "MCP_SECRET": {"project_secret": "MCP_TOKEN"},
        },
        "enabled": enabled,
        "revision": 1,
        "lastTest": None,
    }


def _seed_project(tmp_path: Path) -> tuple[Project, int, int]:
    project = Project.create(tmp_path / "mcp-runtime-bridge.frisket")
    sheet_id = project.add_sheet("Rows")
    source_column = project.add_column(sheet_id, "source")
    project.add_rows(sheet_id, [{"source": "one input row"}], {"source": source_column})
    row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()["id"]
    )
    project.set_secret(
        name="MCP_TOKEN",
        encrypted=encrypt_secret(_SECRET),
        hint="bri***ext",
    )
    return project, sheet_id, row_id


def _spec(sheet_id: int, server_ids: list[str], row_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.mcp_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
        "idempotency_key": "mcp-bridge-run",
        "output_names": {"checked": "checked"},
        "params": {
            "source": ["source"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Return the checked value.",
            "fields": [{"name": "checked", "type": "text"}],
            "mcp_server_ids": server_ids,
        },
    }


class _ToolThenStrictAdapter:
    """One model tool call, followed by the recipe's tool-free strict pass."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self._tool_turns = 0

    async def complete(self, request: LLMRequest, _client: Any) -> LLMResponse:
        self.requests.append(request)
        if request.tools:
            self._tool_turns += 1
            if self._tool_turns == 1:
                tool_name = next(
                    tool["name"]
                    for tool in request.tools
                    if tool["name"].endswith("__inspect_launch")
                )
                return _response(
                    request,
                    content=None,
                    tool_calls=[
                        {
                            "name": tool_name,
                            "args": {"value": "one input row"},
                            "id": "fixture-tool-call",
                        }
                    ],
                )
            return _response(request, content='{"checked":"tool candidate"}')

        assert request.schema is not None
        return _response(
            request,
            content='{"checked":"strict output"}',
            data={"checked": "strict output"},
        )


def _response(
    request: LLMRequest,
    *,
    content: str | None,
    data: dict[str, Any] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        data=data,
        tool_calls=tool_calls,
        tokens_in=7,
        tokens_out=3,
        cost=0.001,
        model=request.model,
    )


def _router(adapter: _ToolThenStrictAdapter) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test"}, cache=None, cache_mode="off", max_retries=0
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001 - test transport seam
    return router


def test_registered_recipe_runs_saved_stdio_server_and_keeps_secret_out_of_run_data(
    tmp_path: Path,
) -> None:
    """The default registered recipe launches current persisted config itself."""
    project, sheet_id, row_id = _seed_project(tmp_path)
    events = tmp_path / "good.events.jsonl"
    server_id = "fixture/server"
    write_project_mcp_servers(
        project,
        [
            _server(
                tmp_path,
                server_id=server_id,
                events=events,
                marker="stale-definition-must-not-launch",
            ),
            _server(
                tmp_path,
                server_id="enabled-but-unselected",
                events=tmp_path / "unselected.events.jsonl",
            ),
        ],
    )
    plaintext_reads: list[str] = []
    original_secret_plaintext = project.secret_plaintext

    def observe_secret_plaintext(name: str) -> str | None:
        plaintext_reads.append(name)
        return original_secret_plaintext(name)

    project.secret_plaintext = observe_secret_plaintext  # type: ignore[method-assign]
    adapter = _ToolThenStrictAdapter()
    spec = _spec(sheet_id, [server_id], row_id)
    from frisket.actions.registry import ACTION_REGISTRY

    ACTION_REGISTRY.get("map.mcp_extract")
    try:
        # Constructing/selecting the recipe is not a credential read.  The
        # child-bound plaintext is resolved only when the run opens its scope.
        assert plaintext_reads == []
        # The current saved record changes after recipe lookup.  The runtime
        # must resolve it as the execution scope opens, rather than retaining
        # an earlier settings snapshot.
        write_project_mcp_servers(
            project,
            [
                _server(tmp_path, server_id=server_id, events=events),
                _server(
                    tmp_path,
                    server_id="enabled-but-unselected",
                    events=tmp_path / "unselected.events.jsonl",
                ),
            ],
        )

        progress = run_action_with_confirmation(
            project, spec, router=_router(adapter), project_id="mcp-bridge"
        )

        assert progress.status == "completed", progress.errors
        assert plaintext_reads == ["MCP_TOKEN"]
        output = next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == "checked"
        )
        assert project.get_values(sheet_id, int(output["id"])) == {
            row_id: "strict output"
        }

        started = [event for event in _events(events) if event["event"] == "started"]
        assert len(started) == 1
        assert started[0]["argv_marker"] == "persisted-current-definition"
        methods = [
            event["method"] for event in _events(events) if event["event"] == "request"
        ]
        assert methods == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/list",
            "tools/call",
        ]
        _wait_for_process_stop(int(started[0]["pid"]))

        tool_request = next(request for request in adapter.requests if request.tools)
        # A persisted ID containing '/' cannot collide with a bare MCP tool
        # name in the model namespace.
        assert [tool["name"] for tool in tool_request.tools or []] == [
            "fixture_server__inspect_launch",
            "fixture_server__mixed_result",
        ]
        model_context = json.dumps(
            [
                {"messages": request.messages, "tools": request.tools}
                for request in adapter.requests
            ],
            default=str,
        )
        assert "explicit_env" in model_context
        assert "literal-from-settings" not in model_context
        assert _SECRET not in model_context
        assert any(
            request.schema is not None and request.tools is None
            for request in adapter.requests
        )

        durable = json.dumps(
            {
                "runner_spec": spec,
                "run": dict(
                    project.db.execute(
                        "SELECT * FROM runs WHERE id=?", (progress.run_id,)
                    ).fetchone()
                ),
                "model_calls": [
                    dict(row)
                    for row in project.db.execute(
                        "SELECT * FROM model_calls WHERE run_id=?", (progress.run_id,)
                    )
                ],
                "result": project.get_values(sheet_id, int(output["id"])),
                "diagnostic": [error.model_dump() for error in progress.errors],
            },
            default=str,
        )
        assert _SECRET not in durable
    finally:
        project.close()


@pytest.mark.parametrize("selected_id", ["disabled", "missing"])
def test_disabled_or_missing_saved_server_halts_before_any_row_is_run(
    tmp_path: Path, selected_id: str
) -> None:
    project, sheet_id, row_id = _seed_project(tmp_path)
    events = tmp_path / "disabled.events.jsonl"
    write_project_mcp_servers(
        project,
        [_server(tmp_path, server_id="disabled", events=events, enabled=False)],
    )
    try:
        adapter = _ToolThenStrictAdapter()
        result = run_action_with_confirmation(
            project,
            _spec(sheet_id, [selected_id], row_id),
            router=_router(adapter),
            project_id="mcp-bridge",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "local_artifact_unavailable"
        assert adapter.requests == []
        assert _events(events) == []
    finally:
        project.close()


def test_one_failed_saved_server_does_not_hide_tools_from_another(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_id = _seed_project(tmp_path)
    good_events = tmp_path / "survivor.events.jsonl"
    good_id = "survivor/server"
    write_project_mcp_servers(
        project,
        [
            _server(tmp_path, server_id=good_id, events=good_events),
            _server(
                tmp_path,
                server_id="unavailable/server",
                events=tmp_path / "unavailable.events.jsonl",
                command="definitely-not-an-mcp-command",
            ),
        ],
    )
    adapter = _ToolThenStrictAdapter()
    try:
        progress = run_action_with_confirmation(
            project,
            _spec(sheet_id, ["unavailable/server", good_id], row_id),
            router=_router(adapter),
            project_id="mcp-bridge",
        )
        assert progress.status == "completed", progress.errors
        assert [event["event"] for event in _events(good_events)].count("started") == 1
        assert any(request.tools for request in adapter.requests)
    finally:
        project.close()


def test_tool_inventory_echoing_a_configured_secret_is_rejected_before_model_use(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_id = _seed_project(tmp_path)
    events = tmp_path / "secret-inventory.events.jsonl"
    server_id = "secret-inventory/server"
    write_project_mcp_servers(
        project,
        [
            _server(
                tmp_path,
                server_id=server_id,
                events=events,
                scenario="secret_inventory",
            )
        ],
    )
    adapter = _ToolThenStrictAdapter()
    try:
        progress = run_action_with_confirmation(
            project,
            _spec(sheet_id, [server_id], row_id),
            router=_router(adapter),
            project_id="mcp-bridge",
        )
        assert progress.status == "failed"
        assert progress.errors[0].code == "local_artifact_unavailable"
        assert adapter.requests == []
        started = [event for event in _events(events) if event["event"] == "started"]
        assert len(started) == 1
        _wait_for_process_stop(int(started[0]["pid"]))
    finally:
        project.close()


def test_lost_saved_server_tool_response_halts_without_automatic_retry(
    tmp_path: Path,
) -> None:
    """A post-dispatch stdio failure is a run halt, not an agent observation."""
    project, sheet_id, row_id = _seed_project(tmp_path)
    events = tmp_path / "lost-response.events.jsonl"
    server_id = "hanging/server"
    write_project_mcp_servers(
        project,
        [
            _server(
                tmp_path,
                server_id=server_id,
                events=events,
                scenario="malformed_call",
            )
        ],
    )
    try:
        progress = run_action_with_confirmation(
            project,
            _spec(sheet_id, [server_id], row_id),
            router=_router(_ToolThenStrictAdapter()),
            project_id="mcp-bridge",
        )
        assert progress.status == "failed"
        assert progress.errors[0].code == "external_effect_reconciliation_required"
        assert [
            event["method"]
            for event in _events(events)
            if event["event"] == "request" and event["method"] == "tools/call"
        ] == ["tools/call"]
    finally:
        project.close()
