"""Canonical {{column}} templates."""

import asyncio

import httpx

from frisket.actions.research import WebSearchParams, web_search
from frisket.actions.types import Row, discover_references
from frisket.ai.llm import ModelRouter
from frisket.ops.base import Recipe
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
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
