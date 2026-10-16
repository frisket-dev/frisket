"""One scoped lexical/vector/hybrid query service over the retrieval sidecar."""

from __future__ import annotations

import json
import math
import sqlite3
from html import escape
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any, Literal

from frisket.engine.store import cells
from frisket.search_hydration import native_snippets, searchable_text

from .contracts import (
    BindingRevision,
    ChannelCoverage,
    IndexedPart,
    LocatedHit,
    PageRange,
    ResultGroup,
    RetrievalContribution,
    ScopedBinding,
    SearchCoverage,
    SearchRequest,
    SearchResponse,
    SourceAnchor,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TextRange,
    TimeRange,
    WeightedSemanticQuery,
)
from .native import (
    RETRIEVAL_CACHE_KIB,
    RETRIEVAL_MMAP_BYTES,
    configure_retrieval_connection,
    pack_vector,
)
from .ranking import compose_weighted_vectors, reciprocal_rank_fusion
from .scope import CompiledRetrievalScope, compile_scope
from .source_adapter import (
    UnsupportedLexicalSourceError,
    resolve_lexical_source_text,
)
from .sources import canonical_json, identity_digest, input_hash
from .store import RETRIEVAL_DB_NAME


HYBRID_POOL = 400
RERANK_POOL = 100
Reranker = Callable[[str, Sequence[str]], Sequence[float]]


class RetrievalQueryError(RuntimeError):
    """The requested query cannot be executed with its declared contracts."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SemanticQueryVectors:
    """Encoder output aligned to a request's semantic terms and exclusions."""

    term_vectors: tuple[Sequence[float], ...]
    exclusion_vectors: tuple[Sequence[float], ...] = ()


@dataclass(frozen=True)
class _Candidate:
    source: SourceRevision
    part_id: str
    producer_recipe_id: str
    part_kind: str
    locator: SourceLocator
    binding: ScopedBinding
    method: Literal["lexical", "semantic"]
    channel: str
    raw_score: float
    representation_id: str | None
    space_id: str | None
    lexical_rowid: int | None = None
    lexical_content_hash: str | None = None
    snippet: str | None = None

    def fusion_key(self, grouping: Literal["source", "row"]) -> str:
        if grouping == "row":
            return f"row:{self.binding.sheet_id}:{self.binding.row_id}"
        return _source_key(self.source)


class PinnedRetrievalReader(AbstractContextManager["PinnedRetrievalReader"]):
    """One connection with project.db as main and a read-only attached sidecar."""

    def __init__(self, project: Any):
        self.path = Path(project.path)
        self.db_path = self.path / "project.db"
        self.blob_store = project.blob_store
        self._connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("retrieval reader is closed")
        return self._connection

    def open(self) -> "PinnedRetrievalReader":
        if self._connection is not None:
            return self
        sidecar = self.path / RETRIEVAL_DB_NAME
        if not sidecar.is_file():
            raise RetrievalQueryError(
                "retrieval_not_prepared", "the project retrieval index does not exist"
            )
        db = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            configure_retrieval_connection(db)
            db.execute(
                "ATTACH DATABASE ? AS idx",
                (f"{sidecar.resolve().as_uri()}?mode=ro",),
            )
            db.execute(f"PRAGMA idx.cache_size=-{RETRIEVAL_CACHE_KIB}")
            db.execute(f"PRAGMA idx.mmap_size={RETRIEVAL_MMAP_BYTES}")
            db.execute("BEGIN")
            # Read one page from each database after BEGIN so both WAL snapshots
            # are pinned before scope compilation or ranking begins.
            fence = db.execute(
                "SELECT main_meta.value,idx_meta.value "
                "FROM main.meta AS main_meta "
                "CROSS JOIN idx.retrieval_meta AS idx_meta "
                "WHERE main_meta.key='op_cursor' "
                "AND idx_meta.key='schema_version'"
            ).fetchone()
            if fence is None:
                raise RetrievalQueryError(
                    "retrieval_snapshot_invalid", "could not pin the retrieval snapshot"
                )
        except BaseException:
            db.close()
            raise
        self._connection = db
        return self

    def close(self) -> None:
        db, self._connection = self._connection, None
        if db is None:
            return
        try:
            if db.in_transaction:
                db.rollback()
        finally:
            db.close()

    def __enter__(self) -> "PinnedRetrievalReader":
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def columns(self, sheet_id: int, include_hidden: bool = False) -> list[sqlite3.Row]:
        return cells.columns(self, sheet_id, include_hidden)

    def get_values(
        self,
        sheet_id: int,
        column_id: int,
        row_ids: list[int] | None = None,
        *,
        apply_edits: bool = True,
        tolerate_decode_errors: bool = False,
        preserve_invalid: bool = False,
    ) -> dict[int, Any]:
        return cells.get_values(
            self,
            sheet_id,
            column_id,
            row_ids,
            apply_edits=apply_edits,
            tolerate_decode_errors=tolerate_decode_errors,
            preserve_invalid=preserve_invalid,
        )


