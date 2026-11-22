"""Shared query-preview normalization and independent alert conditions.

Saved selection and presentation belong to View; alerts reference that View.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

QUERY_SPEC_VERSION = "frisket.query.v1"
DETECTION_POLICY_NEW_MATCHES = "new_matches"
DETECTION_POLICY_MEMBERSHIP_CHANGED = "membership_changed"
DETECTION_POLICY_COUNT_CHANGED = "count_changed"
DETECTION_POLICY_THRESHOLD_CROSSED = "threshold_crossed"
DETECTION_POLICY_ROW_CHANGED = "row_changed"
DETECTION_POLICY_CONDITIONS = "conditions"
EMBEDDING_SIMILARITY_KIND = "embedding_similarity"
EMBEDDING_HYBRID_KIND = "embedding_hybrid"
LOCAL_RETRIEVAL_SIMILARITY_KIND = "local_retrieval"
# Legacy query-preview consumers may still request row-only anchors. Unified
# Views permit prepared text and truthful single-column cell anchors as well.
_ROW_ANCHOR_KINDS = {"row"}
_PREPARED_ANCHOR_KINDS = {"row", "row_cell", "manual_text_query"}

DETECTION_POLICY_KINDS = {
    DETECTION_POLICY_NEW_MATCHES,
    DETECTION_POLICY_MEMBERSHIP_CHANGED,
    DETECTION_POLICY_COUNT_CHANGED,
    DETECTION_POLICY_THRESHOLD_CROSSED,
    DETECTION_POLICY_ROW_CHANGED,
    DETECTION_POLICY_CONDITIONS,
}


MAX_QUERY_TERMS = 16
DEFAULT_EXCLUDE_THRESHOLD = 0.5


def normalize_manual_text_excludes(anchor: dict[str, Any]) -> list[dict[str, Any]]:
    raw = anchor.get("exclude")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("manual_text_query exclude must be a list")
    if len(raw) > MAX_QUERY_TERMS:
        raise ValueError(
            f"manual_text_query supports at most {MAX_QUERY_TERMS} exclude terms"
        )
    out: list[dict[str, Any]] = []
    for index, term in enumerate(raw):
        if not isinstance(term, dict):
            raise ValueError(f"exclude {index} must be an object {{text, threshold}}")
        text = term.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"exclude {index} needs non-empty text")
        threshold = term.get("threshold", DEFAULT_EXCLUDE_THRESHOLD)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not math.isfinite(threshold)
        ):
            raise ValueError(f"exclude {index} threshold must be a finite number")
        threshold = float(threshold)
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"exclude {index} threshold must be in (0, 1]")
        out.append({"text": text.strip(), "threshold": threshold})
    return out


def normalize_manual_text_terms(anchor: dict[str, Any]) -> list[dict[str, Any]]:
    raw_terms = anchor.get("terms")
    text = anchor.get("text")
    if raw_terms is not None and text is not None:
        raise ValueError("manual_text_query takes either text or terms, not both")
    if raw_terms is None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("manual_text_query requires non-empty text or terms")
        return [{"text": text.strip(), "weight": 1.0}]
    if not isinstance(raw_terms, list) or not raw_terms:
        raise ValueError("manual_text_query terms must be a non-empty list")
    if len(raw_terms) > MAX_QUERY_TERMS:
        raise ValueError(f"manual_text_query supports at most {MAX_QUERY_TERMS} terms")
    out: list[dict[str, Any]] = []
    for index, term in enumerate(raw_terms):
        if not isinstance(term, dict):
            raise ValueError(f"term {index} must be an object {{text, weight}}")
        value = term.get("text")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"term {index} needs non-empty text")
        weight = term.get("weight", 1.0)
        if (
            isinstance(weight, bool)
            or not isinstance(weight, int | float)
            or not math.isfinite(weight)
        ):
            raise ValueError(f"term {index} weight must be a finite number")
        out.append({"text": value.strip(), "weight": float(weight)})
    return out


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
    if not isinstance(query, dict):
        raise ValueError("query spec must be an object")
    kind = str(query.get("kind") or "").strip().lower()
    if query.get("schema_version") == QUERY_SPEC_VERSION:
        if kind not in {
            "search.fts",
            "sheet.filter",
            EMBEDDING_SIMILARITY_KIND,
            EMBEDDING_HYBRID_KIND,
            LOCAL_RETRIEVAL_SIMILARITY_KIND,
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
    if kind == LOCAL_RETRIEVAL_SIMILARITY_KIND:
        return _normalize_local_retrieval_similarity_query(query, scope=scope)
    raise ValueError(
        "watch query kind must be fts/search.fts, filter/sheet.filter, "
        "embedding_similarity, embedding_hybrid, or local_retrieval"
    )


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
    if kind == DETECTION_POLICY_CONDITIONS:
        normalized: dict[str, Any] = {"kind": kind}
        for name, default in (
            ("entered", True),
            ("exited", False),
            ("count_changed", False),
        ):
            value = policy.get(name, default)
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
            normalized[name] = value
        fields = policy.get("fields", [])
        if not isinstance(fields, list):
            raise ValueError("fields must be an array")
        normalized["fields"] = [_normalize_row_changed_field(field) for field in fields]
        if policy.get("threshold") is not None:
            threshold = policy["threshold"]
            if not isinstance(threshold, dict):
                raise ValueError("threshold must be an object")
            normalized["threshold"] = _normalize_threshold_policy(threshold)
        return normalized
    allowed = ", ".join(sorted(DETECTION_POLICY_KINDS))
    raise ValueError(f"detection policy kind must be one of: {allowed}")


def detection_conditions(policy: dict[str, Any]) -> dict[str, Any]:
    """One independent-condition representation for stored and newly authored alerts."""
    policy = normalize_detection_policy(policy)
    kind = policy["kind"]
    if kind == DETECTION_POLICY_CONDITIONS:
        return policy
    return {
        "kind": DETECTION_POLICY_CONDITIONS,
        "entered": kind
        in {DETECTION_POLICY_NEW_MATCHES, DETECTION_POLICY_MEMBERSHIP_CHANGED},
        "exited": kind == DETECTION_POLICY_MEMBERSHIP_CHANGED,
        "count_changed": kind == DETECTION_POLICY_COUNT_CHANGED,
        "fields": policy.get("fields", []),
        **({"threshold": policy} if kind == DETECTION_POLICY_THRESHOLD_CROSSED else {}),
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
    allowed = _PREPARED_ANCHOR_KINDS if allow_row_cell else _ROW_ANCHOR_KINDS
    if anchor_kind not in allowed:
        if allow_row_cell:
            raise ValueError(
                "embedding_similarity view anchor must be a row, row_cell, or "
                "manual_text_query; vector_ref is not re-resolvable"
            )
        raise ValueError(
            "embedding_similarity watch anchor must be a row (anchors the whole-row "
            "vector); manual_text_query/vector_ref re-embed per run and row_cell is "
            "not column-specific yet, so neither is watchable"
        )
    if anchor_kind == "manual_text_query":
        # Store canonical
        # `terms` (single `text` -> one term weight 1.0) and, when present, canonical
        # `exclude` (hard NOT) — hash-stable, and the raw query string never leaks into
        # the stored spec. Shared with the similarity resolver. A query
        # with no `exclude` stores no `exclude` field, so query cache identities stay stable.
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
        # Preserve the cell binding; the resolver verifies the prepared index
        # represents exactly that column.
        if anchor_kind == "row_cell" and anchor.get("column_id") is not None:
            normalized_anchor["column_id"] = _positive_int(
                anchor["column_id"], "anchor.column_id"
            )
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


_LOCAL_RETRIEVAL_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "scope",
        "min_score",
        "query_text",
        "source_anchor",
        "space_id",
    }
)


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _finite_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _normalize_source_revision(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("source_anchor.source must be an object")
    unknown = set(raw) - {
        "source_id",
        "source_kind",
        "version_kind",
        "version_value",
        "content_hash",
    }
    if unknown:
        raise ValueError(
            f"unknown source_anchor.source fields: {', '.join(sorted(unknown))}"
        )
    version_value = raw.get("version_value")
    if isinstance(version_value, bool):
        raise ValueError(
            "source_anchor.source.version_value must be positive int or string"
        )
    if isinstance(version_value, int):
        version_value = _positive_int(
            version_value, "source_anchor.source.version_value"
        )
    elif isinstance(version_value, str):
        version_value = _require_nonempty_str(
            version_value, "source_anchor.source.version_value"
        )
    else:
        raise ValueError(
            "source_anchor.source.version_value must be a positive integer or string"
        )
    out: dict[str, Any] = {
        "source_id": _require_nonempty_str(
            raw.get("source_id"), "source_anchor.source.source_id"
        ),
        "source_kind": _require_nonempty_str(
            raw.get("source_kind"), "source_anchor.source.source_kind"
        ),
        "version_kind": _require_nonempty_str(
            raw.get("version_kind"), "source_anchor.source.version_kind"
        ),
        "version_value": version_value,
    }
    if raw.get("content_hash") is not None:
        out["content_hash"] = _require_nonempty_str(
            raw["content_hash"], "source_anchor.source.content_hash"
        )
    return out


def _normalize_text_range(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("source_anchor.locator.text must be an object")
    unknown = set(raw) - {
        "text_surface_id",
        "content_hash",
        "start",
        "end",
        "offset_unit",
    }
    if unknown:
        raise ValueError(f"unknown locator.text fields: {', '.join(sorted(unknown))}")
    start = raw.get("start")
    end = raw.get("end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
    ):
        raise ValueError("source_anchor.locator.text must satisfy 0 <= start < end")
    unit = raw.get("offset_unit", "unicode_codepoint")
    if unit != "unicode_codepoint":
        raise ValueError(
            "source_anchor.locator.text.offset_unit must be unicode_codepoint"
        )
    return {
        "text_surface_id": _require_nonempty_str(
            raw.get("text_surface_id"), "source_anchor.locator.text.text_surface_id"
        ),
        "content_hash": _require_nonempty_str(
            raw.get("content_hash"), "source_anchor.locator.text.content_hash"
        ),
        "start": start,
        "end": end,
        "offset_unit": "unicode_codepoint",
    }


def _normalize_page_range(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"start", "end"}:
        raise ValueError("source_anchor.locator.pages must be {start, end}")
    start = _positive_int(raw.get("start"), "source_anchor.locator.pages.start")
    end = _positive_int(raw.get("end"), "source_anchor.locator.pages.end")
    if end < start:
        raise ValueError("source_anchor.locator.pages.end must not be before start")
    return {"start": start, "end": end}


def _normalize_time_range(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"start_seconds", "end_seconds"}:
        raise ValueError(
            "source_anchor.locator.time must be {start_seconds, end_seconds}"
        )
    start = _finite_number(
        raw.get("start_seconds"), "source_anchor.locator.time.start_seconds"
    )
    end = _finite_number(
        raw.get("end_seconds"), "source_anchor.locator.time.end_seconds"
    )
    if start < 0 or end < start:
        raise ValueError(
            "source_anchor.locator.time must satisfy 0 <= start_seconds <= end_seconds"
        )
    return {"start_seconds": start, "end_seconds": end}


def _normalize_source_locator(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("source_anchor.locator must be an object")
    unknown = set(raw) - {"kind", "text", "pages", "time", "source_span_ids"}
    if unknown:
        raise ValueError(
            f"unknown source_anchor.locator fields: {', '.join(sorted(unknown))}"
        )
    out: dict[str, Any] = {
        "kind": _require_nonempty_str(raw.get("kind"), "source_anchor.locator.kind"),
    }
    if raw.get("text") is not None:
        out["text"] = _normalize_text_range(raw["text"])
    if raw.get("pages") is not None:
        out["pages"] = _normalize_page_range(raw["pages"])
    if raw.get("time") is not None:
        out["time"] = _normalize_time_range(raw["time"])
    if raw.get("source_span_ids") is not None:
        spans = raw["source_span_ids"]
        if not isinstance(spans, list):
            raise ValueError("source_anchor.locator.source_span_ids must be a list")
        out["source_span_ids"] = [
            _require_nonempty_str(span, "source_anchor.locator.source_span_ids[]")
            for span in spans
        ]
    return out


def _normalize_scoped_binding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("source_anchor.binding must be an object")
    unknown = set(raw) - {
        "binding_id",
        "sheet_id",
        "row_id",
        "column_id",
        "dependencies",
    }
    if unknown:
        raise ValueError(
            f"unknown source_anchor.binding fields: {', '.join(sorted(unknown))}"
        )
    out: dict[str, Any] = {
        "binding_id": _require_nonempty_str(
            raw.get("binding_id"), "source_anchor.binding.binding_id"
        ),
        "sheet_id": _positive_int(
            raw.get("sheet_id"), "source_anchor.binding.sheet_id"
        ),
        "row_id": _positive_int(raw.get("row_id"), "source_anchor.binding.row_id"),
    }
    if raw.get("column_id") is not None:
        out["column_id"] = _positive_int(
            raw["column_id"], "source_anchor.binding.column_id"
        )
    if raw.get("dependencies") is not None:
        deps = raw["dependencies"]
        if not isinstance(deps, list):
            raise ValueError("source_anchor.binding.dependencies must be a list")
        normalized_deps: list[dict[str, int]] = []
        for dep in deps:
            if not isinstance(dep, dict) or set(dep) - {
                "row_id",
                "column_id",
                "retrieval_revision",
            }:
                raise ValueError(
                    "source_anchor.binding dependency must be {row_id, column_id, retrieval_revision}"
                )
            rev = dep.get("retrieval_revision")
            if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
                raise ValueError("retrieval_revision must be a non-negative integer")
            normalized_deps.append(
                {
                    "row_id": _positive_int(dep.get("row_id"), "dependency.row_id"),
                    "column_id": _positive_int(
                        dep.get("column_id"), "dependency.column_id"
                    ),
                    "retrieval_revision": rev,
                }
            )
        out["dependencies"] = normalized_deps
    return out


def _normalize_source_anchor(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("local_retrieval source_anchor must be an object")
    unknown = set(raw) - {"source", "locator", "binding", "exclude_exact"}
    if unknown:
        raise ValueError(
            f"unknown local_retrieval source_anchor fields: {', '.join(sorted(unknown))}"
        )
    exclude_exact = raw.get("exclude_exact", True)
    if not isinstance(exclude_exact, bool):
        raise ValueError(
            "local_retrieval source_anchor.exclude_exact must be a boolean"
        )
    out: dict[str, Any] = {
        "source": _normalize_source_revision(raw.get("source")),
        "locator": _normalize_source_locator(raw.get("locator")),
        "exclude_exact": exclude_exact,
    }
    if raw.get("binding") is not None:
        out["binding"] = _normalize_scoped_binding(raw["binding"])
    return out


def _normalize_local_retrieval_similarity_query(
    query: dict[str, Any], *, scope: dict[str, Any] | None
) -> dict[str, Any]:
    unknown = set(query) - _LOCAL_RETRIEVAL_FIELDS
    if unknown:
        raise ValueError(
            f"unknown local_retrieval similarity fields: {', '.join(sorted(unknown))}"
        )
    raw_min_score = query.get("min_score")
    if raw_min_score is None:
        raise ValueError(
            "local_retrieval similarity requires an explicit min_score threshold in (0, 1]"
        )
    min_score = _finite_number(raw_min_score, "local_retrieval min_score")
    if not 0.0 < min_score <= 1.0:
        raise ValueError("local_retrieval min_score must be in (0, 1]")
    query_text = query.get("query_text")
    source_anchor = query.get("source_anchor")
    if query_text is None and source_anchor is None:
        raise ValueError(
            "local_retrieval similarity requires query_text and/or source_anchor"
        )
    normalized: dict[str, Any] = {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": LOCAL_RETRIEVAL_SIMILARITY_KIND,
        "scope": _normalize_scope(query.get("scope") or scope, default_kind="project"),
        "min_score": min_score,
    }
    if query_text is not None:
        normalized["query_text"] = _require_nonempty_str(
            query_text, "local_retrieval query_text"
        )
    if source_anchor is not None:
        normalized["source_anchor"] = _normalize_source_anchor(source_anchor)
    if query.get("space_id") is not None:
        normalized["space_id"] = _require_nonempty_str(
            query["space_id"], "local_retrieval space_id"
        )
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
