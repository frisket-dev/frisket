from __future__ import annotations

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.research_types import Researcher, SearchResult
from frisket.actions.types import ActionParams, ModelRef, Row, RowResult, Template


class WebSearchParams(ActionParams):
    query: Template[str]
    model: ModelRef
    max_results: int = Field(default=10, ge=1, le=20)


class WebSearchOutput(BaseModel):
    answer: str | None
    search_results: list[SearchResult]


async def web_search(
    params: WebSearchParams,
    row: Row,
    researcher: Researcher,
) -> RowResult[WebSearchOutput]:
    query = params.query.render(row)
    if not query.strip():
        raise ValueError("empty search query after substitution")
    result = await researcher.quick_search(
        row, query=query, max_results=params.max_results
    )
    return RowResult(
        output=WebSearchOutput(
            answer=result.answer, search_results=result.search_results
        )
    )


WEB_SEARCH = action(
    examples=(
        WebSearchParams(
            query=Template[str](text="{{source}} annual report"),
            model="anthropic/claude-haiku-4-5",
            max_results=5,
        ),
    ),
    name="web_search",
    title="Quick search",
    description="Search once per row and answer from the result snippets, keeping the search results.",
    category=ActionCategory.SOURCES,
    run=map_rows(web_search),
)