class RetrievalQueryService:
    def __init__(self, project: Any, *, reranker: Reranker | None = None):
        self.project = project
        self.reranker = reranker

    def search(
        self,
        request: SearchRequest,
        *,
        semantic_vectors: SemanticQueryVectors | None = None,
        cancel_event: Event | None = None,
    ) -> SearchResponse:
        """Execute one request against an authorization-pinned project snapshot."""

        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("retrieval search was stopped")
        if request.scope.is_empty:
            return _empty_response(request)
        if request.ranking.rerank == "on" and self.reranker is None:
            raise RetrievalQueryError(
                "reranker_unavailable",
                "reranking was required but no reranker is available",
            )
        rerank_pool = (
            min(RERANK_POOL, max(request.limit * 4, request.limit))
            if request.ranking.rerank != "off" and self.reranker is not None
            else request.limit
        )
        try:
            with PinnedRetrievalReader(self.project) as reader:
                if cancel_event is not None:
                    reader.db.set_progress_handler(
                        lambda: int(cancel_event.is_set()), 1000
                    )
                try:
                    scope = compile_scope(reader, request.scope)
                    if request.mode == "lexical":
                        lexical, _query = self._lexical_candidates(
                            reader, request, scope, limit=rerank_pool
                        )
                        groups = _single_method_groups(lexical, request)
                        coverage = self._coverage(
                            reader, request, scope, channels=("lexical",)
                        )
                    elif request.mode == "semantic":
                        semantic = self._semantic_candidates(
                            reader,
                            request,
                            scope,
                            semantic_vectors=semantic_vectors,
                            limit=rerank_pool,
                        )
                        groups = _single_method_groups(semantic, request)
                        channels = _semantic_coverage_channels(reader, request, scope)
                        coverage = self._coverage(
                            reader,
                            request,
                            scope,
                            channels=channels or ("semantic",),
                        )
                    else:
                        pool = min(HYBRID_POOL, max(rerank_pool, request.limit * 4))
                        lexical, _query = self._lexical_candidates(
                            reader, request, scope, limit=pool, fusion=True
                        )
                        semantic = self._semantic_candidates(
                            reader,
                            request,
                            scope,
                            semantic_vectors=semantic_vectors,
                            limit=pool,
                            fusion=True,
                        )
                        groups = _hybrid_groups(lexical, semantic, request)
                        channels = (
                            "lexical",
                            *_semantic_coverage_channels(reader, request, scope),
                        )
                        coverage = self._coverage(
                            reader, request, scope, channels=channels
                        )
                    groups = self._rerank(groups, request)
                finally:
                    if cancel_event is not None:
                        reader.db.set_progress_handler(None, 0)
        except sqlite3.OperationalError as exc:
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and "interrupt" in str(exc).lower()
            ):
                raise InterruptedError("retrieval search was stopped") from exc
            raise
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("retrieval search was stopped")
        return SearchResponse(results=tuple(groups[: request.limit]), coverage=coverage)

    def _rerank(
        self, groups: list[ResultGroup], request: SearchRequest
    ) -> list[ResultGroup]:
        if request.ranking.rerank == "off" or self.reranker is None or not groups:
            return groups
        query_text = _rerank_query_text(request)
        documents = [group.primary.snippet or "" for group in groups]
        if query_text is None or not any(documents):
            if request.ranking.rerank == "on":
                raise RetrievalQueryError(
                    "reranker_input_unavailable",
                    "reranking requires a textual query and hydrated candidate text",
                )
            return groups
        try:
            scores = tuple(
                float(score) for score in self.reranker(query_text, documents)
            )
            if len(scores) != len(groups) or any(
                not math.isfinite(score) for score in scores
            ):
                raise ValueError("reranker returned invalid scores")
        except Exception as exc:
            if request.ranking.rerank == "on":
                raise RetrievalQueryError("reranker_failed", str(exc)) from exc
            return groups
        ranked = sorted(
            zip(groups, scores, strict=True), key=lambda item: item[1], reverse=True
        )
        return [
            replace(group, primary=replace(group.primary, rank_score=score))
            for group, score in ranked
        ]

    def _lexical_candidates(
        self,
        reader: PinnedRetrievalReader,
        request: SearchRequest,
        scope: CompiledRetrievalScope,
        *,
        limit: int,
        fusion: bool = False,
    ) -> tuple[list[_Candidate], str]:
        if not isinstance(request.query, TextQuery):
            raise RetrievalQueryError(
                "invalid_query", "lexical retrieval requires a text query"
            )
        query = (
            request.query.text
            if request.query.lexical_syntax == "fts"
            else _quoted_fts(request.query.text)
        )
        grouping = "row" if request.group_by == "row" else "source"
        group_sql = (
            "printf('row:%d:%d',retrieval_binding.sheet_id,retrieval_binding.row_id)"
            if grouping == "row"
            else _sql_source_key("lexical_source")
        )
        dataset_join, dataset_where, dataset_params = _lexical_dataset_filter(request)
        sql = f"""
        WITH {scope.cte_sql}, matched AS NOT MATERIALIZED (
          SELECT DISTINCT lexical_source.rowid AS lexical_rowid,
                 lexical_source.source_id,lexical_source.version_kind,
                 lexical_source.version_value,lexical_source.version_type,
                 lexical_source.content_hash AS lexical_content_hash,
                 lexical_source.locator_json,source.source_kind,source.content_hash,
                 retrieval_binding.binding_id,retrieval_binding.sheet_id,
                 retrieval_binding.row_id,retrieval_binding.column_id,
                 bm25(lexical_fts) AS raw_score,{group_sql} AS group_key
          FROM idx.lexical_fts
          JOIN idx.lexical_sources AS lexical_source
            ON lexical_source.rowid=lexical_fts.rowid
          JOIN idx.sources AS source
            ON source.source_id=lexical_source.source_id
           AND source.version_kind=lexical_source.version_kind
           AND source.version_value=lexical_source.version_value
           AND source.version_type=lexical_source.version_type
          JOIN idx.bindings AS retrieval_binding
            ON retrieval_binding.source_id=lexical_source.source_id
           AND retrieval_binding.version_kind=lexical_source.version_kind
           AND retrieval_binding.version_value=lexical_source.version_value
           AND retrieval_binding.version_type=lexical_source.version_type
          JOIN scoped_bindings AS scoped
            ON scoped.sheet_id=retrieval_binding.sheet_id
           AND scoped.row_id=retrieval_binding.row_id
           AND (scoped.column_id IS NULL
                OR scoped.column_id=retrieval_binding.column_id)
          {dataset_join}
          WHERE lexical_fts MATCH ?
            AND {_visible_column("retrieval_binding")}
            AND {_binding_is_current("retrieval_binding")}
            {dataset_where}
        ), ranked AS (
          SELECT *,ROW_NUMBER() OVER (
            PARTITION BY group_key ORDER BY raw_score,binding_id
          ) AS group_rank
          FROM matched
        )
        SELECT * FROM ranked WHERE group_rank=1
        ORDER BY raw_score,binding_id,source_id
        LIMIT ?
        """
        params = [
            *scope.params,
            query,
            *dataset_params,
            limit,
        ]
        try:
            rows = reader.db.execute(sql, params).fetchall()
            effective_query = query
        except sqlite3.OperationalError as exc:
            if request.query.lexical_syntax != "fts" or not _is_fts_syntax_error(exc):
                raise
            effective_query = _quoted_fts(request.query.text)
            params[len(scope.params)] = effective_query
            rows = reader.db.execute(sql, params).fetchall()
        candidates = _attach_binding_dependencies(
            reader, [_lexical_candidate(row) for row in rows]
        )
        candidates = _hydrate_lexical(reader, candidates, effective_query)
        return candidates, effective_query

    def _semantic_candidates(
        self,
        reader: PinnedRetrievalReader,
        request: SearchRequest,
        scope: CompiledRetrievalScope,
        *,
        semantic_vectors: SemanticQueryVectors | None,
        limit: int,
        fusion: bool = False,
    ) -> list[_Candidate]:
        if not request.space_id:
            raise RetrievalQueryError(
                "embedding_space_required", "semantic retrieval requires space_id"
            )
        anchor_exclusion: tuple[str, str, str, str, str] | None = None
        if isinstance(request.query, SourceAnchor):
            if semantic_vectors is None:
                query_blob, dimension, _ = _resolve_anchor_vector(
                    reader, request, scope
                )
            else:
                query_blob, dimension = _source_anchor_query_blob(semantic_vectors)
            if request.query.exclude_exact:
                version_value, version_type = _encode_version(
                    request.query.source.version_value
                )
                anchor_exclusion = (
                    request.query.source.source_id,
                    request.query.source.version_kind,
                    version_value,
                    version_type,
                    canonical_json(request.query.locator),
                )
            exclusion_blobs: tuple[bytes, ...] = ()
            thresholds: tuple[float, ...] = ()
            minimum = None
        else:
            query_blob, exclusion_blobs, thresholds, minimum, dimension = (
                _semantic_query_blob(request, semantic_vectors)
            )
        grouping = (
            "row"
            if request.group_by == "row"
            else ("source" if fusion or request.group_by == "source" else "part")
        )
        group_sql = {
            "row": "printf('row:%d:%d',retrieval_binding.sheet_id,retrieval_binding.row_id)",
            "source": _sql_source_key("part"),
            "part": "part.part_id",
        }[grouping]
        dataset_join, dataset_where, dataset_params = _semantic_dataset_filter(request)
        exclude_sql = "".join(
            " AND (1.0-vec1_cos_distance(representation.vector,?))<?"
            for _ in exclusion_blobs
        )
        minimum_sql = "" if minimum is None else " AND similarity>=?"
        sql = f"""
        WITH {scope.cte_sql}, eligible AS NOT MATERIALIZED (
          SELECT DISTINCT part.part_id,part.source_id,part.version_kind,part.version_value,
                 part.version_type,part.recipe_id,part.part_kind,part.locator_json,
                 source.source_kind,source.content_hash,
                 retrieval_binding.binding_id,retrieval_binding.sheet_id,
                 retrieval_binding.row_id,retrieval_binding.column_id,
                 part_representation.channel,representation.representation_id,
                 representation.space_id,{group_sql} AS group_key,
                 1.0-vec1_cos_distance(representation.vector,?) AS similarity
          FROM scoped_bindings AS scoped
          JOIN idx.bindings AS retrieval_binding
            ON retrieval_binding.sheet_id=scoped.sheet_id
           AND retrieval_binding.row_id=scoped.row_id
           AND (scoped.column_id IS NULL
                OR scoped.column_id=retrieval_binding.column_id)
          JOIN idx.parts AS part
            ON part.source_id=retrieval_binding.source_id
           AND part.version_kind=retrieval_binding.version_kind
           AND part.version_value=retrieval_binding.version_value
           AND part.version_type=retrieval_binding.version_type
          JOIN idx.sources AS source
            ON source.source_id=part.source_id
           AND source.version_kind=part.version_kind
           AND source.version_value=part.version_value
           AND source.version_type=part.version_type
          JOIN idx.part_representations AS part_representation
            ON part_representation.part_id=part.part_id
          JOIN idx.representations AS representation
            ON representation.representation_id=part_representation.representation_id
          {dataset_join}
          WHERE {_visible_column("retrieval_binding")}
            AND {_binding_is_current("retrieval_binding")}
            AND representation.space_id=? AND representation.dimension=?
            {dataset_where}
            {"AND NOT (part.source_id=? AND part.version_kind=? AND part.version_value=? AND part.version_type=? AND part.locator_json=?)" if anchor_exclusion is not None else ""}
            {exclude_sql}
        ), accepted AS NOT MATERIALIZED (
          SELECT * FROM eligible WHERE 1=1{minimum_sql}
        ), grouped AS (
          SELECT *,ROW_NUMBER() OVER (
            PARTITION BY group_key
            ORDER BY similarity DESC,binding_id,part_id,representation_id
          ) AS group_rank
          FROM accepted
        )
        SELECT * FROM grouped WHERE group_rank=1
        ORDER BY similarity DESC,group_key,binding_id,part_id,representation_id
        LIMIT ?
        """
        params: list[Any] = [
            *scope.params,
            sqlite3.Binary(query_blob),
            request.space_id,
            dimension,
            *dataset_params,
            *(anchor_exclusion or ()),
        ]
        for blob, threshold in zip(exclusion_blobs, thresholds, strict=True):
            params.extend((sqlite3.Binary(blob), threshold))
        if minimum is not None:
            params.append(minimum)
        params.append(limit)
        return _hydrate_semantic(
            reader,
            _attach_binding_dependencies(
                reader,
                [_semantic_candidate(row) for row in reader.db.execute(sql, params)],
            ),
        )

    def _coverage(
        self,
        reader: PinnedRetrievalReader,
        request: SearchRequest,
        scope: CompiledRetrievalScope,
        *,
        channels: tuple[str, ...],
    ) -> SearchCoverage:
        requested_channels = tuple(dict.fromkeys(channels or (request.mode,)))
        channel_coverage: list[ChannelCoverage] = []
        any_ready = False
        any_pending = False
        any_failed = False
        any_unavailable = False
        for channel in requested_channels:
            ready, pending, failed = _scoped_coverage_state(
                reader, request, scope, channel
            )
            any_ready = any_ready or ready
            any_pending = any_pending or pending
            any_failed = any_failed or failed
            unavailable = not (ready or pending or failed)
            any_unavailable = any_unavailable or unavailable
            if ready:
                state = "ready"
            elif failed:
                state = "failed"
            elif pending:
                state = "pending"
            else:
                state = "unavailable"
            channel_coverage.append(ChannelCoverage(channel=channel, state=state))
        if any_ready and not (any_pending or any_failed or any_unavailable):
            coverage_state = "complete"
        elif any_ready:
            coverage_state = "partial"
        elif any_pending and not (any_failed or any_unavailable):
            coverage_state = "preparing"
        elif any_failed and not (any_pending or any_unavailable):
            coverage_state = "failed"
        elif any_unavailable and not (any_pending or any_failed):
            coverage_state = "unavailable"
        else:
            coverage_state = "partial"
        effective = (
            ("lexical", "semantic") if request.mode == "hybrid" else (request.mode,)
        )
        return SearchCoverage(
            requested_mode=request.mode,
            effective_modes=effective,
            state=coverage_state,
            searched_channels=tuple(
                channel.channel
                for channel in channel_coverage
                if channel.state == "ready"
            ),
            is_top_k=True,
            truncated=False,
            channels=tuple(channel_coverage),
        )


