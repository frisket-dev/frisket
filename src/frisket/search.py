"""Keyword search and explicit maintenance over the shared retrieval store.

Interactive search never prepares sources or downloads models. CLI diagnostics
and workers may explicitly invoke the maintenance functions exported here.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from frisket.retrieval.contracts import (
    RankingPolicy,
    RetrievalScope,
    SearchRequest,
    SheetScope,
    TextQuery,
)
from frisket.retrieval.service import search_with_encoding
from frisket.search_index import (
    SearchIndexNotReady,
    drain_index,  # noqa: F401 -- explicit maintenance API
    index_batch,  # noqa: F401 -- bounded worker API
    index_needs_work,  # noqa: F401 -- scheduling API
)


def search_project(
    project: Any, query: str, limit: int = 50, rerank: str = "auto"
) -> list[dict[str, Any]]:
    """Return current located keyword matches; signal incomplete preparation."""
    response = search_with_encoding(
        project,
        SearchRequest(
            scope=RetrievalScope(),
            query=TextQuery(query),
            mode="lexical",
            group_by="source",
            limit=limit,
            ranking=RankingPolicy(rerank=rerank),
        ),
    )
    if response.coverage.state in {"preparing", "unavailable", "failed"} or any(
        channel.state == "pending" for channel in response.coverage.channels
    ):
        raise SearchIndexNotReady()
    columns: dict[int, dict[int, Any]] = {}
    hits = []
    for group in response.results:
        hit = group.primary
        binding = hit.binding
        if binding.sheet_id not in columns:
            columns[binding.sheet_id] = {
                int(column["id"]): column
                for column in project.columns(binding.sheet_id)
            }
        column = columns[binding.sheet_id].get(binding.column_id)
        hits.append(
            {
                "sheet_id": binding.sheet_id,
                "row_id": binding.row_id,
                "column_id": binding.column_id,
                "column_name": column["name"] if column is not None else None,
                "ai_generated": bool(column["ai_generated"])
                if column is not None
                else False,
                "snip": hit.snippet or "",
                "location": asdict(hit.part.locator),
            }
        )
    return hits


def search_sheet(project: Any, sheet_id: int, query: str, limit: int = 50) -> list[int]:
    """Rank rows inside a sheet using the same scoped keyword query."""
    response = search_with_encoding(
        project,
        SearchRequest(
            scope=RetrievalScope((SheetScope(sheet_id),)),
            query=TextQuery(query),
            mode="lexical",
            group_by="row",
            limit=limit,
            ranking=RankingPolicy(rerank="off"),
        ),
    )
    if response.coverage.state != "complete":
        raise SearchIndexNotReady()
    return [group.primary.binding.row_id for group in response.results]


__all__ = [
    "SearchIndexNotReady",
    "drain_index",
    "index_batch",
    "index_needs_work",
    "search_project",
    "search_sheet",
]
