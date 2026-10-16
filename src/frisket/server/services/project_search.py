"""Browser projection for shared project retrieval."""

from __future__ import annotations

from typing import Any

from frisket.contracts.http.project_search import (
    ProjectSearchCellAnchor,
    ProjectSearchLocatedAnchor,
    ProjectSearchProjectScope,
    ProjectSearchRequest,
    ProjectSearchSimilarQuery,
    ProjectSearchTextQuery,
)
from frisket.engine._workers.embeddinggemma_model import SETUP_REF
from frisket.retrieval.contracts import (
    BindingRevision,
    CellAnchor,
    LocatedHit,
    PageRange,
    RankingPolicy,
    RetrievalScope,
    ScopedBinding,
    SearchCoverage,
    SearchRequest,
    SheetScope,
    SourceAnchor,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TextRange,
    TimeRange,
)
from frisket.retrieval.service import search_with_encoding
from frisket.server.workspace import Workspace


_DOCUMENT_COLUMN_TYPES = frozenset({"file", "media", "image", "video", "audio", "link"})


def _source_payload(source: SourceRevision) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "source_kind": source.source_kind,
        "version_kind": source.version_kind,
        "version_value": source.version_value,
        "content_hash": source.content_hash,
    }


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


def _binding_payload(binding: ScopedBinding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "sheet_id": binding.sheet_id,
        "row_id": binding.row_id,
        "column_id": binding.column_id,
        "dependencies": [
            {
                "row_id": dependency.row_id,
                "column_id": dependency.column_id,
                "retrieval_revision": dependency.retrieval_revision,
            }
            for dependency in binding.dependencies
        ],
    }


def _evidence_navigation(project: Any, hit: LocatedHit) -> dict[str, Any] | None:
    binding = hit.binding
    if binding.column_id is None:
        return None
    cell = (binding.sheet_id, binding.row_id, binding.column_id)
    for span_id in hit.part.locator.source_span_ids:
        row = project.db.execute(
            "SELECT l.stable_id AS evidence_link_id,sp.stable_id AS span_id "
            "FROM source_spans sp "
            "JOIN source_artifacts a ON a.id=sp.artifact_id "
            "JOIN evidence_link_spans els ON els.span_id=sp.id "
            "JOIN evidence_links l ON l.id=els.link_id "
            "WHERE sp.stable_id=? AND l.status='active' "
            "AND ((a.source_sheet_id=? AND a.source_row_id=? "
            "AND a.source_column_id=?) OR (l.sheet_id=? AND l.row_id=? "
            "AND l.column_id=?)) ORDER BY els.rank,l.id LIMIT 1",
            (span_id, *cell, *cell),
        ).fetchone()
        if row is not None:
            return {
                "kind": "evidence",
                "evidence_link_id": str(row["evidence_link_id"]),
                "span_id": str(row["span_id"]),
            }
    return None


def _navigation_payload(
    project: Any, hit: LocatedHit, *, column: dict[str, Any] | None
) -> dict[str, Any]:
    evidence = _evidence_navigation(project, hit)
    if evidence is not None:
        return evidence
    binding = hit.binding
    locator = hit.part.locator
    if binding.column_id is not None and (
        locator.pages is not None
        or locator.time is not None
        or (
            column is not None and str(column.get("type", "")) in _DOCUMENT_COLUMN_TYPES
        )
    ):
        return {
            "kind": "document",
            "sheet_id": binding.sheet_id,
            "row_id": binding.row_id,
            "column_id": binding.column_id,
            "page": locator.pages.start if locator.pages is not None else None,
            "time_seconds": (
                locator.time.start_seconds if locator.time is not None else None
            ),
        }
    return {
        "kind": "cell",
        "sheet_id": binding.sheet_id,
        "row_id": binding.row_id,
        "column_id": binding.column_id,
        "text_range": _location_payload(hit)["text"],
    }


def _search_hit_payload(
    project: Any, hit: LocatedHit, *, column: dict[str, Any] | None
) -> dict[str, Any]:
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
        "column_name": str(column["name"]) if column is not None else None,
        "ai_generated": bool(column["ai_generated"]) if column is not None else False,
        "snippet": hit.snippet or "",
        "score": hit.rank_score,
        "source": _source_payload(hit.part.source),
        "part": {
            "part_id": hit.part.part_id,
            "part_kind": hit.part.part_kind,
            "producer_recipe_id": hit.part.producer_recipe_id,
        },
        "binding": _binding_payload(hit.binding),
        "location": _location_payload(hit),
        "navigation": _navigation_payload(project, hit, column=column),
        "contributions": contributions,
        "additional_binding_count": hit.additional_binding_count,
    }