def _scoped_coverage_state(
    reader: PinnedRetrievalReader,
    request: SearchRequest,
    scope: CompiledRetrievalScope,
    channel: str,
) -> tuple[bool, bool, bool]:
    binding_join = (
        "FROM scoped_bindings AS scoped "
        "JOIN idx.bindings AS retrieval_binding "
        "ON retrieval_binding.sheet_id=scoped.sheet_id "
        "AND retrieval_binding.row_id=scoped.row_id "
        "AND (scoped.column_id IS NULL "
        "OR scoped.column_id=retrieval_binding.column_id) "
    )
    binding_where = (
        _visible_column("retrieval_binding")
        + " AND "
        + _binding_is_current("retrieval_binding")
    )
    if channel == "lexical":
        row = reader.db.execute(
            f"""
            WITH {scope.cte_sql}
            SELECT EXISTS (
              SELECT 1 {binding_join}
              JOIN idx.lexical_sources AS lexical_source
                ON lexical_source.source_id=retrieval_binding.source_id
               AND lexical_source.version_kind=retrieval_binding.version_kind
               AND lexical_source.version_value=retrieval_binding.version_value
               AND lexical_source.version_type=retrieval_binding.version_type
              WHERE {binding_where} LIMIT 1
            ) AS ready
            """,
            scope.params,
        ).fetchone()
        return bool(row["ready"]), False, False

    concrete_channel = channel != "semantic"
    ready_channel_sql = "AND part_representation.channel=?" if concrete_channel else ""
    preparation_channel_sql = "AND preparation.channel=?" if concrete_channel else ""
    params: list[Any] = [*scope.params, request.space_id]
    if concrete_channel:
        params.append(channel)
    if concrete_channel:
        params.append(channel)
    if concrete_channel:
        params.append(channel)
    row = reader.db.execute(
        f"""
        WITH {scope.cte_sql}
        SELECT EXISTS (
          SELECT 1 {binding_join}
          JOIN idx.parts AS part
            ON part.source_id=retrieval_binding.source_id
           AND part.version_kind=retrieval_binding.version_kind
           AND part.version_value=retrieval_binding.version_value
           AND part.version_type=retrieval_binding.version_type
          JOIN idx.part_representations AS part_representation
            ON part_representation.part_id=part.part_id
          JOIN idx.representations AS representation
            ON representation.representation_id=part_representation.representation_id
          WHERE {binding_where} AND representation.space_id=?
          {ready_channel_sql} LIMIT 1
        ) AS ready,
        EXISTS (
          SELECT 1 {binding_join}
          JOIN idx.preparation AS preparation
            ON preparation.source_id=retrieval_binding.source_id
           AND preparation.target_version_kind=retrieval_binding.version_kind
           AND preparation.target_version_value=retrieval_binding.version_value
           AND preparation.target_version_type=retrieval_binding.version_type
          WHERE {binding_where}
            AND preparation.status IN ('pending','preparing')
            {preparation_channel_sql} LIMIT 1
        ) AS pending,
        EXISTS (
          SELECT 1 {binding_join}
          JOIN idx.preparation AS preparation
            ON preparation.source_id=retrieval_binding.source_id
           AND preparation.target_version_kind=retrieval_binding.version_kind
           AND preparation.target_version_value=retrieval_binding.version_value
           AND preparation.target_version_type=retrieval_binding.version_type
          WHERE {binding_where} AND preparation.status='failed'
            {preparation_channel_sql} LIMIT 1
        ) AS failed
        """,
        params,
    ).fetchone()
    return bool(row["ready"]), bool(row["pending"]), bool(row["failed"])


