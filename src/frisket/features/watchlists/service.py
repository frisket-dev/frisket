"""Shared watch evaluation service for manual and source-triggered runs."""

from __future__ import annotations

import json
from typing import Any

from frisket.server.notifications.projections import emit_watch_run_notification
from frisket.querysets import resolve_sheet_filter_rows
from frisket.engine.store import Project
from frisket.features.watchlists.specs import normalize_query_spec, query_spec_hash


_WATCH_FILTER_BATCH_SIZE = 500


class WatchBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def watch_query(row: Any) -> dict[str, Any]:
    raw = row["query"]
    if isinstance(raw, dict):
        query = raw
    elif isinstance(raw, str):
        try:
            query = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise WatchBindingError(
                "query_invalid",
                "watch query must be valid JSON",
            ) from exc
    else:
        query = None
    if not isinstance(query, dict):
        raise WatchBindingError(
            "query_invalid",
            "watch query must be an object",
        )
    return query


def watch_hit_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["is_new"] = bool(data.get("is_new"))
    return data


def evaluate_watch(project: Project, row: Any) -> list[dict[str, Any]]:
    resolution = resolve_watch_run(project, row)
    watch = resolution["watch"]
    return evaluate_resolved_watch(project, watch)


def evaluate_resolved_watch(
    project: Project, watch: dict[str, Any]
) -> list[dict[str, Any]]:
    kind = _query_kind(watch["query"])
    if kind == "search.fts":
        return _evaluate_fts_watch(project, watch)
    if kind == "sheet.filter":
        return _evaluate_filter_watch(project, watch)
    if kind == "embedding_similarity":
        return _evaluate_embedding_similarity_watch(project, watch)
    raise ValueError(
        "watch query kind must be fts/search.fts, filter/sheet.filter, "
        "or embedding_similarity"
    )


def run_watch_evaluation(project: Project, row: Any) -> dict[str, Any]:
    watch_id = int(row["id"])
    op_cursor_before = int(row["last_evaluated_op"] or 0)
    try:
        resolution = resolve_watch_run(project, row)
        hits = evaluate_resolved_watch(project, resolution["watch"])
        status = "ok"
        error = None
        error_code = None
        op_cursor_after = project.op_cursor
        advance_cursor = True
    except WatchBindingError as exc:
        resolution = {
            "resolved_query": {},
            "resolved_query_hash": None,
        }
        hits = []
        status = "error"
        error = exc.message
        error_code = exc.code
        op_cursor_after = op_cursor_before
        advance_cursor = False
    run_id = project.record_watch_run(
        watch_id,
        hits=hits,
        status=status,
        error=error,
        op_cursor_before=op_cursor_before,
        op_cursor_after=op_cursor_after,
        resolved_query_hash=resolution["resolved_query_hash"],
        resolved_query=resolution["resolved_query"],
        error_code=error_code,
        advance_cursor=advance_cursor,
    )
    run = project.get_watch_run(run_id)
    if run is None:
        raise RuntimeError("watch run was not recorded")
    notification = emit_watch_run_notification(
        project,
        watch_id=watch_id,
        run_id=run_id,
    )
    return {
        "watch_id": watch_id,
        "run_id": run_id,
        "run": dict(run),
        "hits": [watch_hit_dict(hit) for hit in project.watch_run_hits(run_id, 500)],
        "notification": notification,
    }


def resolve_watch_run(project: Project, row: Any) -> dict[str, Any]:
    watch = {**dict(row), "query": watch_query(row)}
    try:
        resolved_query = normalize_query_spec(
            watch["query"],
            scope=_query_scope_for_watch(watch),
        )
    except ValueError as exc:
        raise WatchBindingError("query_invalid", str(exc)) from exc
    resolved_query_hash = query_spec_hash(resolved_query)
    return {
        "watch": watch,
        "resolved_query": resolved_query,
        "resolved_query_hash": resolved_query_hash,
    }


def _query_scope_for_watch(watch: dict[str, Any]) -> dict[str, Any]:
    if str(watch.get("scope") or "project").strip().lower() == "sheet":
        return {"kind": "sheet", "sheet_id": _coerce_sheet_id(watch.get("sheet_id"))}
    return {"kind": "project"}


def _query_kind(query: dict[str, Any]) -> str:
    kind = str(query.get("kind") or "").strip().lower()
    if kind in {"fts", "search.fts"}:
        return "search.fts"
    if kind in {"filter", "sheet.filter"}:
        return "sheet.filter"
    return kind


def _query_scope(query: dict[str, Any]) -> dict[str, Any]:
    scope = query.get("scope")
    return scope if isinstance(scope, dict) else {}


def _query_sheet_id(query: dict[str, Any], watch: dict[str, Any]) -> int | None:
    scope = _query_scope(query)
    sheet_id = query.get("sheet_id") or scope.get("sheet_id") or watch.get("sheet_id")
    if sheet_id is None:
        return None
    return _coerce_sheet_id(sheet_id)