def _coverage_payload(
    coverage: SearchCoverage, *, offer_local_setup: bool
) -> dict[str, Any]:
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
                "setup": (
                    {"setup_ref": SETUP_REF, "scope": "workspace"}
                    if offer_local_setup
                    and channel.channel == "semantic"
                    and channel.state == "unavailable"
                    else None
                ),
            }
            for channel in coverage.channels
        ],
    }


def _scope_payload(scope: ProjectSearchRequest) -> RetrievalScope:
    if isinstance(scope.scope, ProjectSearchProjectScope):
        return RetrievalScope()
    return RetrievalScope(
        sheets=tuple(
            SheetScope(
                sheet_id=item.sheet_id,
                parent_row_id=item.parent_row_id,
                row_ids=tuple(item.row_ids) if item.row_ids is not None else None,
                column_ids=(
                    tuple(item.column_ids) if item.column_ids is not None else None
                ),
                filter_json=item.filter_json,
            )
            for item in scope.scope.sheets
        )
    )


def _locator_from_wire(value: Any) -> SourceLocator:
    return SourceLocator(
        kind=value.kind,
        text=(
            TextRange(
                text_surface_id=value.text.text_surface_id,
                content_hash=value.text.content_hash,
                start=value.text.start,
                end=value.text.end,
            )
            if value.text is not None
            else None
        ),
        pages=(
            PageRange(start=value.pages.start, end=value.pages.end)
            if value.pages is not None
            else None
        ),
        time=(
            TimeRange(
                start_seconds=value.time.start_seconds,
                end_seconds=value.time.end_seconds,
            )
            if value.time is not None
            else None
        ),
        source_span_ids=tuple(value.source_span_ids),
    )


def _binding_from_wire(value: Any) -> ScopedBinding:
    return ScopedBinding(
        binding_id=value.binding_id,
        sheet_id=value.sheet_id,
        row_id=value.row_id,
        column_id=value.column_id,
        dependencies=tuple(
            BindingRevision(
                row_id=dependency.row_id,
                column_id=dependency.column_id,
                retrieval_revision=dependency.retrieval_revision,
            )
            for dependency in value.dependencies
        ),
    )


def _request_from_wire(request: ProjectSearchRequest) -> SearchRequest:
    query = request.query
    if isinstance(query, ProjectSearchTextQuery):
        retrieval_query: Any = TextQuery(
            query.text, lexical_syntax=query.lexical_syntax
        )
    else:
        assert isinstance(query, ProjectSearchSimilarQuery)
        anchor = query.anchor
        if isinstance(anchor, ProjectSearchCellAnchor):
            retrieval_query = CellAnchor(
                sheet_id=anchor.sheet_id,
                row_id=anchor.row_id,
                column_id=anchor.column_id,
                exclude_exact=anchor.exclude_exact,
                locator=(
                    _locator_from_wire(anchor.location)
                    if anchor.location is not None
                    else None
                ),
            )
        else:
            assert isinstance(anchor, ProjectSearchLocatedAnchor)
            retrieval_query = SourceAnchor(
                source=SourceRevision(
                    source_id=anchor.source.source_id,
                    source_kind=anchor.source.source_kind,
                    version_kind=anchor.source.version_kind,
                    version_value=anchor.source.version_value,
                    content_hash=anchor.source.content_hash,
                ),
                locator=_locator_from_wire(anchor.location),
                binding=(
                    _binding_from_wire(anchor.binding)
                    if anchor.binding is not None
                    else None
                ),
                exclude_exact=anchor.exclude_exact,
            )
    return SearchRequest(
        scope=_scope_payload(request),
        query=retrieval_query,
        mode=request.mode,
        group_by=request.group_by,
        limit=request.limit,
        named_index_id=request.named_index_id,
        ranking=RankingPolicy(rerank=request.ranking.rerank),
    )


class ProjectSearchService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def search(
        self, project_id: str, *, request: ProjectSearchRequest
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        search_request = _request_from_wire(request)
        response = search_with_encoding(
            project,
            search_request,
            runtime=self._workspace.embedding_runtime,
        )
        columns: dict[tuple[int, int], dict[str, Any]] = {
            (int(sheet["id"]), int(column["id"])): column
            for sheet in project.sheets()
            for column in project.columns(int(sheet["id"]))
        }

        def hit_payload(hit: LocatedHit) -> dict[str, Any]:
            column = (
                columns.get((hit.binding.sheet_id, hit.binding.column_id))
                if hit.binding.column_id is not None
                else None
            )
            return _search_hit_payload(project, hit, column=column)

        return {
            "groups": [
                {
                    "group_key": group.group_key,
                    "primary": hit_payload(group.primary),
                    "supporting": [hit_payload(hit) for hit in group.supporting],
                    "additional_count": group.additional_count,
                }
                for group in response.results
            ],
            "coverage": _coverage_payload(
                response.coverage,
                offer_local_setup=request.named_index_id is None,
            ),
        }


__all__ = ["ProjectSearchService"]