def _semantic_coverage_channels(
    reader: PinnedRetrievalReader,
    request: SearchRequest,
    scope: CompiledRetrievalScope,
) -> tuple[str, ...]:
    """List scoped semantic channels independently of the returned top-k rows."""

    dataset_join, dataset_where, dataset_params = _semantic_dataset_filter(request)
    rows = reader.db.execute(
        f"""
        WITH {scope.cte_sql}, scoped_channels(channel) AS (
          SELECT DISTINCT part_representation.channel
          FROM scoped_bindings AS scoped
          JOIN idx.bindings AS retrieval_binding
            ON retrieval_binding.sheet_id=scoped.sheet_id
           AND retrieval_binding.row_id=scoped.row_id
           AND (scoped.column_id IS NULL
                OR scoped.column_id=retrieval_binding.column_id)
          JOIN idx.parts AS part
            ON part.source_id=retrieval_binding.source_id
           AND part.version_kind=retrieval_binding.version_kind
           AND part.version_value=retrieval_binding.version_value
           AND part.version_type=retrieval_binding.version_type
          JOIN idx.part_representations AS part_representation
            ON part_representation.part_id=part.part_id
          JOIN idx.representations AS representation
            ON representation.representation_id=part_representation.representation_id
          {dataset_join}
          WHERE {_visible_column("retrieval_binding")}
            AND {_binding_is_current("retrieval_binding")}
            AND representation.space_id=?
            {dataset_where}
          UNION
          SELECT DISTINCT preparation.channel
          FROM scoped_bindings AS scoped
          JOIN idx.bindings AS retrieval_binding
            ON retrieval_binding.sheet_id=scoped.sheet_id
           AND retrieval_binding.row_id=scoped.row_id
           AND (scoped.column_id IS NULL
                OR scoped.column_id=retrieval_binding.column_id)
          JOIN idx.preparation AS preparation
            ON preparation.source_id=retrieval_binding.source_id
           AND preparation.target_version_kind=retrieval_binding.version_kind
           AND preparation.target_version_value=retrieval_binding.version_value
           AND preparation.target_version_type=retrieval_binding.version_type
          WHERE {_visible_column("retrieval_binding")}
            AND {_binding_is_current("retrieval_binding")}
            AND preparation.status IN ('pending','preparing','failed')
        )
        SELECT channel FROM scoped_channels ORDER BY channel
        """,
        (*scope.params, request.space_id, *dataset_params),
    ).fetchall()
    return tuple(str(row["channel"]) for row in rows)


