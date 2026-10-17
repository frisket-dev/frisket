"""Local search composition: shared query encoding followed by scoped retrieval.

This entry point never prepares a corpus, downloads a model, or resolves a
remote provider. Paid named-index actions retain their existing admission
boundary and pass admitted query vectors to ``RetrievalQueryService``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from threading import Event
from typing import TYPE_CHECKING, Any

from .contracts import (
    CellAnchor,
    ChannelCoverage,
    SearchCoverage,
    SearchRequest,
    SearchResponse,
    SourceAnchor,
    TextQuery,
    WeightedSemanticQuery,
)
from .encoding import (
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
    TextEncodingInput,
)
from .query import (
    Reranker,
    RetrievalQueryError,
    RetrievalQueryService,
    SemanticQueryVectors,
    empty_search_response,
)
from .store import RETRIEVAL_DB_NAME, RetrievalStore

if TYPE_CHECKING:
    from frisket.ai.embeddings.runtime import EmbeddingRuntime


def search_with_encoding(
    project: Any,
    request: SearchRequest,
    *,
    runtime: EmbeddingRuntime | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[str], None] | None = None,
    reranker: Reranker | None = None,
) -> SearchResponse:
    """Search with an injected warm local runtime, or a bounded standalone one."""

    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("retrieval search was stopped")
    if request.scope.is_empty or (
        not (project.path / RETRIEVAL_DB_NAME).is_file()
        and project.db.execute("SELECT 1 FROM current_cells LIMIT 1").fetchone() is None
    ):
        return empty_search_response(request)
    if reranker is None and request.ranking.rerank != "off":
        from frisket.ai.embeddings.backends import local_reranker

        reranker = local_reranker()
    query = RetrievalQueryService(project, reranker=reranker)
    if not (project.path / RETRIEVAL_DB_NAME).is_file():
        channels = (
            ("lexical", "semantic") if request.mode == "hybrid" else (request.mode,)
        )
        return SearchResponse(
            results=(),
            coverage=SearchCoverage(
                requested_mode=request.mode,
                effective_modes=(),
                state="preparing",
                searched_channels=(),
                is_top_k=True,
                truncated=False,
                channels=tuple(
                    ChannelCoverage(channel=c, state="pending") for c in channels
                ),
            ),
        )
    if request.mode == "lexical":
        return query.search(request, cancel_event=cancel_event)
    space = EmbeddingGemma2Space()
    anchor_input = None
    if isinstance(request.query, CellAnchor):
        from .anchors import resolve_cell_anchor

        selected = resolve_cell_anchor(
            project,
            request.query,
            named_index_id=request.named_index_id,
            cancelled=cancel_event.is_set if cancel_event is not None else None,
        )
        request = replace(
            request,
            query=selected.anchor,
            space_id=selected.space_id or request.space_id or space.identity,
        )
        anchor_input = selected.input
    if isinstance(request.query, SourceAnchor):
        try:
            response = query.search(
                replace(request, space_id=request.space_id or space.identity),
                cancel_event=cancel_event,
            )
            return replace(response, response_anchor=request.query)
        except RetrievalQueryError as exc:
            if exc.code != "source_anchor_not_found":
                raise
            if anchor_input is None:
                from .located_anchor import resolve_located_anchor

                selected = resolve_located_anchor(
                    project,
                    request.query,
                    cancelled=cancel_event.is_set if cancel_event is not None else None,
                )
                request = replace(request, query=selected.anchor)
                anchor_input = selected.input
    if request.space_id not in (None, space.identity):
        raise RetrievalQueryError(
            "local_embedding_space_unavailable",
            "This search requires a different embedding model.",
        )

    from frisket.ai.embeddings.facade import EmbeddingFacade
    from frisket.ai.embeddings.runtime import EmbeddingRuntime

    inputs, term_count = (
        ((anchor_input,), 1) if anchor_input is not None else _query_inputs(request)
    )
    owned = runtime is None
    encoder = runtime if runtime is not None else EmbeddingRuntime()
    try:
        if progress is not None:
            progress("Encoding the search query")
        with RetrievalStore(project) as cache:
            vectors = EmbeddingFacade(cache).encode(
                inputs,
                encoder=partial(encoder.encode, cancelled=cancel_event),
                purpose=(
                    EncodingPurpose.DOCUMENT
                    if anchor_input is not None
                    else EncodingPurpose.QUERY
                ),
                space=space,
                space_id=space.identity,
            )
    except EncodingError as exc:
        if exc.code != "model_unavailable":
            raise
        return _without_local_model(query, request, cancel_event)
    finally:
        if owned:
            encoder.close()
    if progress is not None:
        progress("Searching project material")
    response = query.search(
        replace(request, space_id=space.identity),
        semantic_vectors=SemanticQueryVectors(
            term_vectors=tuple(vectors[:term_count]),
            exclusion_vectors=tuple(vectors[term_count:]),
        ),
        cancel_event=cancel_event,
    )
    return replace(
        response,
        response_anchor=request.query
        if isinstance(request.query, SourceAnchor)
        else None,
    )


def _query_inputs(request: SearchRequest) -> tuple[tuple[TextEncodingInput, ...], int]:
    if isinstance(request.query, TextQuery):
        return (TextEncodingInput(request.query.text),), 1
    if isinstance(request.query, WeightedSemanticQuery):
        return (
            tuple(
                TextEncodingInput(term.text)
                for term in (*request.query.terms, *request.query.exclusions)
            ),
            len(request.query.terms),
        )
    raise RetrievalQueryError(
        "invalid_query", "Search requires text or a source anchor."
    )


def _without_local_model(
    query: RetrievalQueryService, request: SearchRequest, cancel_event: Event | None
) -> SearchResponse:
    return query.without_semantic(
        request,
        reason="Install the local search model to use semantic search.",
        cancel_event=cancel_event,
    )
