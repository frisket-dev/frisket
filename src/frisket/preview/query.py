"""Query preview contract helpers built on the shared rowset evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from frisket.actions.types import (
    QUERY_PREVIEW_SCHEMA_VERSION as QUERY_PREVIEW_SCHEMA_VERSION,
    QueryPreviewResult as QueryPreview,
)
from frisket.querysets import (
    SHEET_FILTER_ROWSET_EVALUATOR,
    SheetRowSetError,
    resolve_sheet_filter_rows,
)
from frisket.engine.store import Project
from frisket.features.watchlists.specs import (
    canonical_json,
    normalize_query_spec,
    query_spec_hash,
)

if TYPE_CHECKING:
    from frisket.ai.embeddings.similarity import EmbeddingQueryBinding


QUERY_PREVIEW_EVALUATOR = SHEET_FILTER_ROWSET_EVALUATOR
DEFAULT_QUERY_PREVIEW_LIMIT = 50
MAX_QUERY_PREVIEW_LIMIT = 500


@dataclass(frozen=True)
class QueryPreviewResolution:
    output: QueryPreview
    provider_use: tuple[dict[str, Any], ...]
    source_op_cursor: int
    embedding_binding: EmbeddingQueryBinding | None = None


class QueryPreviewError(ValueError):
    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def resolve_query_preview(
    project: Project,
    query: dict[str, Any],
    *,
    limit: int = DEFAULT_QUERY_PREVIEW_LIMIT,
    offset: int = 0,
) -> QueryPreview:
    return resolve_query_preview_resolution(
        project, query, limit=limit, offset=offset
    ).output


def resolve_query_preview_resolution(
    project: Project,
    query: dict[str, Any],
    *,
    limit: int = DEFAULT_QUERY_PREVIEW_LIMIT,
    offset: int = 0,
) -> QueryPreviewResolution:
    with project.read_snapshot() as snapshot:
        return _resolve_query_preview_in_snapshot(
            snapshot, query, limit=limit, offset=offset
        )


def _resolve_query_preview_in_snapshot(
    project: Project,
    query: dict[str, Any],
    *,
    limit: int,
    offset: int,
) -> QueryPreviewResolution:
    if isinstance(limit, bool) or isinstance(offset, bool):
        raise QueryPreviewError(
            "invalid_query_params",
            "query.preview limit and offset must be integers",
            field="limit",
        )
    try:
        bounded_limit = min(max(0, int(limit)), MAX_QUERY_PREVIEW_LIMIT)
        bounded_offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise QueryPreviewError(
            "invalid_query_params",
            "query.preview limit and offset must be integers",
            field="limit",
        ) from None

    try:
        # Row and row_cell anchors reuse stored vectors; manual and hybrid queries
        # compute a query embedding. A saved lens may carry either form.
        normalized = normalize_query_spec(query, allow_row_cell=True)
    except ValueError as exc:
        raise QueryPreviewError("invalid_query_spec", str(exc), field="query") from exc
    if normalized["kind"] == "embedding_similarity":
        return _resolve_embedding_similarity_preview(
            project,
            normalized,
            limit=bounded_limit,
            offset=bounded_offset,
        )
    if normalized["kind"] == "embedding_hybrid":
        return _resolve_embedding_hybrid_preview(
            project,
            normalized,
            limit=bounded_limit,
            offset=bounded_offset,
        )
    if normalized["kind"] != "sheet.filter":
        raise QueryPreviewError(
            "unsupported_query_kind",
            "query preview supports sheet.filter, embedding_similarity, and "
            "embedding_hybrid queries",
            field="query.kind",
        )
    scope = normalized.get("scope")
    if not isinstance(scope, dict):
        raise QueryPreviewError(
            "invalid_query_spec",
            "sheet.filter query scope must be an object",
            field="query.scope",
        )
    sheet_id = scope.get("sheet_id")
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool) or sheet_id < 1:
        raise QueryPreviewError(
            "invalid_sheet_ref",
            "sheet.filter query scope requires a positive sheet_id",
            field="query.scope.sheet_id",
        )
    _require_visible_sheet(project, sheet_id)

    filter_json = canonical_json(normalized.get("filter", {}))
    sort_json = canonical_json(normalized["sort"]) if "sort" in normalized else None
    try:
        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=bounded_limit,
            offset=bounded_offset,
        )
    except SheetRowSetError as exc:
        raise QueryPreviewError(
            "invalid_query_filter", str(exc), field="query.filter"
        ) from exc

    output = QueryPreview(
        sheet_id=sheet_id,
        query=normalized,
        query_hash=query_spec_hash(normalized, allow_row_cell=True),
        row_ids=rowset.row_ids,
        row_count=len(rowset.row_ids),
        total=rowset.total,
        limit=rowset.limit,
        offset=rowset.offset,
        evaluator=dict(QUERY_PREVIEW_EVALUATOR),
        scores={},
    )
    return QueryPreviewResolution(
        output=output,
        provider_use=(
            {
                "provider": "local",
                "provider_kind": "local_process",
                "service": "frisket.querysets.sheet_filter",
                "version": "v1",
                "external_api": False,
                "cost_actual": 0.0,
            },
        ),
        source_op_cursor=project.op_cursor,
    )


EMBEDDING_SIMILARITY_EVALUATOR = {
    "kind": "embedding_similarity",
    "version": "frisket.embedding_similarity.v1",
}


def _resolve_embedding_similarity_preview(
    project: Project,
    normalized: dict[str, Any],
    *,
    limit: int,
    offset: int,
) -> QueryPreviewResolution:
    """Resolve embedding_similarity through the shared resolver.

    Stored row/row-cell anchors never embed; manual text uses the local default
    gateway. Ranked hits project into row_ids plus per-row distance/score.
    """
    from frisket.ai.embeddings.freshness import embedding_search_freshness_error
    from frisket.ai.embeddings.similarity import (
        SimilarityError,
        resolve_embedding_similarity,
    )
    from frisket.ai.embeddings.vector_backend import VectorBackend

    # Freshness gate (product path, NOT the resolver): never silently search a
    # partial/stale index. Runs before resolution so an appended-but-unembedded row
    # or a changed ready row blocks with a typed, refresh-needed code.
    backend = VectorBackend(project)
    try:
        backend.ensure_schema()
        backend.begin_read_snapshot()
        gate = embedding_search_freshness_error(
            project, normalized.get("embedding_index_id"), backend=backend
        )
        if gate is not None:
            code, message = gate
            raise QueryPreviewError(code, message, field="query.embedding_index_id")
        result = resolve_embedding_similarity(
            project, normalized, gateway=None, backend=backend
        )
    except SimilarityError as exc:
        raise QueryPreviewError(exc.code, exc.message, field=exc.field) from exc
    finally:
        backend.close()
    hits = list(result.hits)
    total = len(hits)
    # limit=0 is an EMPTY window (total preserved) — not "return all".
    window = hits[offset : offset + limit]
    row_ids = [hit.row_id for hit in window]
    scores = {
        hit.row_id: {"distance": hit.distance, "score": hit.score} for hit in window
    }
    sheet_id = result.sheet_id if result.sheet_id is not None else 0
    output = QueryPreview(
        sheet_id=sheet_id,
        query=normalized,
        query_hash=query_spec_hash(normalized, allow_row_cell=True),
        row_ids=row_ids,
        row_count=len(row_ids),
        total=total,
        limit=limit,
        offset=offset,
        evaluator=dict(EMBEDDING_SIMILARITY_EVALUATOR),
        scores=scores,
    )
    return QueryPreviewResolution(
        output=output,
        provider_use=tuple(_embedding_provider_use("embedding_similarity", result)),
        source_op_cursor=project.op_cursor,
        embedding_binding=result.binding,
    )


EMBEDDING_HYBRID_EVALUATOR = {
    "kind": "embedding_hybrid",
    "version": "frisket.embedding_hybrid.v1",
}


def _resolve_embedding_hybrid_preview(
    project: Project,
    normalized: dict[str, Any],
    *,
    limit: int,
    offset: int,
) -> QueryPreviewResolution:
    """Resolve an embedding_hybrid QuerySpec: freshness-gate the vector index, fuse the
    sheet FTS + vector ranks by RRF, and window the result. The per-row score is the
    fused RRF score (distance is None — there is no single cosine distance for a fused
    rank). The vector side's egress gate fires inside resolve_embedding_hybrid."""
    from frisket.ai.embeddings.freshness import embedding_search_freshness_error
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid
    from frisket.ai.embeddings.similarity import SimilarityError
    from frisket.ai.embeddings.vector_backend import VectorBackend

    backend = VectorBackend(project)
    try:
        backend.ensure_schema()
        backend.begin_read_snapshot()
        gate = embedding_search_freshness_error(
            project, normalized.get("embedding_index_id"), backend=backend
        )
        if gate is not None:
            code, message = gate
            raise QueryPreviewError(code, message, field="query.embedding_index_id")
        result = resolve_embedding_hybrid(
            project, normalized, gateway=None, backend=backend
        )
    except SimilarityError as exc:
        field = "text" if exc.field == "anchor.text" else exc.field
        raise QueryPreviewError(exc.code, exc.message, field=field) from exc
    finally:
        backend.close()
    hits = list(result.hits)
    total = len(hits)
    window = hits[offset : offset + limit]
    row_ids = [hit.row_id for hit in window]
    scores = {hit.row_id: {"distance": None, "score": hit.score} for hit in window}
    output = QueryPreview(
        sheet_id=result.sheet_id,
        query=normalized,
        query_hash=query_spec_hash(normalized),
        row_ids=row_ids,
        row_count=len(row_ids),
        total=total,
        limit=limit,
        offset=offset,
        evaluator=dict(EMBEDDING_HYBRID_EVALUATOR),
        scores=scores,
    )
    return QueryPreviewResolution(
        output=output,
        provider_use=tuple(_embedding_provider_use("embedding_hybrid", result)),
        source_op_cursor=project.op_cursor,
        embedding_binding=result.binding,
    )