def search(
    project: Any,
    request: SearchRequest,
    *,
    semantic_vectors: SemanticQueryVectors | None = None,
    cancel_event: Event | None = None,
    reranker: Reranker | None = None,
) -> SearchResponse:
    """Shared public retrieval entry point."""

    return RetrievalQueryService(project, reranker=reranker).search(
        request,
        semantic_vectors=semantic_vectors,
        cancel_event=cancel_event,
    )


def _semantic_query_blob(
    request: SearchRequest, vectors: SemanticQueryVectors | None
) -> tuple[bytes, tuple[bytes, ...], tuple[float, ...], float | None, int]:
    if vectors is None or not vectors.term_vectors:
        raise RetrievalQueryError(
            "query_embedding_required", "semantic query vectors were not supplied"
        )
    if isinstance(request.query, WeightedSemanticQuery):
        if len(vectors.term_vectors) != len(request.query.terms):
            raise RetrievalQueryError(
                "query_embedding_mismatch", "term vectors do not match semantic terms"
            )
        if len(vectors.exclusion_vectors) != len(request.query.exclusions):
            raise RetrievalQueryError(
                "query_embedding_mismatch",
                "exclusion vectors do not match semantic exclusions",
            )
        composed = compose_weighted_vectors(
            vectors.term_vectors, tuple(term.weight for term in request.query.terms)
        )
        thresholds = tuple(term.threshold for term in request.query.exclusions)
        minimum = request.query.minimum_similarity
    elif isinstance(request.query, TextQuery):
        if len(vectors.term_vectors) != 1 or vectors.exclusion_vectors:
            raise RetrievalQueryError(
                "query_embedding_mismatch",
                "plain text requires exactly one query vector",
            )
        composed = compose_weighted_vectors(vectors.term_vectors, (1.0,))
        thresholds = ()
        minimum = None
    else:
        raise RetrievalQueryError("invalid_query", "unsupported semantic query")
    dimension = len(composed)
    query_blob = pack_vector(composed, dimensions=dimension)
    exclusion_blobs = tuple(
        pack_vector(compose_weighted_vectors((vector,), (1.0,)), dimensions=dimension)
        for vector in vectors.exclusion_vectors
    )
    return query_blob, exclusion_blobs, thresholds, minimum, dimension


