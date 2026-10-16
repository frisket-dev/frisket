"""Project search services."""

from __future__ import annotations

from typing import Any

from frisket.retrieval.contracts import (
    LocatedHit,
    RankingPolicy,
    RetrievalScope,
    SearchCoverage,
    SearchRequest,
    TextQuery,
)
from frisket.retrieval.service import search_with_encoding
from frisket.server.workspace import Workspace


def _location_payload(hit: LocatedHit) -> dict[str, Any]:
    locator = hit.part.locator
    return {
        "kind": locator.kind,
        "precision": hit.precision,
        "text": (
            {
                "text_surface_id": locator.text.text_surface_id,
                "content_hash": locator.text.content_hash,
                "start": locator.text.start,
                "end": locator.text.end,
            }
            if locator.text is not None
            else None
        ),
        "pages": (
            {"start": locator.pages.start, "end": locator.pages.end}
            if locator.pages is not None
            else None
        ),
        "time": (
            {
                "start_seconds": locator.time.start_seconds,
                "end_seconds": locator.time.end_seconds,
            }
            if locator.time is not None
            else None
        ),
        "source_span_ids": list(locator.source_span_ids),
    }


def _search_hit_payload(
    hit: LocatedHit, *, column: dict[str, Any] | None
) -> dict[str, Any]:
    source = hit.part.source
    contributions = [
        {
            "method": contribution.method,
            "channel": contribution.channel,
            "rank": contribution.rank,
            "representation_id": contribution.representation_id,
            "space_id": contribution.space_id,
            "score": contribution.score,
        }
        for contribution in hit.contributions
    ]
    return {
        "sheet_id": hit.binding.sheet_id,
        "row_id": hit.binding.row_id,
        "column_id": hit.binding.column_id,
        "column_name": str(column["name"]) if column is not None else None,
        "ai_generated": bool(column["ai_generated"]) if column is not None else False,
        "snip": hit.snippet or "",
        "score": hit.rank_score,
        "semantic": any(item["method"] == "semantic" for item in contributions),
        "source": {
            "source_id": source.source_id,
            "source_kind": source.source_kind,
            "version_kind": source.version_kind,
            "version_value": source.version_value,
            "content_hash": source.content_hash,
        },
        "part": {
            "part_id": hit.part.part_id,
            "part_kind": hit.part.part_kind,
            "producer_recipe_id": hit.part.producer_recipe_id,
        },
        "location": _location_payload(hit),
        "contributions": contributions,
        "additional_binding_count": hit.additional_binding_count,
    }


def _coverage_payload(coverage: SearchCoverage) -> dict[str, Any]:
    return {
        "requested_mode": coverage.requested_mode,
        "effective_modes": list(coverage.effective_modes),
        "state": coverage.state,
        "searched_channels": list(coverage.searched_channels),
        "is_top_k": coverage.is_top_k,
        "truncated": coverage.truncated,
        "channels": [
            {
                "channel": channel.channel,
                "state": channel.state,
                "recipe_id": channel.recipe_id,
                "eligible_count": channel.eligible_count,
                "prepared_count": channel.prepared_count,
                "failed_count": channel.failed_count,
                "reason": channel.reason,
            }
            for channel in coverage.channels
        ],
    }


class ProjectSearchService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def search(
        self,
        project_id: str,
        *,
        q: str,
        limit: int,
        mode: str,
        rerank: str,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if mode not in {"keyword", "semantic"}:
            raise ValueError("search mode must be keyword or semantic")
        if rerank not in {"off", "auto", "on"}:
            raise ValueError("rerank must be off, auto, or on")
        semantic = mode == "semantic"
        response = search_with_encoding(
            project,
            SearchRequest(
                scope=RetrievalScope(),
                query=TextQuery(q),
                mode="semantic" if semantic else "lexical",
                group_by="source",
                limit=limit,
                ranking=RankingPolicy(rerank=rerank),
            ),
            runtime=self._workspace.embedding_runtime,
        )
        columns: dict[tuple[int, int], dict[str, Any]] = {
            (int(sheet["id"]), int(column["id"])): column
            for sheet in project.sheets()
            for column in project.columns(int(sheet["id"]))
        }
        hits = [
            _search_hit_payload(
                group.primary,
                column=(
                    columns.get(
                        (
                            group.primary.binding.sheet_id,
                            group.primary.binding.column_id,
                        )
                    )
                    if group.primary.binding.column_id is not None
                    else None
                ),
            )
            for group in response.results
        ]
        coverage = _coverage_payload(response.coverage)
        indexing = response.coverage.state == "preparing" or any(
            channel.state == "pending" for channel in response.coverage.channels
        )
        return {"hits": hits, "indexing": indexing, "coverage": coverage}
