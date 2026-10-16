"""Named-index hybrid retrieval through the unified query service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.engine.store import Project
from frisket.retrieval.contracts import (
    RankingPolicy,
    RetrievalScope,
    SearchRequest,
    SheetScope,
    TextQuery,
)
from frisket.retrieval.query import RetrievalQueryError, RetrievalQueryService
from frisket.retrieval.store import RetrievalStore

from .scope import IndexScopeError, resolve_index_scope_rows
from .similarity import (
    EmbeddingQueryBinding,
    QueryEmbeddingUse,
    SimilarityError,
    _embedding_query_binding,
    _load_index,
    _manual_query,
)

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class HybridHit:
    row_id: int
    score: float
    vector_rank: int | None
    keyword_rank: int | None


@dataclass(frozen=True)
class HybridResult:
    index_id: str
    sheet_id: int
    hits: list[HybridHit]
    query_embedding_use: QueryEmbeddingUse | None
    binding: EmbeddingQueryBinding


def resolve_embedding_hybrid(
    project: Project,
    query: dict[str, Any],
    *,
    gateway: Any = None,
    effect_project: Project | None = None,
) -> HybridResult:
    """Fuse whole-cell lexical matches with the index's semantic row composites."""

    if not isinstance(query, dict):
        raise SimilarityError("invalid_query_spec", "query must be an object")
    index_id = str(query.get("embedding_index_id") or "").strip()
    if not index_id:
        raise SimilarityError(
            "invalid_query_spec",
            "embedding_hybrid requires embedding_index_id",
            field="embedding_index_id",
        )
    sheet_id = query.get("sheet_id")
    if sheet_id is None and isinstance(query.get("scope"), dict):
        sheet_id = query["scope"].get("sheet_id")
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool):
        raise SimilarityError(
            "invalid_query_spec",
            "embedding_hybrid requires a sheet_id",
            field="sheet_id",
        )
    text = query.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SimilarityError(
            "invalid_query_spec",
            "embedding_hybrid requires non-empty text",
            field="text",
        )
    text = text.strip()
    raw_limit = query.get("limit", 50)
    if (
        isinstance(raw_limit, bool)
        or not isinstance(raw_limit, int)
        or not 1 <= raw_limit <= 500
    ):
        raise SimilarityError(
            "invalid_query_spec", "limit must be in [1, 500]", field="limit"
        )
    raw_k = query.get("k_rrf", DEFAULT_RRF_K)
    if isinstance(raw_k, bool) or not isinstance(raw_k, int) or raw_k < 1:
        raise SimilarityError(
            "invalid_query_spec", "k_rrf must be a positive integer", field="k_rrf"
        )

    index, space, descriptor, facade_space = _load_index(
        project, {"embedding_index_id": index_id}
    )
    if int(index["sheet_id"]) != sheet_id:
        raise SimilarityError(
            "embedding_space_mismatch",
            "the requested sheet is not the embedding index sheet",
            field="sheet_id",
        )
    try:
        row_ids = tuple(resolve_index_scope_rows(project, index))
    except IndexScopeError as exc:
        raise SimilarityError(
            exc.code, exc.message, field="embedding_index_id"
        ) from exc

    with RetrievalStore(project) as retrieval:
        _semantic_query, vectors, query_use = _manual_query(
            project,
            retrieval,
            index=index,
            space=space,
            descriptor=descriptor,
            facade_space=facade_space,
            anchor={"kind": "manual_text_query", "text": text},
            maximum_distance=None,
            gateway=gateway,
            effect_project=effect_project,
        )
        request = SearchRequest(
            scope=RetrievalScope((SheetScope(sheet_id=sheet_id, row_ids=row_ids),)),
            query=TextQuery(text=text, lexical_syntax="plain"),
            mode="hybrid",
            group_by="row",
            limit=raw_limit,
            named_index_id=index_id,
            space_id=str(space["id"]),
            ranking=RankingPolicy(k_rrf=raw_k, rerank="off"),
        )
        try:
            response = RetrievalQueryService(project).search(
                request, semantic_vectors=vectors
            )
        except RetrievalQueryError as exc:
            raise SimilarityError(exc.code, str(exc)) from exc
        binding = _embedding_query_binding(index, space, retrieval)

    hits: list[HybridHit] = []
    for group in response.results:
        vector_rank = None
        keyword_rank = None
        for contribution in group.primary.contributions:
            if contribution.method == "semantic":
                vector_rank = contribution.rank
            elif contribution.method == "lexical":
                keyword_rank = contribution.rank
        hits.append(
            HybridHit(
                row_id=group.primary.binding.row_id,
                score=group.primary.rank_score,
                vector_rank=vector_rank,
                keyword_rank=keyword_rank,
            )
        )
    return HybridResult(
        index_id=index_id,
        sheet_id=sheet_id,
        hits=hits,
        query_embedding_use=query_use,
        binding=binding,
    )