def _source_anchor_query_blob(
    vectors: SemanticQueryVectors,
) -> tuple[bytes, int]:
    if len(vectors.term_vectors) != 1 or vectors.exclusion_vectors:
        raise RetrievalQueryError(
            "query_embedding_mismatch",
            "a source anchor requires exactly one source vector",
        )
    composed = compose_weighted_vectors(vectors.term_vectors, (1.0,))
    dimension = len(composed)
    return pack_vector(composed, dimensions=dimension), dimension


def _resolve_anchor_vector(
    reader: PinnedRetrievalReader,
    request: SearchRequest,
    scope: CompiledRetrievalScope,
) -> tuple[bytes, int, str]:
    anchor = request.query
    if not isinstance(anchor, SourceAnchor) or not request.space_id:
        raise RetrievalQueryError("invalid_anchor", "source anchor requires a space")
    version_value, version_type = _encode_version(anchor.source.version_value)
    binding_sql = ""
    binding_params: tuple[Any, ...] = ()
    if anchor.binding is not None:
        binding_sql = (
            "AND retrieval_binding.binding_id=? "
            "AND retrieval_binding.sheet_id=? AND retrieval_binding.row_id=? "
            "AND retrieval_binding.column_id IS ?"
        )
        binding_params = (
            anchor.binding.binding_id,
            anchor.binding.sheet_id,
            anchor.binding.row_id,
            anchor.binding.column_id,
        )
    sql = f"""
    WITH {scope.cte_sql}
    SELECT DISTINCT part.part_id,representation.vector,representation.dimension
    FROM scoped_bindings AS scoped
    JOIN idx.bindings AS retrieval_binding
      ON retrieval_binding.sheet_id=scoped.sheet_id
     AND retrieval_binding.row_id=scoped.row_id
     AND (scoped.column_id IS NULL
          OR scoped.column_id=retrieval_binding.column_id)
    JOIN idx.parts AS part
      ON part.source_id=retrieval_binding.source_id
     AND part.version_kind=retrieval_binding.version_kind
     AND part.version_value=retrieval_binding.version_value
     AND part.version_type=retrieval_binding.version_type
    JOIN idx.part_representations AS part_representation
      ON part_representation.part_id=part.part_id
    JOIN idx.representations AS representation
      ON representation.representation_id=part_representation.representation_id
    WHERE {_visible_column("retrieval_binding")}
      AND {_binding_is_current("retrieval_binding")}
      AND part.source_id=? AND part.version_kind=?
      AND part.version_value=? AND part.version_type=?
      AND part.locator_json=? AND representation.space_id=?
      {binding_sql}
    ORDER BY retrieval_binding.binding_id,part.part_id,representation.representation_id
    LIMIT 2
    """
    rows = reader.db.execute(
        sql,
        (
            *scope.params,
            anchor.source.source_id,
            anchor.source.version_kind,
            version_value,
            version_type,
            canonical_json(anchor.locator),
            request.space_id,
            *binding_params,
        ),
    ).fetchall()
    if not rows:
        raise RetrievalQueryError(
            "source_anchor_not_found",
            "the exact source location is not prepared in the authorized scope",
        )
    if len(rows) != 1:
        raise RetrievalQueryError(
            "source_anchor_ambiguous",
            "the source location resolves to more than one prepared representation",
        )
    row = rows[0]
    return bytes(row["vector"]), int(row["dimension"]), str(row["part_id"])


def _lexical_candidate(row: sqlite3.Row) -> _Candidate:
    source = _source_revision(row)
    locator = _locator_from_json(str(row["locator_json"]))
    return _Candidate(
        source=source,
        part_id=identity_digest(
            "lexical_source_part", {"source": source, "locator": locator}
        ),
        producer_recipe_id="frisket.lexical.whole-source.v1",
        part_kind="lexical_source",
        locator=locator,
        binding=_binding(row),
        method="lexical",
        channel="lexical",
        raw_score=float(row["raw_score"]),
        representation_id=None,
        space_id=None,
        lexical_rowid=int(row["lexical_rowid"]),
        lexical_content_hash=str(row["lexical_content_hash"]),
    )


def _semantic_candidate(row: sqlite3.Row) -> _Candidate:
    return _Candidate(
        source=_source_revision(row),
        part_id=str(row["part_id"]),
        producer_recipe_id=str(row["recipe_id"]),
        part_kind=str(row["part_kind"]),
        locator=_locator_from_json(str(row["locator_json"])),
        binding=_binding(row),
        method="semantic",
        channel=str(row["channel"]),
        raw_score=float(row["similarity"]),
        representation_id=str(row["representation_id"]),
        space_id=str(row["space_id"]),
    )