def query_preview_payload(preview: QueryPreview) -> dict[str, Any]:
    return preview.model_dump(mode="json")


def query_preview_ref(preview: QueryPreview) -> dict[str, Any]:
    return {
        "kind": "query_preview",
        **query_preview_payload(preview),
    }


def query_preview_rowset_ref(preview: QueryPreview) -> dict[str, Any]:
    return {
        "kind": "query_preview_rowset",
        "sheet_id": preview.sheet_id,
        "query_hash": preview.query_hash,
        "row_ids": list(preview.row_ids),
        "row_count": preview.row_count,
        "total": preview.total,
        "offset": preview.offset,
        "limit": preview.limit,
        "evaluator": preview.evaluator.model_dump(mode="json"),
    }


def _embedding_provider_use(kind: str, result: Any) -> list[dict[str, Any]]:
    """Describe the evaluator from facts returned by the evaluation itself."""
    use = result.query_embedding_use
    evaluation = (
        "local_hybrid"
        if kind == "embedding_hybrid"
        else ("local_embedding" if use is not None else "stored_vector")
    )
    fact: dict[str, Any] = {
        "provider": use.provider_id if use is not None else "local",
        "provider_kind": use.provider_kind if use is not None else "local_process",
        "service": f"frisket.query_preview.{evaluation}",
        "version": "v1",
        "external_api": False,
        "cost_actual": 0.0,
    }
    if use is not None:
        fact["model"] = use.actual_model_id
        fact["request_count"] = 1
    return [fact]


