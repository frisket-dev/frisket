"""Canonical {{column}} templates."""

import asyncio

import httpx

from frisket.actions.research import WebSearchParams, web_search
from frisket.actions.types import Row, discover_references
from frisket.ai.llm import ModelRouter
from frisket.ops.base import Recipe
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.project import ProjectReadSnapshot
import frisket.engine.runner.map_runner as map_runner_module
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.authoring.templates import render_column_template


def _project(tmp_path):
    return Project.create(tmp_path / "templates.frisket", name="templates")


def _seed(project: Project, rows: list[dict]):
    sheet = project.add_sheet("data")
    cols = {name: project.add_column(sheet, name) for name in rows[0]}
    project.add_rows(sheet, rows, cols)
    return sheet


def test_template_uses_shared_local_batches_with_receipt_reopen_and_undo(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_ROWS", 3)
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_SECONDS", 1000)
    batches = []
    publish = RunResultStore.publish_result_batch

    def record(store, run_id, batch, **kwargs):
        publish(store, run_id, batch, **kwargs)
        batches.append(len(batch))

    monkeypatch.setattr(RunResultStore, "publish_result_batch", record)
    pages = []
    read = ProjectReadSnapshot.get_values_with_refs

    def record_read(snapshot, sheet_id, column_id, row_ids=None, **kwargs):
        pages.append((column_id, tuple(row_ids)))
        return read(snapshot, sheet_id, column_id, row_ids=row_ids, **kwargs)

    monkeypatch.setattr(ProjectReadSnapshot, "get_values_with_refs", record_read)
    project = _project(tmp_path)
    path = project.path
    try:
        sheet = _seed(project, [{"name": f"row {i}", "suffix": "!"} for i in range(8)])
        result = run_action_spec(
            project,
            {
                "action_id": "map.template",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"template": {"text": "{{name}} | {{suffix}}"}},
                "output_names": {"rendered": "rendered"},
                "idempotency_key": "batched-template",
            },
            project_id="templates",
        )
        assert result.status == "completed", result.errors
        assert batches == [3, 3, 2]
        assert [len(ids) for _column, ids in pages] == [3, 3, 3, 3, 2, 2]
        column = next(
            c["id"] for c in project.columns(sheet) if c["name"] == "rendered"
        )
        assert list(project.get_values(sheet, column).values()) == [
            f"row {i} | !" for i in range(8)
        ]
        assert (
            project.db.execute(
                "SELECT count(*) FROM run_rows WHERE run_id=?", (result.run_id,)
            ).fetchone()[0]
            == 8
        )
        assert (
            project.db.execute(
                "SELECT count(*) FROM execution_attempts WHERE run_id=?",
                (result.run_id,),
            ).fetchone()[0]
            == 1
        )
        assert result.receipt_id
        assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
    finally:
        project.close()
    reopened = Project(path)
    try:
        assert len(reopened.get_values(sheet, column)) == 8
        reopened.undo()
        assert all(c["id"] != column for c in reopened.columns(sheet))
        assert (
            reopened.db.execute(
                "SELECT count(*) FROM results WHERE run_id=?", (result.run_id,)
            ).fetchone()[0]
            == 8
        )
    finally:
        reopened.close()


def test_typed_runner_template_infers_columns_without_input_columns(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["q"] = request.url.params.get("q", "")
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "48.8584",
                    "lon": "2.2945",
                    "display_name": "Paris, France",
                }
            ],
        )

    p = _project(tmp_path)
    sheet = _seed(
        p,
        [{"address": "Tour Eiffel", "city": "Paris", "country": "France"}],
    )
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    deps = ExecutorDeps(
        execution_composition=open_execution_composition(
            p, router, ExecutionCompositionContext.direct()
        )
    )
    request = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": {"text": "{{address}}, {{city}}, {{country}}"}},
        "idempotency_key": "template-inferred-columns",
    }
    try:
        result = run_action_spec(
            p, request, project_id="templates", router=router, deps=deps
        )
        assert result.status == "completed", result.errors
        assert seen["q"] == "Tour Eiffel, Paris, France"
    finally:
        p.close()


def test_template_renderer_does_not_rerender_inserted_values():
    rendered = render_column_template(
        "{{headline}} {{name_full}} {{name}}",
        {
            "headline": "literal {{name}} should stay literal",
            "name": "Ada",
            "name_full": "Ada Lovelace",
        },
    )
    assert rendered == "literal {{name}} should stay literal Ada Lovelace Ada"


def test_source_column_discovery_for_recipe_templates_and_search():
    assert Recipe().source_columns(
        {"input_columns": ["a"], "input_template": "{{b}} {{ a }}"}
    ) == ["a", "b"]
    assert [
        ref.column
        for ref in discover_references(
            WebSearchParams(query={"text": "{{city}} {{topic}}"})
        )
    ] == ["city", "topic"]


def test_web_search_execute_renders_query_without_network(monkeypatch):
    from frisket.actions.research_types import SearchResult

    seen: dict[str, object] = {}

    class Searcher:
        async def search(self, query: str, *, max_results: int):
            seen["query"] = query
            seen["max_results"] = max_results
            return [
                SearchResult(
                    title="Paris tariffs",
                    url="https://example.com/paris",
                    snippet="Result snippet",
                )
            ]

    result = asyncio.run(
        web_search(
            WebSearchParams(query={"text": "{{city}} {{topic}}"}, max_results=3),
            Row({"city": "Paris", "topic": "tariffs"}),
            Searcher(),
        )
    )

    assert seen == {"query": "Paris tariffs", "max_results": 3}
    assert result.model_dump(mode="json") == {
        "output": {
            "search_results": [
                {
                    "title": "Paris tariffs",
                    "url": "https://example.com/paris",
                    "snippet": "Result snippet",
                }
            ]
        }
    }
