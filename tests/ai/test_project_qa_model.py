from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from datetime import date

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.structured import FrisketRouterModel
from frisket.ai.llm.types import LLMRequest, LLMResponse, SchemaViolation
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.runner import ProviderSpendCapExceeded
from frisket.querysets import anchor_relative_date_filters
from frisket.server.services.project_qa_runner import run_turn
from frisket.server.services.project_qa_query import evaluate_query
from frisket.server.services.project_qa_tools import (
    MAX_OBSERVATION_CHARS,
    ProjectQAScopeError,
    ProjectQATools,
    validate_scope,
)
from frisket.team.security.secrets import encrypt_secret, key_hint


class _AskAdapter:
    """A deterministic tool agent: read, repair an unknown citation, answer."""

    def __init__(
        self, project: Project, *, repeats_unknown_citation: bool = False
    ) -> None:
        self.project = project
        self.repeats_unknown_citation = repeats_unknown_citation
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
                if tool["name"]
                not in {
                    "inspect_sheets",
                    "read_rows",
                    "query_rows",
                    "search_cells",
                    "open_source",
                    "describe_action",
                    "propose_action",
                }
            )
            citation_id = (
                "unknown-citation"
                if len(self.requests) == 2 or self.repeats_unknown_citation
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


class _SchemaFaultRouter:
    def __init__(self, receipt: LLMResponse) -> None:
        self.receipt = receipt

    async def complete_transport(self, *args: Any, **kwargs: Any) -> LLMResponse:
        raise SchemaViolation("invalid provider output", wire_calls=[self.receipt])


class _TypedOutput(BaseModel):
    text: str


def test_schema_only_output_keeps_forced_output_mapping_over_tool_metadata() -> None:
    model = FrisketRouterModel(
        ModelRouter(cache=None, cache_mode="off", use_env_keys=False), "anthropic/test"
    )
    response = model._to_model_response(  # noqa: SLF001 - regression at shim seam
        LLMResponse(
            content=None,
            data={"text": "typed"},
            tokens_in=1,
            tokens_out=1,
            cost=0.0,
            model="anthropic/test",
            tool_calls=[{"name": "read_rows", "args": {}, "id": "read-1"}],
        ),
        SimpleNamespace(output_tools=[SimpleNamespace(name="emit")]),
        "tool",
    )
    [part] = response.parts
    assert part.tool_name == "emit"
    assert part.args_as_dict() == {"text": "typed"}


def test_schema_failure_accounts_attached_provider_receipt_once() -> None:
    receipt = LLMResponse(
        content="not typed output",
        data=None,
        tokens_in=11,
        tokens_out=4,
        cost=0.01,
        model="anthropic/test",
    )
    accounted: list[LLMResponse] = []

    async def on_response(response: LLMResponse) -> None:
        accounted.append(response)

    model = FrisketRouterModel(
        _SchemaFaultRouter(receipt),  # type: ignore[arg-type]
        "anthropic/test",
        on_response=on_response,
    )
    agent = Agent(model, output_type=_TypedOutput, retries=0)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(agent.run("Return typed output"))
    assert model.wire_calls == [receipt]
    assert model.attempts == 1
    assert accounted == [receipt]


def test_relative_query_dates_are_saved_as_explicit_bounds() -> None:
    assert anchor_relative_date_filters(
        {"Published": {"date_relative": {"amount": 7, "unit": "days"}}},
        reference_date=date(2026, 9, 25),
    ) == {"Published": {"between": {"start": "2026-09-19", "end": "2026-09-25"}}}


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
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.read_rows(1, row_ids=[1], column_ids=[1, 2])
        with pytest.raises(ProjectQAScopeError, match="outside"):
            tools.read_rows(1, row_ids=[2])
        with pytest.raises(ProjectQAScopeError, match="outside"):
            tools.read_rows(999)
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


def test_scoped_query_count_search_and_open_source_do_not_widen_rows(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "scoped-tools.frisket", name="Scoped tools")
    try:
        sheet_id = project.add_sheet("Evidence")
        columns = {
            "Text": project.add_column(sheet_id, "Text"),
            "Group": project.add_column(sheet_id, "Group"),
            "Private": project.add_column(sheet_id, "Private"),
        }
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "Text": "visible alpha " + "x" * 9_000,
                    "Group": "A",
                    "Private": "private alpha",
                },
                {
                    "Text": "visible beta",
                    "Group": "B",
                    "Private": "private beta",
                },
                {
                    "Text": "visible gamma",
                    "Group": "C",
                    "Private": "private gamma",
                },
            ],
            columns,
        )
        scope = {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": row_ids[:2]}],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Scoped", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="scoped", question="Read", scope=scope
        )
        tools = ProjectQATools(project, turn, store)
        query = {
            "schema_version": "frisket.query.v1",
            "kind": "sheet.filter",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "filter": {"Text": {"contains": "visible"}},
        }
        result = tools.query_rows(query, limit=0, count_by=columns["Group"])
        assert result["total"] == 2
        assert result["row_ids"] == []
        assert result["scope"]["row_ids"] == row_ids[:2]
        assert result["count_by"] == {
            "column_id": columns["Group"],
            "values": [
                {"value": '"A"', "count": 1},
                {"value": '"B"', "count": 1},
            ],
            "complete": True,
        }
        assert result["query_hash"]
        replayed = evaluate_query(project, result["query"], result["scope"], limit=0)
        assert replayed["total"] == result["total"]
        assert replayed["scope"] == result["scope"]
        search = tools.search_cells("visible", sheet_id)
        assert {hit["row_id"] for hit in search["hits"]} == set(row_ids[:2])
        assert {hit["column_id"] for hit in search["hits"]} == {columns["Text"]}
        read = tools.read_rows(sheet_id, [row_ids[0]], [columns["Text"]])
        opened = tools.open_source(read["rows"][0]["cells"][0]["citation_id"])
        assert len(opened["passages"][0]["text"]) > 2_000
        assert opened["truncated"] is True
        proposal = tools.propose_action(
            "map",
            "Copy text",
            {
                "action_kind": "map.template",
                "sheet_id": sheet_id,
                "template": {"text": "{{Text}}"},
            },
        )["proposal"]
        assert proposal["spec"]["scope"]["row_ids"] == row_ids[:2]
        assert store.events(thread["id"])["events"][-1]["kind"] == "action_proposal"
    finally:
        project.close()


