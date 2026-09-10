from __future__ import annotations

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.research_types import SearchResult, WebSearcher
from frisket.actions.types import ActionParams, Row, RowResult, Template


class WebSearchParams(ActionParams):
    query: Template[str]
    max_results: int = Field(default=10, ge=1, le=20)


class WebSearchOutput(BaseModel):
    search_results: list[SearchResult]


async def web_search(
    params: WebSearchParams,
    row: Row,
    searcher: WebSearcher,
) -> RowResult[WebSearchOutput]:
    query = params.query.render(row)
    if not query.strip():
        raise ValueError("empty search query after substitution")
    return RowResult(
        output=WebSearchOutput(
            search_results=await searcher.search(query, max_results=params.max_results)
        )
    )


WEB_SEARCH = action(
    examples=(
        WebSearchParams(
            query=Template[str](text="{{source}} annual report"), max_results=5
        ),
    ),
    name="web_search",
    title="Quick search",
    description="Search the web for each row and save structured citation results.",
    category=ActionCategory.SOURCES,
    run=map_rows(web_search),
)
