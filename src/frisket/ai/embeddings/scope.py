"""Resolve an embedding index's source row scope — shared by the refresh action,
freshness, and any watch/server caller.

Extracted from the executor's private ``_scope_rows`` so watch/server code can ask
"which rows are in this index's stored source scope?" WITHOUT importing
executor-private functions. One definition keeps the action and the freshness/UI
views in agreement about what "should be embedded".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from frisket.engine.store import Project
from frisket.features.watchlists.specs import canonical_json, normalize_query_spec
from frisket.querysets import (
    SheetRowScopePlan,
    SheetRowSetError,
    execute_sheet_row_page_without_total,
    sheet_row_scope_plan,
)

DEFAULT_SCOPE_PAGE_SIZE = 500


class IndexScopeError(ValueError):
    """A stored source scope could not be resolved. Carries the typed code callers
    map onto their own surface (refresh action error / watch binding error)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _stable_plan(plan: SheetRowScopePlan) -> SheetRowScopePlan:
    """Add a unique final key for every OFFSET-paged named-index scope."""

    if plan.order_parts:
        # The shared sorted-plan compiler already appends position + id.
        return plan
    return replace(plan, order_parts=["r.position ASC", "r.id ASC"])


def _safe_obj(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scope_plan(
    project: Project, sheet_id: int, spec: dict[str, Any]
) -> SheetRowScopePlan:
    """Compile one stored named-index source scope.

    Empty spec ``{}`` means all visible rows. A non-empty spec must be a
    ``sheet.filter`` QuerySpec whose scope sheet_id matches; anything else raises
    ``IndexScopeError``."""
    if not spec:
        try:
            return _stable_plan(sheet_row_scope_plan(project, sheet_id))
        except SheetRowSetError as exc:
            raise IndexScopeError("invalid_query_spec", str(exc)) from exc
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
        return _stable_plan(
            sheet_row_scope_plan(
                project,
                sheet_id,
                filter_=filter_json,
                sort=sort_json,
            )
        )
    except SheetRowSetError as exc:
        raise IndexScopeError("invalid_query_spec", str(exc)) from exc


def iter_scope_row_pages(
    project: Project,
    sheet_id: int,
    spec: dict[str, Any],
    *,
    page_size: int = DEFAULT_SCOPE_PAGE_SIZE,
) -> Iterator[list[int]]:
    """Yield ordered row-id pages without materializing the full source scope."""

    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError("scope page size must be a positive integer")
    plan = scope_plan(project, sheet_id, spec)
    offset = 0
    while True:
        rows = execute_sheet_row_page_without_total(
            project, plan, limit=page_size, offset=offset
        )
        if not rows:
            return
        yield [int(row["id"]) for row in rows]
        if len(rows) < page_size:
            return
        offset += len(rows)


def resolve_scope_rows(
    project: Project, sheet_id: int, spec: dict[str, Any]
) -> list[int]:
    """Materialize a complete scope for explicit all-row action boundaries."""

    return [
        row_id
        for page in iter_scope_row_pages(project, sheet_id, spec)
        for row_id in page
    ]


def resolve_index_scope_rows(project: Project, index: Any) -> list[int]:
    """Ordered visible row ids in an index's STORED source scope — the same scope a
    full refresh embeds. Empty when the index has no sheet."""
    sheet_id = index["sheet_id"]
    if sheet_id is None:
        return []
    return resolve_scope_rows(
        project, int(sheet_id), _safe_obj(index["source_query_json"])
    )


def index_scope_plan(project: Project, index: Any) -> SheetRowScopePlan | None:
    """Compile an index's stored source scope, or None for an unbound index."""

    sheet_id = index["sheet_id"]
    if sheet_id is None:
        return None
    return scope_plan(project, int(sheet_id), _safe_obj(index["source_query_json"]))


def iter_index_scope_row_pages(
    project: Project, index: Any, *, page_size: int = DEFAULT_SCOPE_PAGE_SIZE
) -> Iterator[list[int]]:
    """Yield an index's source rows in stable stored-scope order."""

    sheet_id = index["sheet_id"]
    if sheet_id is None:
        return
    yield from iter_scope_row_pages(
        project,
        int(sheet_id),
        _safe_obj(index["source_query_json"]),
        page_size=page_size,
    )