def _hydrate_lexical(
    reader: PinnedRetrievalReader, candidates: list[_Candidate], query: str
) -> list[_Candidate]:
    hydrated: list[tuple[_Candidate, str]] = []
    for candidate in candidates:
        try:
            text = resolve_lexical_source_text(
                reader,
                source=candidate.source,
                locator=candidate.locator,
                binding=candidate.binding,
            )
        except UnsupportedLexicalSourceError as exc:
            raise RetrievalQueryError("lexical_source_unavailable", str(exc)) from exc
        if text is None or input_hash(text) != candidate.lexical_content_hash:
            continue
        hydrated.append((candidate, text))
    snippets = native_snippets(
        reader.db,
        query,
        [
            {"index_id": index, "content": text}
            for index, (_candidate, text) in enumerate(hydrated, start=1)
        ],
        include_rerank=False,
    )
    return [
        replace(candidate, snippet=snippets[index][0])
        for index, (candidate, _text) in enumerate(hydrated, start=1)
        if index in snippets
    ]


def _attach_binding_dependencies(
    reader: PinnedRetrievalReader, candidates: list[_Candidate]
) -> list[_Candidate]:
    binding_ids = tuple(dict.fromkeys(row.binding.binding_id for row in candidates))
    if not binding_ids:
        return candidates
    rows = reader.db.execute(
        "SELECT dependency.binding_id,dependency.row_id,dependency.column_id,"
        "dependency.retrieval_revision "
        "FROM idx.binding_dependencies AS dependency "
        "JOIN json_each(?) AS selected "
        "ON selected.type='text' AND selected.value=dependency.binding_id "
        "ORDER BY dependency.binding_id,dependency.column_id,dependency.row_id",
        (json.dumps(binding_ids),),
    ).fetchall()
    by_binding: dict[str, list[BindingRevision]] = defaultdict(list)
    for row in rows:
        by_binding[str(row["binding_id"])].append(
            BindingRevision(
                row_id=int(row["row_id"]),
                column_id=int(row["column_id"]),
                retrieval_revision=int(row["retrieval_revision"]),
            )
        )
    return [
        replace(
            candidate,
            binding=replace(
                candidate.binding,
                dependencies=tuple(by_binding[candidate.binding.binding_id]),
            ),
        )
        for candidate in candidates
    ]


def _hydrate_semantic(
    reader: PinnedRetrievalReader, candidates: list[_Candidate]
) -> list[_Candidate]:
    hydrated: list[_Candidate] = []
    for candidate in candidates:
        text_range = candidate.locator.text
        column_id = candidate.binding.column_id
        if text_range is None or column_id is None:
            hydrated.append(candidate)
            continue
        values = reader.get_values(
            candidate.binding.sheet_id,
            column_id,
            row_ids=[candidate.binding.row_id],
        )
        text = searchable_text(values.get(candidate.binding.row_id))
        if text is None or input_hash(text) != text_range.content_hash:
            continue
        if text_range.end > len(text):
            continue
        context_start = max(0, text_range.start - 100)
        context_end = min(len(text), text_range.end + 100)
        snippet = (
            escape(text[context_start : text_range.start])
            + "<b>"
            + escape(text[text_range.start : text_range.end])
            + "</b>"
            + escape(text[text_range.end : context_end])
        )
        hydrated.append(replace(candidate, snippet=snippet))
    return hydrated


def _single_method_groups(
    candidates: list[_Candidate], request: SearchRequest
) -> list[ResultGroup]:
    groups: list[ResultGroup] = []
    for rank, candidate in enumerate(candidates, start=1):
        key = _request_group_key(candidate, request)
        hit = _located_hit(candidate, rank=rank, rank_score=1.0 / rank)
        groups.append(ResultGroup(group_key=key, primary=hit))
    return groups


def _hybrid_groups(
    lexical: list[_Candidate], semantic: list[_Candidate], request: SearchRequest
) -> list[ResultGroup]:
    grouping: Literal["source", "row"] = (
        "row" if request.group_by == "row" else "source"
    )
    lexical_by_key = {row.fusion_key(grouping): row for row in lexical}
    semantic_by_key = {row.fusion_key(grouping): row for row in semantic}
    fused = reciprocal_rank_fusion(
        [list(semantic_by_key), list(lexical_by_key)], k=request.ranking.k_rrf
    )
    groups: list[ResultGroup] = []
    for fused_rank, item in enumerate(fused, start=1):
        key = str(item.key)
        semantic_candidate = semantic_by_key.get(key)
        lexical_candidate = lexical_by_key.get(key)
        primary = semantic_candidate or lexical_candidate
        if primary is None:
            continue
        hit = _located_hit(primary, rank=fused_rank, rank_score=item.score)
        contributions: list[RetrievalContribution] = []
        semantic_rank, lexical_rank = item.ranks
        if semantic_candidate is not None and semantic_rank is not None:
            contributions.append(_contribution(semantic_candidate, semantic_rank))
        if lexical_candidate is not None and lexical_rank is not None:
            contributions.append(_contribution(lexical_candidate, lexical_rank))
            if hit.snippet is None:
                hit = replace(hit, snippet=lexical_candidate.snippet)
        hit = replace(hit, contributions=tuple(contributions))
        groups.append(ResultGroup(group_key=key, primary=hit))
        if len(groups) == request.limit:
            break
    return groups


