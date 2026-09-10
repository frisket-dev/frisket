"""Resolve an embedding index's source row scope — shared by the refresh action,
freshness, and any watch/server caller.

Extracted from the executor's private ``_scope_rows`` so watch/server code can ask
"which rows are in this index's stored source scope?" WITHOUT importing
executor-private functions. One definition keeps the action and the freshness/UI
views in agreement about what "should be embedded".
"""

from __future__ import annotations

import json
from typing import Any

from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.engine.store import Project
from frisket.features.watchlists.specs import canonical_json, normalize_query_spec

# resolve_sheet_filter_rows is LIMIT-bounded; a cap far above any real sheet means
# "every row in scope".
_ALL_ROWS_LIMIT = 1_000_000_000


class IndexScopeError(ValueError):
    """A stored source scope could not be resolved. Carries the typed code callers
    map onto their own surface (refresh action error / watch binding error)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_obj(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_scope_rows(
    project: Project, sheet_id: int, spec: dict[str, Any]
) -> list[int]:
    """Ordered visible row ids for a scope spec against ``sheet_id``.

    Empty spec ``{}`` means all visible rows. A non-empty spec must be a
    ``sheet.filter`` QuerySpec whose scope sheet_id matches; anything else raises
    ``IndexScopeError``."""
    if not spec:
        return list(project.visible_row_ids(sheet_id))
    try:
        normalized = normalize_query_spec(spec)
    except ValueError as exc:
        raise IndexScopeError("invalid_query_spec", str(exc)) from exc
    if normalized.get("kind") != "sheet.filter":
        raise IndexScopeError(
            "embedding_source_unsupported",
            f"unsupported query kind {normalized.get('kind')!r}; "
            "only sheet.filter scopes can be embedded",
        )
    scope = normalized.get("scope") or {}
    q_sheet = scope.get("sheet_id")
    if q_sheet != sheet_id:
        raise IndexScopeError(
            "invalid_query_spec",
            f"query sheet_id {q_sheet} does not match index sheet_id {sheet_id}",
        )
    filter_json = canonical_json(normalized.get("filter", {}))
    sort_json = canonical_json(normalized["sort"]) if "sort" in normalized else None
    try:
        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=_ALL_ROWS_LIMIT,
            offset=0,
        )
    except SheetRowSetError as exc:
        raise IndexScopeError("invalid_query_spec", str(exc)) from exc
    return list(rowset.row_ids)


def resolve_index_scope_rows(project: Project, index: Any) -> list[int]:
    """Ordered visible row ids in an index's STORED source scope — the same scope a
    full refresh embeds. Empty when the index has no sheet."""
    sheet_id = index["sheet_id"]
    if sheet_id is None:
        return []
    return resolve_scope_rows(
        project, int(sheet_id), _safe_obj(index["source_query_json"])
    )