def revalidate_query_preview_resolution(
    project: Project, resolved: QueryPreviewResolution
) -> None:
    """Reject a preview whose source or embedding snapshot drifted before receipt."""
    binding = resolved.embedding_binding
    if binding is not None:
        from frisket.ai.embeddings.freshness import embedding_search_freshness_error
        from frisket.ai.embeddings.similarity import current_embedding_query_binding

        gate = embedding_search_freshness_error(project, binding.index_id)
        if gate is not None:
            code, message = gate
            raise QueryPreviewError(code, message, field="query.embedding_index_id")
        if current_embedding_query_binding(project, binding.index_id) != binding:
            raise QueryPreviewError(
                "stale_input",
                "the embedding index changed while the query preview was running; retry",
                field="query.embedding_index_id",
            )
    if project.op_cursor != resolved.source_op_cursor:
        raise QueryPreviewError(
            "stale_input",
            "the project changed while the query preview was running; retry",
            field="query",
        )


def _require_visible_sheet(project: Project, sheet_id: int) -> None:
    if any(int(sheet["id"]) == sheet_id for sheet in project.sheets()):
        return
    raise QueryPreviewError(
        "invalid_sheet_ref",
        "sheet.filter query sheet_id does not identify a visible sheet",
        field="query.scope.sheet_id",
    )
