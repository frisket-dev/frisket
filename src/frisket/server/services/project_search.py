"""Browser projection for shared project retrieval."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Literal

from frisket.contracts.http.project_search import (
    ProjectSearchCellAnchor,
    ProjectSearchLocatedAnchor,
    ProjectSearchProjectScope,
    ProjectSearchRequest,
    ProjectSearchSimilarQuery,
    ProjectSearchTextQuery,
)
from frisket.engine._workers.embeddinggemma_model import SETUP_REF
from frisket.engine.jobs.engine_setup import local_search_setup_requirements
from frisket.retrieval.contracts import (
    BindingRevision,
    CellAnchor,
    LocatedHit,
    LocationPrecision,
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
    return _locator_payload(hit.part.locator, precision=hit.precision)


def _locator_payload(
    locator: SourceLocator, *, precision: LocationPrecision = "exact"
) -> dict[str, Any]:
    return {
        "kind": locator.kind,
        "precision": precision,
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


def _contains_search_anchor(span_text: object, anchor: str) -> bool:
    """Match highlighted search text without accepting partial word substrings."""

    text = " ".join(str(span_text or "").split()).casefold()
    if not text or not anchor:
        return False
    offset = 0
    while (start := text.find(anchor, offset)) >= 0:
        end = start + len(anchor)
        left_ok = not (anchor[0].isalnum() or anchor[0] == "_") or (
            start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_")
        )
        right_ok = not (anchor[-1].isalnum() or anchor[-1] == "_") or (
            end == len(text) or not (text[end].isalnum() or text[end] == "_")
        )
        if left_ok and right_ok:
            return True
        offset = start + 1
    return False


def _evidence_navigation(project: Any, hit: LocatedHit) -> dict[str, Any] | None:
    binding = hit.binding
    if binding.column_id is None:
        return None
    cell = (binding.sheet_id, binding.row_id, binding.column_id)
    rows = project.db.execute(
        "SELECT cast(requested.key AS INTEGER) AS requested_rank,"
        "l.stable_id AS evidence_link_id,sp.stable_id AS span_id,"
        "sp.page_start,sp.page_end,sp.start_ms,sp.end_ms,"
        "coalesce(sp.quote,sp.snippet) AS span_text "
        "FROM json_each(?) requested "
        "JOIN source_spans sp ON sp.stable_id=requested.value "
        "JOIN source_artifacts a ON a.id=sp.artifact_id "
        "JOIN evidence_link_spans els ON els.span_id=sp.id "
        "JOIN evidence_links l ON l.id=els.link_id "
        "WHERE requested.type='text' AND l.status='active' "
        "AND ((a.source_sheet_id=? AND a.source_row_id=? "
        "AND a.source_column_id=?) OR (l.sheet_id=? AND l.row_id=? "
        "AND l.column_id=?)) ORDER BY requested_rank,els.rank,l.id",
        (json.dumps(hit.part.locator.source_span_ids), *cell, *cell),
    ).fetchall()
    candidates = []
    prior_rank = None
    for row in rows:
        requested_rank = int(row["requested_rank"])
        if requested_rank != prior_rank:
            candidates.append(row)
            prior_rank = requested_rank
    if not candidates:
        return None

    locator = hit.part.locator
    if locator.pages is not None:
        located = [
            row
            for row in candidates
            if row["page_start"] is not None
            and row["page_end"] is not None
            and int(row["page_start"]) <= locator.pages.end
            and locator.pages.start <= int(row["page_end"])
        ]
        if located:
            candidates = located
    if locator.time is not None:
        located = [
            row
            for row in candidates
            if row["start_ms"] is not None
            and row["end_ms"] is not None
            and int(row["start_ms"]) <= locator.time.end_seconds * 1000
            and locator.time.start_seconds * 1000 <= int(row["end_ms"])
        ]
        if located:
            candidates = located

    anchors = {
        " ".join(unescape(value).split()).casefold()
        for value in re.findall(r"<b>(.*?)</b>", hit.snippet or "", flags=re.DOTALL)
        if value.strip()
    }
    if anchors:
        scored = [
            (
                sum(
                    _contains_search_anchor(row["span_text"], anchor)
                    for anchor in anchors
                ),
                row,
            )
            for row in candidates
        ]
        best = max((score for score, _row in scored), default=0)
        if best:
            candidates = [row for score, row in scored if score == best]

    row = candidates[0]
    return {
        "kind": "evidence",
        "evidence_link_id": str(row["evidence_link_id"]),
        "span_id": str(row["span_id"]),
    }


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
    coverage: SearchCoverage,
    *,
    setup_scope: Literal["workspace", "organization"] | None,
) -> dict[str, Any]:
    setup_requirement = None
    if setup_scope is not None and any(
        channel.channel == "semantic" and channel.state == "unavailable"
        for channel in coverage.channels
    ):
        setup_requirement = next(
            (
                item
                for item in local_search_setup_requirements()
                if item.setup_ref == SETUP_REF
            ),
            None,
        )
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
                    {
                        "setup_ref": SETUP_REF,
                        "scope": setup_scope,
                        "display_name": setup_requirement.display_name,
                        "download_bytes": setup_requirement.download_bytes_if_missing,
                        "additional_runtime_download": bool(
                            setup_requirement.runtime_installable
                            and not setup_requirement.runtime_ready
                        ),
                        "license": setup_requirement.license,
                        "source_url": setup_requirement.source_url,
                    }
                    if setup_requirement is not None
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
        self,
        project_id: str,
        *,
        request: ProjectSearchRequest,
        offer_setup: bool | None = None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        search_request = _request_from_wire(request)
        response = search_with_encoding(
            project,
            search_request,
            runtime=self._workspace.embedding_runtime,
        )
        columns: dict[tuple[int, int], dict[str, Any]] = {
            (int(sheet["id"]), int(column["id"])): dict(column)
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

        anchor = response.response_anchor
        edition = getattr(self._workspace, "edition", "solo")
        if offer_setup is None:
            offer_setup = edition == "solo"
        setup_scope: Literal["workspace", "organization"] | None = None
        if offer_setup and request.named_index_id is None:
            if edition == "solo":
                setup_scope = "workspace"
            elif edition == "team":
                setup_scope = "organization"

        coverage = _coverage_payload(
            response.coverage,
            setup_scope=setup_scope,
        )
        return {
            "response_anchor": (
                {
                    "kind": "located",
                    "source": _source_payload(anchor.source),
                    "location": _locator_payload(anchor.locator),
                    "binding": _binding_payload(anchor.binding)
                    if anchor.binding
                    else None,
                    "exclude_exact": anchor.exclude_exact,
                }
                if anchor is not None
                else None
            ),
            "groups": [
                {
                    "group_key": group.group_key,
                    "primary": hit_payload(group.primary),
                    "supporting": [hit_payload(hit) for hit in group.supporting],
                    "additional_count": group.additional_count,
                }
                for group in response.results
            ],
            "coverage": coverage,
        }


__all__ = ["ProjectSearchService"]
