"""Hybrid keyword and vector search fused by Reciprocal Rank Fusion.

The same query ``text`` drives BOTH a sheet-scoped FTS/BM25 keyword search and a vector
search over the index (as a manual_text_query); the two ranked row-id lists fuse by RRF
into one ranked row-set. RRF (rank-based) because BM25 (unbounded) and cosine (−1..1)
scales are incompatible. Only the vector side egresses (one gateway call, egress-gated);
FTS is local.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.search import rrf_fuse, search_sheet
from frisket.engine.store import Project

from .similarity import (
    EmbeddingQueryBinding,
    QueryEmbeddingUse,
    SimilarityError,
    resolve_embedding_similarity,
)
from .vector_backend import VectorBackend

DEFAULT_RRF_K = 60
# Over-fetch each side so RRF has enough candidates to fuse before the caller windows.
_HYBRID_POOL = 200


@dataclass(frozen=True)
class HybridHit:
    row_id: int
    score: float  # the fused RRF score (higher = better)
    vector_rank: int | None  # 1-based rank on the vector side (None = absent)
    keyword_rank: int | None  # 1-based rank on the keyword side (None = absent)


@dataclass(frozen=True)
class HybridResult:
    index_id: str
    sheet_id: int
    hits: list[HybridHit]
    query_embedding_use: QueryEmbeddingUse
    binding: EmbeddingQueryBinding


def resolve_embedding_hybrid(
    project: Project,
    query: dict[str, Any],
    *,
    gateway: Any = None,
    backend: VectorBackend | None = None,
) -> HybridResult:
    """Resolve an ``embedding_hybrid`` query into a fused, ranked row-set. The vector
    side reuses resolve_embedding_similarity (so the SAME remote-egress gate fires before
    any embed); the keyword side is a local sheet-scoped FTS. Hidden rows the FTS might
    surface are dropped (the vector side already excludes them)."""
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
    try:
        k_rrf = int(query.get("k_rrf", DEFAULT_RRF_K))
    except (TypeError, ValueError):
        k_rrf = DEFAULT_RRF_K
    if k_rrf < 1:
        k_rrf = DEFAULT_RRF_K
    # Over-fetch each side so RRF has candidates beyond the requested output window.
    raw_limit = query.get("limit", 50)
    if isinstance(raw_limit, bool):
        raise SimilarityError(
            "invalid_query_spec", "limit must be a positive integer", field="limit"
        )
    try:
        want = int(raw_limit)
    except (TypeError, ValueError):
        raise SimilarityError(
            "invalid_query_spec", "limit must be a positive integer", field="limit"
        ) from None
    if want <= 0:
        raise SimilarityError(
            "invalid_query_spec", "limit must be a positive integer", field="limit"
        )
    available_rows = project.row_count(sheet_id)
    pool = min(max(_HYBRID_POOL, want * 4), max(1, available_rows))

    # VECTOR side first — its egress gate raises BEFORE any embed for a remote index
    # without allow_remote, so the FTS work below never runs in that case.
    vec_query = {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "sheet_id": sheet_id,
        "anchor": {"kind": "manual_text_query", "text": text},
        "limit": pool,
    }
    vec_result = resolve_embedding_similarity(
        project, vec_query, gateway=gateway, backend=backend
    )
    vec_ids = [hit.row_id for hit in vec_result.hits]

    # KEYWORD side — local sheet-scoped FTS.
    fts_ids = search_sheet(project, sheet_id, text, limit=pool)

    fused = rrf_fuse([vec_ids, fts_ids], k=k_rrf)
    vec_rank = {rid: i + 1 for i, rid in enumerate(vec_ids)}
    fts_rank = {rid: i + 1 for i, rid in enumerate(fts_ids)}
    visible = set(project.visible_row_ids(sheet_id))
    hits = [
        HybridHit(
            row_id=rid,
            score=score,
            vector_rank=vec_rank.get(rid),
            keyword_rank=fts_rank.get(rid),
        )
        for rid, score in fused
        if rid in visible
    ][:want]
    if vec_result.query_embedding_use is None:
        raise RuntimeError("hybrid query did not produce a query embedding")
    return HybridResult(
        index_id=index_id,
        sheet_id=int(sheet_id),
        hits=hits,
        query_embedding_use=vec_result.query_embedding_use,
        binding=vec_result.binding,
    )
