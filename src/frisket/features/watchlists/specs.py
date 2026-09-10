"""Versioned watchlist query and subscription spec helpers.

The Stage 1 foundation keeps the public Watchlists MVP API stable while adding
canonical, hashable shapes for later watch and notification lanes.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

QUERY_SPEC_VERSION = "frisket.query.v1"
LENS_SPEC_VERSION = "frisket.lens.v1"
WATCH_SPEC_VERSION = "frisket.watch.v1"
DETECTION_POLICY_NEW_MATCHES = "new_matches"
DETECTION_POLICY_MEMBERSHIP_CHANGED = "membership_changed"
DETECTION_POLICY_COUNT_CHANGED = "count_changed"
DETECTION_POLICY_THRESHOLD_CROSSED = "threshold_crossed"
DETECTION_POLICY_ROW_CHANGED = "row_changed"
EMBEDDING_SIMILARITY_KIND = "embedding_similarity"
EMBEDDING_HYBRID_KIND = "embedding_hybrid"
# Anchors a watch can re-evaluate deterministically WITHOUT a provider call. Only a
# `row` anchor (the whole-row vector) is watchable in this slice: manual_text_query
# re-embeds (and may egress) per run, vector_ref is not re-resolvable, and
# `row_cell` is NOT actually column-specific yet (the resolver uses the same
# whole-row vector as `row`), so
# accepting it would be a misleading contract.
_EMBEDDING_WATCH_ANCHOR_KINDS = {"row"}
# The LENS / resolve path additionally accepts `row_cell` (a column-scoped row
# anchor, resolver-safe — reads the stored whole-row vector, NO provider call) AND
# `manual_text_query` (a saved/composed text search that RE-EMBEDS
# its terms on each open, egress-gated like Show-Similar). Watches stay row-only:
# a manual_text_query watch would re-embed (and maybe egress) on every scheduled run.
_EMBEDDING_LENS_ANCHOR_KINDS = {"row", "row_cell", "manual_text_query"}

DETECTION_POLICY_KINDS = {
    DETECTION_POLICY_NEW_MATCHES,
    DETECTION_POLICY_MEMBERSHIP_CHANGED,
    DETECTION_POLICY_COUNT_CHANGED,
    DETECTION_POLICY_THRESHOLD_CROSSED,
    DETECTION_POLICY_ROW_CHANGED,
}


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def query_spec_hash(query: dict[str, Any], *, allow_row_cell: bool = False) -> str:
    normalized = normalize_query_spec(query, allow_row_cell=allow_row_cell)
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def normalize_query_spec(
    query: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    allow_row_cell: bool = False,
) -> dict[str, Any]:
    # ``allow_row_cell`` widens the embedding_similarity anchor set to include
    # ``row_cell`` for the LENS / resolve path. A row_cell anchor is resolver-safe
    # (similarity.py reads the stored whole-row vector, NO provider call), so a
    # saved lens may carry it; WATCHES keep the default row-only set because a
    # row_cell anchor is not column-specific yet and re-evaluating it as a watch
    # would be a misleading contract (see ``_EMBEDDING_WATCH_ANCHOR_KINDS``).
    if not isinstance(query, dict):
        raise ValueError("query spec must be an object")
    kind = str(query.get("kind") or "").strip().lower()
    if query.get("schema_version") == QUERY_SPEC_VERSION:
        if kind not in {
            "search.fts",
            "sheet.filter",
            EMBEDDING_SIMILARITY_KIND,
            EMBEDDING_HYBRID_KIND,
        }:
            raise ValueError(f"unsupported query kind: {kind}")
    if kind in {"fts", "search.fts"}:
        return _normalize_fts_query(query, scope=scope)
    if kind in {"filter", "sheet.filter"}:
        return _normalize_filter_query(query, scope=scope)
    if kind == EMBEDDING_SIMILARITY_KIND:
        return _normalize_embedding_similarity_query(
            query, scope=scope, allow_row_cell=allow_row_cell
        )
    if kind == EMBEDDING_HYBRID_KIND:
        return _normalize_embedding_hybrid_query(query, scope=scope)
    raise ValueError(
        "watch query kind must be fts/search.fts, filter/sheet.filter, "
        "embedding_similarity, or embedding_hybrid"
    )


def normalize_lens_spec(
    query: dict[str, Any],
    *,
    presentation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if presentation is None:
        presentation = {}
    if not isinstance(presentation, dict):
        raise ValueError("lens presentation must be an object")
    return {
        "schema_version": LENS_SPEC_VERSION,
        # Row and row_cell lens anchors reuse stored vectors; manual similarity and
        # hybrid lenses compute a query embedding when resolved. Watches stay row-only.
        "query": normalize_query_spec(query, allow_row_cell=True),
        "presentation": deepcopy(presentation),
    }


def normalize_detection_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    if policy is None:
        return {"kind": DETECTION_POLICY_NEW_MATCHES}
    if not isinstance(policy, dict):
        raise ValueError("detection policy must be an object")
    kind = str(policy.get("kind") or "").strip().lower()
    if kind == DETECTION_POLICY_NEW_MATCHES:
        return {"kind": DETECTION_POLICY_NEW_MATCHES}
    if kind == DETECTION_POLICY_MEMBERSHIP_CHANGED:
        return {"kind": DETECTION_POLICY_MEMBERSHIP_CHANGED}
    if kind == DETECTION_POLICY_COUNT_CHANGED:
        return {"kind": DETECTION_POLICY_COUNT_CHANGED}
    if kind == DETECTION_POLICY_THRESHOLD_CROSSED:
        return _normalize_threshold_policy(policy)
    if kind == DETECTION_POLICY_ROW_CHANGED:
        return _normalize_row_changed_policy(policy)
    allowed = ", ".join(sorted(DETECTION_POLICY_KINDS))
    raise ValueError(f"detection policy kind must be one of: {allowed}")


def normalize_watch_spec(
    query: dict[str, Any],
    *,
    detection_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_query = normalize_query_spec(query)
    normalized_policy = normalize_detection_policy(detection_policy)
    return {
        "schema_version": WATCH_SPEC_VERSION,
        "query": normalized_query,
        "query_hash": query_spec_hash(normalized_query),
        "detection_policy": normalized_policy,
    }


def watch_spec_metadata(
    query: dict[str, Any],
    *,
    scope: str | dict[str, Any] | None = None,
    sheet_id: int | None = None,
    detection_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope_spec = _scope_from_watch(scope, sheet_id)
    normalized_query = normalize_query_spec(query, scope=scope_spec)
    watch_spec = normalize_watch_spec(
        normalized_query,
        detection_policy=detection_policy,
    )
    return {
        "query_version": QUERY_SPEC_VERSION,
        "query_hash": watch_spec["query_hash"],
        "detection_policy": canonical_json(watch_spec["detection_policy"]),
        "query_spec": watch_spec["query"],
        "watch_spec": watch_spec,
    }


def _normalize_fts_query(
    query: dict[str, Any],
    *,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    q = str(query.get("q") or "").strip()
    if not q:
        raise ValueError("fts watch query requires q")
    mode = str(query.get("mode") or "keyword").strip().lower()
    if mode != "keyword":
        raise ValueError("watchlists Stage 1 supports keyword fts only")
    rerank = str(query.get("rerank") or "off").strip().lower()
    if rerank not in {"off", "auto"}:
        raise ValueError("rerank must be off or auto")
    limit = _positive_int(query.get("limit", 50), "limit")
    return {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": "search.fts",
        "scope": _normalize_scope(query.get("scope") or scope, default_kind="project"),
        "q": q,
        "mode": "keyword",
        "rerank": rerank,
        "limit": limit,
    }


def _normalize_filter_query(
    query: dict[str, Any],
    *,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    query_scope = _normalize_scope(
        query.get("scope") or scope,
        default_kind="sheet",
        fallback_sheet_id=query.get("sheet_id"),
    )
    if query_scope["kind"] != "sheet":
        raise ValueError("filter watch query must be sheet-scoped")
    filter_spec = query.get("filter", {})
    if not isinstance(filter_spec, dict):
        raise ValueError("filter watch query filter must be an object")
    normalized: dict[str, Any] = {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": "sheet.filter",
        "scope": query_scope,
        "filter": deepcopy(filter_spec),
    }
    if "sort" in query and query.get("sort") is not None:
        sort_spec = query.get("sort")
        if not isinstance(sort_spec, list):
            raise ValueError("filter watch query sort must be an array")
        normalized["sort"] = deepcopy(sort_spec)
    return normalized


def _normalize_embedding_similarity_query(
    query: dict[str, Any],
    *,
    scope: dict[str, Any] | None,
    allow_row_cell: bool = False,
) -> dict[str, Any]:
    query_scope = _normalize_scope(
        query.get("scope") or scope,
        default_kind="sheet",
        fallback_sheet_id=query.get("sheet_id"),
    )
    if query_scope["kind"] != "sheet":
        raise ValueError("embedding_similarity watch query must be sheet-scoped")
    index_id = str(query.get("embedding_index_id") or "").strip()
    if not index_id:
        raise ValueError("embedding_similarity watch query requires embedding_index_id")
    anchor = query.get("anchor")
    if not isinstance(anchor, dict):
        raise ValueError("embedding_similarity watch query requires an anchor object")
    anchor_kind = str(anchor.get("kind") or "").strip().lower()
    allowed = (
        _EMBEDDING_LENS_ANCHOR_KINDS
        if allow_row_cell
        else _EMBEDDING_WATCH_ANCHOR_KINDS
    )
    if anchor_kind not in allowed:
        if allow_row_cell:
            raise ValueError(
                "embedding_similarity lens anchor must be a row, row_cell, or "
                "manual_text_query; vector_ref is not re-resolvable"
            )
        raise ValueError(
            "embedding_similarity watch anchor must be a row (anchors the whole-row "
            "vector); manual_text_query/vector_ref re-embed per run and row_cell is "
            "not column-specific yet, so neither is watchable"
        )
    if anchor_kind == "manual_text_query":
        # A composed text search saved as a lens. Store canonical
        # `terms` (single `text` -> one term weight 1.0) and, when present, canonical
        # `exclude` (hard NOT) — hash-stable, and the raw query string never leaks into
        # the stored spec. Shared with the resolver (similarity.normalize_*). A query
        # with no `exclude` stores no `exclude` field, so existing lenses keep their hash.
        from frisket.ai.embeddings.similarity import (
            normalize_manual_text_excludes,
            normalize_manual_text_terms,
        )

        normalized_anchor: dict[str, Any] = {
            "kind": "manual_text_query",
            "terms": normalize_manual_text_terms(anchor),
        }
        excludes = normalize_manual_text_excludes(anchor)
        if excludes:
            normalized_anchor["exclude"] = excludes
    else:
        normalized_anchor = {
            "kind": anchor_kind,
            "row_id": _positive_int(anchor.get("row_id"), "anchor.row_id"),
        }
        # A row_cell anchor may name the originating column (provenance only; the
        # resolver uses the whole-row vector). Preserve it when present so the lens
        # records which cell the "similar to" came from.
        if anchor_kind == "row_cell" and anchor.get("column") is not None:
            column = str(anchor["column"]).strip()
            if column:
                normalized_anchor["column"] = column
    normalized: dict[str, Any] = {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": EMBEDDING_SIMILARITY_KIND,
        "scope": query_scope,
        "embedding_index_id": index_id,
        "anchor": normalized_anchor,
        "limit": _positive_int(query.get("limit", 50), "limit"),
    }
    if query.get("space_id") is not None:
        space_id = str(query["space_id"]).strip()
        if space_id:
            normalized["space_id"] = space_id
    if query.get("threshold") is not None:
        threshold = query["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("embedding_similarity threshold must be a number")
        normalized["threshold"] = float(threshold)
    return normalized


def _normalize_embedding_hybrid_query(
    query: dict[str, Any], *, scope: dict[str, Any] | None
) -> dict[str, Any]:
    """A hybrid keyword-and-vector query. The single ``text`` drives both a
    sheet FTS and a vector search over the index; resolution fuses by RRF. Sheet-scoped
    (FTS needs a sheet)."""
    query_scope = _normalize_scope(
        query.get("scope") or scope,
        default_kind="sheet",
        fallback_sheet_id=query.get("sheet_id"),
    )
    if query_scope["kind"] != "sheet":
        raise ValueError("embedding_hybrid query must be sheet-scoped")
    index_id = str(query.get("embedding_index_id") or "").strip()
    if not index_id:
        raise ValueError("embedding_hybrid query requires embedding_index_id")
    text = query.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("embedding_hybrid query requires non-empty text")
    normalized: dict[str, Any] = {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": EMBEDDING_HYBRID_KIND,
        "scope": query_scope,
        "embedding_index_id": index_id,
        "text": text.strip(),
        "limit": _positive_int(query.get("limit", 50), "limit"),
    }
    if query.get("k_rrf") is not None:
        normalized["k_rrf"] = _positive_int(query.get("k_rrf"), "k_rrf")
    return normalized


def _normalize_scope(
    scope: Any,
    *,
    default_kind: str,
    fallback_sheet_id: Any = None,
) -> dict[str, Any]:
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise ValueError("query scope must be an object")
    kind = str(scope.get("kind") or default_kind).strip().lower()
    if kind == "project":
        return {"kind": "project"}
    if kind == "sheet":
        sheet_id = scope.get("sheet_id", fallback_sheet_id)
        return {"kind": "sheet", "sheet_id": _positive_int(sheet_id, "sheet_id")}
    raise ValueError("query scope must be project or sheet")


def _scope_from_watch(
    scope: str | dict[str, Any] | None,
    sheet_id: int | None,
) -> dict[str, Any] | None:
    if scope is None:
        if sheet_id is None:
            return None
        return {"kind": "sheet", "sheet_id": sheet_id}
    if isinstance(scope, dict):
        return deepcopy(scope)
    kind = str(scope or "project").strip().lower()
    if kind == "sheet":
        return {"kind": "sheet", "sheet_id": sheet_id}
    return {"kind": kind}


def _positive_int(value: Any, field: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if out <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return out


def _normalize_threshold_policy(policy: dict[str, Any]) -> dict[str, Any]:
    metric = str(policy.get("metric") or "matched_rows").strip().lower()
    if metric != "matched_rows":
        raise ValueError("threshold_crossed metric must be matched_rows")
    threshold = _positive_int(policy.get("threshold"), "threshold")
    direction = str(policy.get("direction") or "at_or_above").strip().lower()
    if direction not in {"at_or_above", "below", "any"}:
        raise ValueError(
            "threshold_crossed direction must be at_or_above, below, or any"
        )
    return {
        "kind": DETECTION_POLICY_THRESHOLD_CROSSED,
        "metric": "matched_rows",
        "threshold": threshold,
        "direction": direction,
    }


def _normalize_row_changed_policy(policy: dict[str, Any]) -> dict[str, Any]:
    fields = policy.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("row_changed detection policy requires non-empty fields")
    return {
        "kind": DETECTION_POLICY_ROW_CHANGED,
        "fields": [_normalize_row_changed_field(field) for field in fields],
    }


def _normalize_row_changed_field(field: Any) -> dict[str, Any]:
    if not isinstance(field, dict):
        raise ValueError("row_changed fields must be objects")
    out: dict[str, Any] = {}
    if field.get("sheet_id") is not None:
        out["sheet_id"] = _positive_int(field["sheet_id"], "sheet_id")
    if field.get("column_id") is not None:
        out["column_id"] = _positive_int(field["column_id"], "column_id")
    name = field.get("name")
    if name is not None:
        name_text = str(name).strip()
        if not name_text:
            raise ValueError("row_changed field name must be non-empty")
        out["name"] = name_text
    if "column_id" not in out and "name" not in out:
        raise ValueError("row_changed fields require column_id or name")
    return out
