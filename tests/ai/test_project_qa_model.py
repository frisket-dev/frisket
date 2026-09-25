from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.runner import ProviderSpendCapExceeded
from frisket.server.services.project_qa_runner import run_turn
from frisket.server.services.project_qa_tools import (
    MAX_OBSERVATION_CHARS,
    ProjectQAScopeError,
    ProjectQATools,
    validate_scope,
)
from frisket.team.security.secrets import encrypt_secret, key_hint


class _AskAdapter:
    """A deterministic tool agent: read, repair an unknown citation, answer."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(request)
        if len(self.requests) == 1:
            call = {
                "name": "read_rows",
                "args": {"sheet_id": 1, "row_ids": [1], "column_ids": [1]},
                "id": "read-1",
            }
        else:
            output_name = next(
                tool["name"]
                for tool in request.tools or []
                if tool["name"] not in {"inspect_sheets", "read_rows"}
            )
            citation_id = (
                "unknown-citation"
                if len(self.requests) == 2
                else self.project.db.execute(
                    "SELECT id FROM project_qa_citations ORDER BY created_at LIMIT 1"
                ).fetchone()[0]
            )
            call = {
                "name": output_name,
                "args": {
                    "text": "Only the selected cell is visible.",
                    "citation_ids": [citation_id],
                },
                "id": f"answer-{len(self.requests)}",
            }
        return LLMResponse(
            content=None,
            data=None,
            tokens_in=10,
            tokens_out=3,
            cost=0.01,
            model=request.model,
            tool_calls=[call],
        )


def _project_turn(
    tmp_path: Path, scope: dict[str, Any]
) -> tuple[Project, ProjectQAStore, dict[str, Any]]:
    project = Project.create(tmp_path / "ask-model.frisket", name="Ask model")
    sheet_id = project.add_sheet("Evidence")
    selected_column = project.add_column(sheet_id, "Selected")
    private_column = project.add_column(sheet_id, "Private")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"Selected": "visible evidence", "Private": "do not disclose"},
            {"Selected": "other evidence", "Private": "also private"},
        ],
        {"Selected": selected_column, "Private": private_column},
    )
    assert (sheet_id, selected_column, private_column, row_ids) == (1, 1, 2, [1, 2])
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask", scope=scope)
    turn = store.submit_turn(
        thread["id"],
        request_id="ask-1",
        question="What evidence is selected?",
        scope=scope,
        model="anthropic/test",
    )
    return project, store, turn


def test_file_scope_exposes_only_the_selected_cell(tmp_path: Path) -> None:
    scope = {
        "kind": "sources",
        "sources": [{"kind": "file", "sheet_id": 1, "row_id": 1, "column_id": 1}],
    }
    project, store, turn = _project_turn(tmp_path, scope)
    try:
        tools = ProjectQATools(project, turn, store)
        assert tools.inspect_sheets()["sheets"][0]["row_count"] == 1
        read = tools.read_rows(1)
        assert read["observation_truncated"] is False
        assert read["rows"][0]["row_id"] == 1
        assert read["rows"][0]["cells"][0]["value"] == "visible evidence"
        assert [
            column["name"] for column in tools.inspect_sheets()["sheets"][0]["columns"]
        ] == ["Selected"]
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.read_rows(1, row_ids=[1], column_ids=[2])
        with pytest.raises(ProjectQAScopeError, match="outside"):
            tools.read_rows(1, row_ids=[2])
    finally:
        project.close()


def test_read_observation_budget_and_scope_admission_are_bounded(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "bounded.frisket", name="Bounded")
    try:
        sheet_id = project.add_sheet("Evidence")
        column_ids = {
            f"Column {index}": project.add_column(sheet_id, f"Column {index}")
            for index in range(12)
        }
        [row_id] = project.add_rows(
            sheet_id,
            [{name: "x" * 2_000 for name in column_ids}],
            column_ids,
        )
        scope = {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": [row_id]}],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Bounded", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="bounded", question="Read", scope=scope
        )
        tools = ProjectQATools(project, turn, store)
        read = tools.read_rows(sheet_id)
        values = [cell["value"] for row in read["rows"] for cell in row["cells"]]
        assert read["observation_truncated"] is True
        assert sum(len(str(value)) for value in values) <= MAX_OBSERVATION_CHARS + 12
        with pytest.raises(ProjectQAScopeError, match="hidden or missing row"):
            validate_scope(
                project,
                {
                    "kind": "sources",
                    "sources": [
                        {"kind": "rows", "sheet_id": sheet_id, "row_ids": [999]}
                    ],
                },
            )
    finally:
        project.close()


def test_runner_combines_read_and_typed_output_repairs_citations_and_accounts(
    tmp_path: Path,
) -> None:
    scope = {
        "kind": "sources",
        "sources": [{"kind": "rows", "sheet_id": 1, "row_ids": [1]}],
    }
    project, store, turn = _project_turn(tmp_path, scope)
    try:
        router = ModelRouter(
            keys={"anthropic": "test-key"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        adapter = _AskAdapter(project)
        router._adapters["anthropic"] = adapter  # noqa: SLF001

        answer = asyncio.run(run_turn(project, router, turn, store))

        assert answer["text"] == "Only the selected cell is visible."
        assert answer["citation_ids"] != ["unknown-citation"]
        assert len(adapter.requests) == 3
        assert all(request.schema is None for request in adapter.requests)
        assert all(
            {"inspect_sheets", "read_rows"}
            <= {tool["name"] for tool in request.tools or []}
            for request in adapter.requests
        )
        observations = [
            str(message["content"])
            for request in adapter.requests[1:]
            for message in request.messages
            if message["role"] == "user"
        ]
        assert "visible evidence" in "\n".join(observations)
        assert "do not disclose" not in "\n".join(observations)

        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 3
        usage = [
            event
            for event in store.events(turn["thread_id"])["events"]
            if event["kind"] == "usage"
        ]
        assert len(usage) == 3
        assert len({event["payload"]["call_id"] for event in usage}) == 3
        assert any(
            event["kind"] == "tool_started" and event["payload"]["tool"] == "read_rows"
            for event in store.events(turn["thread_id"])["events"]
        )

        completed = store.finish_turn(turn["id"], status="completed")
        assert completed["usage"] == {"calls": 3, "tokens_in": 30, "tokens_out": 9}
        assert completed["cost_actual"] == pytest.approx(0.03)
    finally:
        project.close()


def test_runner_checks_project_key_spend_before_each_provider_request(
    tmp_path: Path,
) -> None:
    scope = {
        "kind": "sources",
        "sources": [{"kind": "rows", "sheet_id": 1, "row_ids": [1]}],
    }
    project, store, turn = _project_turn(tmp_path, scope)
    try:
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("test-project-key"),
            hint=key_hint("test-project-key"),
            spend_cap_micro=0,
        )
        router = ModelRouter(
            keys={"anthropic": "test-project-key"},
            key_sources={"anthropic": "project_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        adapter = _AskAdapter(project)
        router._adapters["anthropic"] = adapter  # noqa: SLF001

        with pytest.raises(ProviderSpendCapExceeded):
            asyncio.run(run_turn(project, router, turn, store))
        assert adapter.requests == []
    finally:
        project.close()