def _located_hit(candidate: _Candidate, *, rank: int, rank_score: float) -> LocatedHit:
    return LocatedHit(
        part=IndexedPart(
            part_id=candidate.part_id,
            source=candidate.source,
            producer_recipe_id=candidate.producer_recipe_id,
            part_kind=candidate.part_kind,
            locator=candidate.locator,
        ),
        binding=candidate.binding,
        contributions=(_contribution(candidate, rank),),
        rank_score=rank_score,
        precision=(
            "source_unit"
            if candidate.method == "lexical"
            or candidate.part_kind == "transcript_chunk"
            else "exact"
        ),
        snippet=candidate.snippet,
    )


def _contribution(candidate: _Candidate, rank: int) -> RetrievalContribution:
    return RetrievalContribution(
        method=candidate.method,
        channel=candidate.channel,
        rank=rank,
        representation_id=candidate.representation_id,
        space_id=candidate.space_id,
        score=candidate.raw_score,
    )


def _request_group_key(candidate: _Candidate, request: SearchRequest) -> str:
    if request.group_by == "part":
        return candidate.part_id
    if request.group_by == "row":
        return candidate.fusion_key("row")
    return candidate.fusion_key("source")


def _rerank_query_text(request: SearchRequest) -> str | None:
    if isinstance(request.query, TextQuery):
        return request.query.text
    if isinstance(request.query, WeightedSemanticQuery):
        return " ".join(term.text for term in request.query.terms)
    return None


def _source_revision(row: sqlite3.Row) -> SourceRevision:
    value: int | str = (
        int(row["version_value"])
        if row["version_type"] == "integer"
        else str(row["version_value"])
    )
    return SourceRevision(
        source_id=str(row["source_id"]),
        source_kind=str(row["source_kind"]),
        version_kind=str(row["version_kind"]),
        version_value=value,
        content_hash=None if row["content_hash"] is None else str(row["content_hash"]),
    )


def _binding(row: sqlite3.Row) -> ScopedBinding:
    return ScopedBinding(
        binding_id=str(row["binding_id"]),
        sheet_id=int(row["sheet_id"]),
        row_id=int(row["row_id"]),
        column_id=None if row["column_id"] is None else int(row["column_id"]),
    )


def _locator_from_json(raw: str) -> SourceLocator:
    value = json.loads(raw)
    text = value.get("text")
    pages = value.get("pages")
    time = value.get("time")
    return SourceLocator(
        kind=str(value["kind"]),
        text=None if text is None else TextRange(**text),
        pages=None if pages is None else PageRange(**pages),
        time=None if time is None else TimeRange(**time),
        source_span_ids=tuple(value.get("source_span_ids", ())),
    )


def _binding_is_current(binding: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM idx.binding_dependencies AS required_dependency "
        f"WHERE required_dependency.binding_id={binding}.binding_id) AND NOT EXISTS ("
        "SELECT 1 FROM idx.binding_dependencies AS dependency "
        "WHERE dependency.binding_id="
        f"{binding}.binding_id AND NOT EXISTS ("
        "SELECT 1 FROM main.current_cells AS current_head "
        "WHERE current_head.column_id=dependency.column_id "
        "AND current_head.row_id=dependency.row_id "
        "AND current_head.retrieval_revision=dependency.retrieval_revision))"
    )


def _visible_column(binding: str) -> str:
    return (
        f"({binding}.column_id IS NULL OR EXISTS (SELECT 1 FROM main.columns AS visible_column "
        f"WHERE visible_column.id={binding}.column_id "
        f"AND visible_column.sheet_id={binding}.sheet_id AND visible_column.hidden=0))"
    )


def _sql_source_key(alias: str) -> str:
    return (
        f"printf('source:%s:%s:%s:%s',{alias}.source_id,{alias}.version_kind,"
        f"{alias}.version_type,{alias}.version_value)"
    )


def _source_key(source: SourceRevision) -> str:
    return identity_digest("source_group", source)


def _semantic_dataset_filter(
    request: SearchRequest,
) -> tuple[str, str, tuple[Any, ...]]:
    if request.named_index_id is None:
        return "", "", ()
    return (
        "JOIN idx.named_dataset_membership AS dataset_member "
        "ON dataset_member.part_id=part.part_id "
        "AND dataset_member.representation_id=representation.representation_id",
        "AND dataset_member.dataset_id=?",
        (request.named_index_id,),
    )


def _lexical_dataset_filter(request: SearchRequest) -> tuple[str, str, tuple[Any, ...]]:
    if request.named_index_id is None:
        return "", "", ()
    return (
        "JOIN idx.parts AS dataset_part ON "
        "dataset_part.source_id=lexical_source.source_id "
        "AND dataset_part.version_kind=lexical_source.version_kind "
        "AND dataset_part.version_value=lexical_source.version_value "
        "AND dataset_part.version_type=lexical_source.version_type "
        "JOIN idx.named_dataset_membership AS dataset_member "
        "ON dataset_member.part_id=dataset_part.part_id",
        "AND dataset_member.dataset_id=?",
        (request.named_index_id,),
    )


def _encode_version(value: int | str) -> tuple[str, str]:
    return (str(value), "integer") if isinstance(value, int) else (value, "text")


def _quoted_fts(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _is_fts_syntax_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "fts5" in message or "syntax error" in message or "unterminated" in message


def _empty_response(request: SearchRequest) -> SearchResponse:
    effective = ("lexical", "semantic") if request.mode == "hybrid" else (request.mode,)
    return SearchResponse(
        results=(),
        coverage=SearchCoverage(
            requested_mode=request.mode,
            effective_modes=effective,
            state="complete",
            searched_channels=(),
            is_top_k=True,
            truncated=False,
        ),
    )