def _evaluate_fts_watch(
    project: Project, watch: dict[str, Any]
) -> list[dict[str, Any]]:
    from frisket.search import search_project

    query = watch["query"]
    limit = _coerce_positive_int(query.get("limit") or 50, "limit")
    sheet_id = _query_sheet_id(query, watch)
    search_limit = max(limit, 100) if sheet_id is not None else limit
    raw_hits = search_project(
        project,
        str(query["q"]),
        limit=search_limit,
        rerank=str(query.get("rerank") or "off"),
    )
    hits: list[dict[str, Any]] = []
    seen_rows: set[tuple[int, int]] = set()
    for raw in raw_hits:
        hit_sheet_id = int(raw["sheet_id"])
        if sheet_id is not None and hit_sheet_id != sheet_id:
            continue
        row_id = int(raw["row_id"])
        key = (hit_sheet_id, row_id)
        if key in seen_rows:
            continue
        seen_rows.add(key)
        column_id = raw.get("column_id")
        hits.append(
            {
                "sheet_id": hit_sheet_id,
                "row_id": row_id,
                "column_id": int(column_id) if column_id is not None else None,
                "snippet": str(raw.get("snip") or ""),
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _evaluate_filter_watch(
    project: Project, watch: dict[str, Any]
) -> list[dict[str, Any]]:
    query = watch["query"]
    sheet_id = _query_sheet_id(query, watch)
    if sheet_id is None:
        raise ValueError("filter watch query must be sheet-scoped")
    filter_json = json.dumps(query.get("filter") or {})
    row_ids: list[int] = []
    while True:
        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_json,
            sort=None,
            limit=_WATCH_FILTER_BATCH_SIZE,
            offset=len(row_ids),
        )
        row_ids.extend(rowset.row_ids)
        if not rowset.row_ids or len(row_ids) >= rowset.total:
            break
    return [
        {
            "sheet_id": sheet_id,
            "row_id": row_id,
            "column_id": None,
            "snippet": "Matched filter",
        }
        for row_id in row_ids
    ]


# Stale codes the resolver raises that mean "this index needs a refresh before a
# watch can trust its result set" — collapsed to one typed watch outcome.
_EMBEDDING_STALE_CODES = {"embedding_source_stale", "embedding_anchor_not_found"}


def _evaluate_embedding_similarity_watch(
    project: Project, watch: dict[str, Any]
) -> list[dict[str, Any]]:
    # Lazy imports (mirrors _evaluate_fts_watch) — keeps the embeddings package off
    # the watchlists import path unless an embedding_similarity watch runs.
    from frisket.ai.embeddings import EmbeddingStore
    from frisket.ai.embeddings.freshness import index_freshness
    from frisket.ai.embeddings.similarity import (
        SimilarityError,
        resolve_embedding_similarity,
    )

    query = watch["query"]
    index_id = str(query.get("embedding_index_id") or "")
    store = EmbeddingStore(project)
    index = store.get_index(index_id)
    if index is None:
        raise WatchBindingError(
            "embedding_index_not_found", f"no embedding index {index_id!r}"
        )
    # Sheet-scope guard: a watch scoped to sheet B over an index on sheet A would
    # record cross-sheet hits. Enforced at create too, but re-checked here so a
    # legacy/directly-stored watch row cannot slip through.
    watch_sheet = _query_sheet_id(query, watch)
    index_sheet = index["sheet_id"]
    if (
        watch_sheet is not None
        and index_sheet is not None
        and int(watch_sheet) != int(index_sheet)
    ):
        raise WatchBindingError(
            "embedding_index_sheet_mismatch",
            f"watch sheet {watch_sheet} does not match index sheet {index_sheet}",
        )
    # Freshness: a changed ready row (stale) OR a new embeddable row that was never
    # embedded (missing/incomplete) makes the similar-row set untrustworthy — the
    # resolver would silently drop changed candidates and never see the new rows. So
    # block with a typed result (status='error', cursor NOT advanced) until a
    # refresh runs; the index's maintenance_policy governs the refresh path and the
    # watch never calls a provider. Same source-hash + scope definition as
    # refresh/similarity/export (index_freshness -> build_source_payloads +
    # resolve_index_scope_rows).
    fresh = index_freshness(project, index)
    if not fresh.scope_resolved:
        raise WatchBindingError(
            "embedding_index_scope_invalid",
            "the index stored source scope could not be resolved",
        )
    if fresh.missing_keys:
        raise WatchBindingError(
            "embedding_index_incomplete",
            "new embeddable source rows are not embedded; refresh before evaluating",
        )
    if fresh.stale_keys:
        raise WatchBindingError(
            "embedding_index_stale",
            "embedding index has stale rows; refresh before evaluating",
        )
    try:
        # Row anchors never call a provider, so no gateway is needed (watch anchors
        # are restricted to row in the normalizer).
        result = resolve_embedding_similarity(project, query, gateway=None)
    except SimilarityError as exc:
        code = (
            "embedding_index_stale" if exc.code in _EMBEDDING_STALE_CODES else exc.code
        )
        raise WatchBindingError(code, exc.message) from exc
    return [
        {
            "sheet_id": hit.sheet_id,
            "row_id": hit.row_id,
            "column_id": None,
            "snippet": f"similar (distance {hit.distance:.4f})",
        }
        for hit in result.hits
    ]


def _coerce_positive_int(value: Any, field: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if out <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return out


def _coerce_sheet_id(value: Any) -> int:
    return _coerce_positive_int(value, "sheet_id")
