"""One pinned, bounded evaluator for viewing, alerting and action cohorts."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from itertools import islice
from pathlib import Path
from typing import Any

from frisket.ai.embeddings.facade import EmbeddingFacade
from frisket.ai.embeddings.runtime import EmbeddingRuntime
from frisket.contracts.query_spec import (
    LOCAL_RETRIEVAL_SIMILARITY_KIND,
    canonical_json,
)
from frisket.querysets import sheet_row_scope_plan
from frisket.retrieval.contracts import (
    RetrievalScope,
    SearchRequest,
    SheetScope,
    SourceAnchor,
    TextQuery,
    WeightedSemanticQuery,
    WeightedTerm,
)
from frisket.retrieval.encoding import (
    EmbeddingGemma2Space,
    EncodingError,
    EncodingInput,
    EncodingPurpose,
    TextEncodingInput,
)
from frisket.retrieval.located_anchor import resolve_located_anchor
from frisket.retrieval.native import Vec1Unavailable
from frisket.retrieval.query import (
    RetrievalQueryError,
    RetrievalQueryService,
    SemanticQueryVectors,
    source_anchor_vectors,
)
from frisket.retrieval.ranking import compose_weighted_vectors
from frisket.retrieval.store import RETRIEVAL_DB_NAME, RetrievalStore
from .specs import (
    SelectionUnavailable,
    membership_hash,
    normalize_selection,
    source_anchor_from_spec,
)

_MAX_ROWS = 9223372036854775807


class SelectionRead:
    def __init__(
        self,
        snapshot: Any,
        selection: dict[str, Any],
        relation: str,
        params: list[Any],
        order: str,
        order_params: list[Any],
    ):
        self.snapshot = snapshot
        self.selection = selection
        self.query_hash = membership_hash(selection)
        self._relation, self._params = relation, params
        self._order, self._order_params = order, order_params
        count = snapshot.db.execute(
            f"SELECT COUNT(*) FROM {relation}", params
        ).fetchone()[0]
        self.total = (
            min(count, selection["limit"]) if selection["limit"] is not None else count
        )

    def ordered_rows_sql(
        self, *, include_ordinal: bool = False
    ) -> tuple[str, list[Any]]:
        """Compose membership in SQL on this read's snapshot connection.

        Limit before downstream keyset predicates; callers needing stable outer
        order may request an ordinal rather than relying on subquery ordering.
        """
        projection = "*"
        params = []
        if include_ordinal:
            projection += (
                f",ROW_NUMBER() OVER (ORDER BY {self._order}) AS selection_ordinal"
            )
            params.extend(self._order_params)
        return (
            f"SELECT {projection} FROM {self._relation} ORDER BY {self._order} LIMIT ?",
            [*params, *self._params, *self._order_params, self.total],
        )

    def hits(
        self, *, offset: int = 0, limit: int | None = None
    ) -> Iterator[dict[str, Any]]:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("page limits must be nonnegative")
        remaining = max(0, self.total - offset)
        size = min(remaining, limit) if limit is not None else remaining
        for row in self.snapshot.db.execute(
            f"SELECT * FROM {self._relation} ORDER BY {self._order} LIMIT ? OFFSET ?",
            [*self._params, *self._order_params, size, offset],
        ):
            yield dict(row)

    def index_of(self, *, sheet_id: int, row_id: int) -> int | None:
        """Locate in the complete ordered selection, respecting its top-N cap."""
        sql, params = self.ordered_rows_sql(include_ordinal=True)
        row = self.snapshot.db.execute(
            f"SELECT selection_ordinal FROM ({sql}) WHERE row_id=? AND sheet_id=?",
            [*params, row_id, sheet_id],
        ).fetchone()
        return None if row is None else int(row[0]) - 1


def _scope(
    snapshot: Any, selection: dict[str, Any]
) -> tuple[list[int], RetrievalScope]:
    scope = selection["scope"]
    if scope["kind"] == "sheet":
        sheets = [scope["sheet_id"]]
        rows = tuple(scope["row_ids"]) if "row_ids" in scope else None
        return sheets, RetrievalScope(
            (
                SheetScope(
                    sheets[0],
                    row_ids=rows,
                    parent_row_id=scope.get("parent_row_id"),
                    filter_json=canonical_json(selection["filters"]),
                ),
            )
        )
    return [int(row["id"]) for row in snapshot.sheets()], RetrievalScope()


def _require_available_filter_inputs(snapshot: Any, selection: dict[str, Any]) -> None:
    """An unfinished input must not become a negative match or false departure."""
    columns = {
        item["column_id"]
        for item in selection["filters"]
        if "failed" not in item["condition"]
    }
    if selection["limit"] is not None:
        columns.update(
            item["column_id"] for item in selection["order"] if "column_id" in item
        )
    if not columns:
        return
    scope = selection["scope"]
    plan = sheet_row_scope_plan(
        snapshot,
        scope["sheet_id"],
        row_ids=scope.get("row_ids"),
        parent_row_id=scope.get("parent_row_id"),
    )
    placeholders = ",".join("?" for _ in columns)
    missing = snapshot.db.execute(
        f"SELECT 1 FROM {plan.from_sql} JOIN current_cell_values input ON input.row_id=r.id "
        f"AND input.column_id IN ({placeholders}) WHERE ({plan.where_sql}) "
        "AND input.outcome='pending' LIMIT 1",
        [*plan.join_params, *sorted(columns), *plan.where_params],
    ).fetchone()
    if missing:
        raise SelectionUnavailable(
            "A value used by this selection is not completed.",
            code="selection_incomplete",
        )


def _encode_local_inputs(
    project: Any,
    inputs: Sequence[EncodingInput],
    *,
    purpose: EncodingPurpose,
    space: EmbeddingGemma2Space,
    embedding_encoder: Any,
) -> list[tuple[float, ...]]:
    owned_runtime: EmbeddingRuntime | None = None
    try:

        def _encode_fresh(
            fresh_inputs: Sequence[EncodingInput],
            *,
            purpose: EncodingPurpose,
            space: EmbeddingGemma2Space,
        ) -> list[tuple[float, ...]]:
            nonlocal owned_runtime
            encoder: Any = embedding_encoder
            if encoder is None:
                owned_runtime = EmbeddingRuntime()
                encoder = owned_runtime
            if not encoder.available():
                raise EncodingError(
                    "model_unavailable",
                    "Install the local search model to evaluate this local retrieval view.",
                )
            return encoder.encode(fresh_inputs, purpose=purpose, space=space)

        with RetrievalStore(project) as cache:
            return [
                tuple(vector)
                for vector in EmbeddingFacade(cache).encode(
                    inputs,
                    encoder=_encode_fresh,
                    purpose=purpose,
                    space=space,
                    space_id=space.identity,
                )
            ]
    except EncodingError as exc:
        if exc.code == "model_unavailable":
            raise SelectionUnavailable(
                "Install the local search model to evaluate this local retrieval view.",
                code="local_embedding_model_unavailable",
            ) from exc
        raise SelectionUnavailable(str(exc), code=exc.code) from exc
    finally:
        if owned_runtime is not None:
            owned_runtime.close()


def _resolve_local_anchor_vector(
    snapshot: Any,
    anchor: SourceAnchor,
    *,
    space: EmbeddingGemma2Space,
    embedding_encoder: Any,
) -> tuple[SourceAnchor, tuple[float, ...]]:
    try:
        vectors = source_anchor_vectors(
            snapshot,
            SearchRequest(
                scope=RetrievalScope(),
                query=anchor,
                mode="semantic",
                group_by="row",
                limit=1,
                space_id=space.identity,
            ),
        )
        return anchor, tuple(float(value) for value in vectors.term_vectors[0])
    except RetrievalQueryError as exc:
        if exc.code != "source_anchor_not_found":
            raise
        if anchor.binding is None:
            raise SelectionUnavailable(str(exc), code=exc.code) from exc
        resolved = resolve_located_anchor(snapshot, anchor)
        if resolved.input is None:
            raise SelectionUnavailable(
                "The selected source anchor has no encodable input.",
                code="source_anchor_unavailable",
            ) from exc
        encoded = _encode_local_inputs(
            snapshot,
            (resolved.input,),
            purpose=EncodingPurpose.DOCUMENT,
            space=space,
            embedding_encoder=embedding_encoder,
        )
        return resolved.anchor, encoded[0]


def _prepare_local_retrieval_request(
    snapshot: Any,
    query: dict[str, Any],
    *,
    scope: RetrievalScope,
    embedding_encoder: Any,
) -> tuple[SearchRequest, SemanticQueryVectors]:
    if not (Path(snapshot.path) / RETRIEVAL_DB_NAME).is_file():
        raise SelectionUnavailable(
            "Local retrieval index is not prepared yet.",
            code="retrieval_not_prepared",
        )
    space = EmbeddingGemma2Space()
    if query.get("space_id") not in (None, space.identity):
        raise SelectionUnavailable(
            "This local retrieval view requires a different embedding space.",
            code="local_embedding_space_unavailable",
        )
    anchor: SourceAnchor | None = None
    anchor_vector: tuple[float, ...] | None = None
    if "source_anchor" in query:
        anchor, anchor_vector = _resolve_local_anchor_vector(
            snapshot,
            source_anchor_from_spec(query["source_anchor"]),
            space=space,
            embedding_encoder=embedding_encoder,
        )
    text_vector: tuple[float, ...] | None = None
    if "query_text" in query:
        text_vector = _encode_local_inputs(
            snapshot,
            (TextEncodingInput(query["query_text"]),),
            purpose=EncodingPurpose.QUERY,
            space=space,
            embedding_encoder=embedding_encoder,
        )[0]
    if anchor is not None and anchor_vector is not None:
        query_vector = (
            compose_weighted_vectors((text_vector, anchor_vector), (1.0, 1.0))
            if text_vector is not None
            else anchor_vector
        )
        return (
            SearchRequest(
                scope=scope,
                query=anchor,
                mode="semantic",
                group_by="row",
                limit=_MAX_ROWS,
                space_id=space.identity,
            ),
            SemanticQueryVectors(term_vectors=(query_vector,)),
        )
    assert text_vector is not None
    return (
        SearchRequest(
            scope=scope,
            query=WeightedSemanticQuery(
                terms=(WeightedTerm(query["query_text"]),),
                minimum_similarity=float(query["min_score"]),
            ),
            mode="semantic",
            group_by="row",
            limit=_MAX_ROWS,
            space_id=space.identity,
        ),
        SemanticQueryVectors(term_vectors=(text_vector,)),
    )


def _prepare_similarity_criterion(
    snapshot: Any,
    query: dict[str, Any],
    *,
    scope: RetrievalScope,
    embedding_encoder: Any,
) -> tuple[SearchRequest, SemanticQueryVectors | None]:
    if query.get("kind") == LOCAL_RETRIEVAL_SIMILARITY_KIND:
        return _prepare_local_retrieval_request(
            snapshot, query, scope=scope, embedding_encoder=embedding_encoder
        )
    from frisket.ai.embeddings import EmbeddingStore
    from frisket.ai.embeddings.freshness import index_freshness
    from frisket.ai.embeddings.similarity import prepared_similarity_request

    index = EmbeddingStore(snapshot).get_index(query["embedding_index_id"])
    if index is None:
        raise SelectionUnavailable("The similarity index is unavailable.")
    fresh = index_freshness(snapshot, index, scope=scope)
    if not fresh.scope_resolved or fresh.missing_count or fresh.stale_count:
        raise SelectionUnavailable(
            "Refresh the similarity index before checking this view.",
            code="embedding_index_stale"
            if fresh.stale_count
            else "embedding_index_incomplete",
        )
    return prepared_similarity_request(snapshot, query, scope=scope)


def _hit_passes_similarity_threshold(similarity: dict[str, Any], score: float) -> bool:
    if similarity.get("kind") == LOCAL_RETRIEVAL_SIMILARITY_KIND:
        return score >= float(similarity["min_score"])
    threshold = similarity.get("threshold")
    return threshold is None or 1 - score <= threshold


def _stage_matches(
    snapshot: Any,
    selection: dict[str, Any],
    scope: RetrievalScope,
    path: Path,
    *,
    embedding_encoder: Any = None,
) -> None:
    stage = sqlite3.connect(path)
    try:
        stage.execute(
            "CREATE TABLE matches(sheet_id INTEGER,row_id INTEGER,search_score REAL,similarity_score REAL,search_rank INTEGER,similarity_rank INTEGER,PRIMARY KEY(sheet_id,row_id)) WITHOUT ROWID"
        )
        from frisket.retrieval.scope import compile_scope

        candidates = compile_scope(snapshot, scope)
        if (
            snapshot.db.execute(
                f"WITH {candidates.cte_sql} SELECT 1 FROM scoped_bindings LIMIT 1",
                candidates.params,
            ).fetchone()
            is None
        ):
            return
        criteria = []
        if "search" in selection:
            criteria.append(
                (
                    "search",
                    SearchRequest(
                        scope=scope,
                        query=TextQuery(selection["search"]["text"]),
                        group_by="row",
                        limit=_MAX_ROWS,
                    ),
                    None,
                )
            )
        if "similarity" in selection:
            request, vectors = _prepare_similarity_criterion(
                snapshot,
                selection["similarity"],
                scope=scope,
                embedding_encoder=embedding_encoder,
            )
            criteria.append(("similarity", request, vectors))
        for kind, request, vectors in criteria:
            stream = RetrievalQueryService(snapshot).iter_row_matches(
                request, semantic_vectors=vectors
            )
            rank = 0
            while batch := list(islice(stream, 500)):
                values = []
                for hit in batch:
                    rank += 1
                    if kind == "similarity" and not _hit_passes_similarity_threshold(
                        selection["similarity"], float(hit["score"])
                    ):
                        continue
                    values.append((hit["sheet_id"], hit["row_id"], hit["score"], rank))
                stage.executemany(
                    f"INSERT INTO matches(sheet_id,row_id,{kind}_score,{kind}_rank) VALUES(?,?,?,?) "
                    f"ON CONFLICT(sheet_id,row_id) DO UPDATE SET {kind}_score=excluded.{kind}_score,{kind}_rank=excluded.{kind}_rank",
                    values,
                )
                stage.commit()
    finally:
        stage.close()


@contextmanager
def open_selection(
    project: Any,
    definition: dict[str, Any],
    *,
    embedding_encoder: Any = None,
) -> Iterator[SelectionRead]:
    """Own one read lifetime; temporary match storage closes after its snapshot.

    The ordinary filter-only path stays a direct SQL query. Search scores use a
    disposable disk relation so composing/sorting a million matches does not
    require a Python list. This is not another persisted source of truth.
    """
    context = (
        nullcontext(project)
        if getattr(project, "is_read_snapshot", False)
        else project.read_snapshot()
    )
    with context as snapshot:
        selection = normalize_selection(snapshot, definition)
        sheets, retrieval_scope = _scope(snapshot, selection)
        _require_available_filter_inputs(snapshot, selection)
        has_matches = "search" in selection or "similarity" in selection
        if has_matches:
            path = (
                snapshot.temporary_directory(prefix="frisket-view-") / "matches.sqlite"
            )
            match_schema = "view_" + uuid.uuid4().hex
            try:
                _stage_matches(
                    snapshot,
                    selection,
                    retrieval_scope,
                    path,
                    embedding_encoder=embedding_encoder,
                )
            except Vec1Unavailable as exc:
                raise SelectionUnavailable(
                    str(exc), code="embedding_backend_unavailable"
                ) from exc
            snapshot.db.execute(
                f"ATTACH DATABASE ? AS {match_schema}", (f"file:{path}?mode=ro",)
            )
        clauses, params, order, order_params = [], [], [], []
        for sheet in sheets:
            plan = sheet_row_scope_plan(
                snapshot,
                sheet,
                filter_=canonical_json(selection["filters"]),
                sort=canonical_json(
                    [item for item in selection["order"] if "column_id" in item]
                ),
                row_ids=selection["scope"].get("row_ids"),
                parent_row_id=selection["scope"].get("parent_row_id"),
            )
            terms = iter(plan.order_terms[:-2])
            selections, per_sheet_order, per_sheet_params = [], [], []
            # Project query values are projections, not stored data columns.
            if has_matches:
                selections.extend(["matched.search_score", "matched.similarity_score"])
            else:
                selections.extend(["NULL search_score", "NULL similarity_score"])
            for number, item in enumerate(selection["order"]):
                if "column_id" in item:
                    for suffix in ("null", "value"):
                        term = next(terms)
                        alias = f"sort_{number}_{suffix}"
                        selections.append(f"{term.sql}{term.collation} AS {alias}")
                        per_sheet_params.extend(term.params)
                        per_sheet_order.append(
                            f"{alias} {'DESC' if term.descending else 'ASC'}"
                        )
                else:
                    per_sheet_order.append(
                        f"{item['score']}_score {item.get('dir', 'asc').upper()}"
                    )
            join = ""
            where = plan.where_sql
            if has_matches:
                join = f" JOIN {match_schema}.matches matched ON matched.sheet_id=r.sheet_id AND matched.row_id=r.id"
                if selection.get("ranking") != "hybrid":
                    where += "".join(
                        f" AND matched.{kind}_score IS NOT NULL"
                        for kind in ("search", "similarity")
                        if kind in selection
                    )
                selections.append(
                    f"COALESCE(1.0/({selection.get('rank_constant', 60)}+matched.search_rank),0)+COALESCE(1.0/({selection.get('rank_constant', 60)}+matched.similarity_rank),0) AS relevance_score"
                )
            else:
                selections.append("NULL relevance_score")
            clauses.append(
                f"SELECT r.sheet_id,r.id row_id,NULL column_id,r.position,{','.join(selections)} FROM {plan.from_sql}{join} WHERE {where}"
            )
            params.extend([*per_sheet_params, *plan.join_params, *plan.where_params])
            order = per_sheet_order
        if not clauses:
            clauses = [
                "SELECT NULL sheet_id,NULL row_id,NULL column_id,NULL position,NULL search_score,NULL similarity_score,NULL relevance_score WHERE 0"
            ]
        # A selection is ordered only as it says: with no order it keeps sheet
        # order, searched or not. Ranking is an explicit score order, or the
        # hybrid ranking, which has no order item of its own.
        if not order and has_matches and selection.get("ranking") == "hybrid":
            order = ["relevance_score DESC"]
        order += ["sheet_id ASC", "position ASC", "row_id ASC"]
        relation = "(" + " UNION ALL ".join(clauses) + ") AS selected"
        yield SelectionRead(
            snapshot, selection, relation, params, ",".join(order), order_params
        )