def test_file_scope_query_and_search_reject_unselected_columns(tmp_path: Path) -> None:
    scope = {
        "kind": "sources",
        "sources": [{"kind": "file", "sheet_id": 1, "row_id": 1, "column_id": 1}],
    }
    project, store, turn = _project_turn(tmp_path, scope)
    try:
        tools = ProjectQATools(project, turn, store)
        query = {
            "schema_version": "frisket.query.v1",
            "kind": "sheet.filter",
            "scope": {"kind": "sheet", "sheet_id": 1},
            "filter": {"Private": {"contains": "disclose"}},
        }
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.query_rows(query)
        assert tools.search_cells("visible", 1)["hits"]
        assert not tools.search_cells("disclose", 1)["hits"]
        nonrect_scope = {
            "kind": "sources",
            "sources": [
                {"kind": "file", "sheet_id": 1, "row_id": 1, "column_id": 1},
                {"kind": "file", "sheet_id": 1, "row_id": 2, "column_id": 2},
            ],
        }
        thread = store.create_thread(title="Nonrect", scope=nonrect_scope)
        nonrect_turn = store.submit_turn(
            thread["id"],
            request_id="nonrect",
            question="Query",
            scope=nonrect_scope,
        )
        with pytest.raises(ProjectQAScopeError, match="same selected file column"):
            ProjectQATools(project, nonrect_turn, store).query_rows(
                {
                    "schema_version": "frisket.query.v1",
                    "kind": "sheet.filter",
                    "scope": {"kind": "sheet", "sheet_id": 1},
                    "filter": {},
                }
            )
    finally:
        project.close()


def test_file_search_filters_exact_cells_before_ranking(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "file-search.frisket", name="File search")
    try:
        sheet_id = project.add_sheet("Evidence")
        selected = project.add_column(sheet_id, "Selected")
        private = project.add_column(sheet_id, "Private")
        [row_id] = project.add_rows(
            sheet_id,
            [{"Selected": "needle", "Private": "needle " * 100}],
            {"Selected": selected, "Private": private},
        )
        scope = {
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet_id,
                    "row_id": row_id,
                    "column_id": selected,
                }
            ],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="File search", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="file-search", question="Find", scope=scope
        )
        hits = ProjectQATools(project, turn, store).search_cells(
            "needle", sheet_id, limit=1
        )["hits"]
        assert hits and hits[0]["column_id"] == selected
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
            {"inspect_sheets", "read_rows", "query_rows", "search_cells"}
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


def test_runner_refuses_an_answer_that_repeats_an_unknown_citation(
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
        adapter = _AskAdapter(project, repeats_unknown_citation=True)
        router._adapters["anthropic"] = adapter  # noqa: SLF001

        with pytest.raises(UnexpectedModelBehavior):
            asyncio.run(run_turn(project, router, turn, store))
        assert not [
            event
            for event in store.events(turn["thread_id"])["events"]
            if event["kind"] == "answer"
        ]
    finally:
        project.close()


def test_runner_does_not_register_action_tools_when_suggestions_are_off(
    tmp_path: Path,
) -> None:
    scope = {
        "kind": "sources",
        "sources": [{"kind": "rows", "sheet_id": 1, "row_ids": [1]}],
    }
    project, store, turn = _project_turn(tmp_path, scope)
    try:
        turn["suggest_actions"] = False
        router = ModelRouter(
            keys={"anthropic": "test-key"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        adapter = _AskAdapter(project)
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        asyncio.run(run_turn(project, router, turn, store))
        names = {tool["name"] for tool in adapter.requests[0].tools or []}
        assert "open_source" in names
        assert {"describe_action", "propose_action"}.isdisjoint(names)
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
