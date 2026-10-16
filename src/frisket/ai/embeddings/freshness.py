"""Freshness of named embedding indexes in the unified retrieval sidecar."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from frisket.ai.embeddings.scope import IndexScopeError, resolve_index_scope_rows
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.engine.store import Project
from frisket.retrieval.store import RetrievalStore

from .unified_index import dataset_rows, plan_named_index_items

FRESHNESS_FRESH = "fresh"
FRESHNESS_SCOPE_INVALID = "scope_invalid"
FRESHNESS_NEVER_REFRESHED = "never_refreshed"
FRESHNESS_MISSING_ROWS = "missing_rows"
FRESHNESS_STALE_ROWS = "stale_rows"
FRESHNESS_ERRORED_ITEMS = "errored_items"


@dataclass(frozen=True)
class IndexFreshness:
    ready_keys: set[str]
    stale_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    scope_resolved: bool = True
    target_count: int = 0


def freshness_reason(index: Any, fresh: IndexFreshness) -> str:
    if not fresh.scope_resolved:
        return FRESHNESS_SCOPE_INVALID
    if index["last_refreshed_at"] is None:
        return FRESHNESS_NEVER_REFRESHED
    if fresh.missing_keys:
        return FRESHNESS_MISSING_ROWS
    if fresh.stale_keys:
        return FRESHNESS_STALE_ROWS
    if index["error_items"]:
        return FRESHNESS_ERRORED_ITEMS
    return FRESHNESS_FRESH


def freshness_error_code(reason: str) -> str | None:
    if reason == FRESHNESS_STALE_ROWS:
        return "embedding_source_stale"
    if reason in (
        FRESHNESS_MISSING_ROWS,
        FRESHNESS_ERRORED_ITEMS,
        FRESHNESS_NEVER_REFRESHED,
    ):
        return "embedding_index_incomplete"
    if reason == FRESHNESS_SCOPE_INVALID:
        return "embedding_index_scope_invalid"
    return None


def embedding_search_freshness_error(
    project: Project, index_id: Any
) -> tuple[str, str] | None:
    from frisket.ai.embeddings.store import EmbeddingStore

    index = EmbeddingStore(project).get_index(index_id)
    if index is None:
        return ("embedding_index_not_found", f"no embedding index {index_id!r}")
    reason = freshness_reason(index, index_freshness(project, index))
    code = freshness_error_code(reason)
    if code is None:
        return None
    return (
        code,
        f"the embedding index is not fresh enough to search ({reason}); "
        "refresh it before searching",
    )


def _safe_columns(index: Any) -> list[str]:
    try:
        parsed = json.loads(index["source_columns_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(column) for column in parsed] if isinstance(parsed, list) else []


def index_freshness(project: Project, index: Any) -> IndexFreshness:
    """Compare deterministic current members with the published named dataset."""

    try:
        scope_rows = resolve_index_scope_rows(project, index)
    except IndexScopeError:
        return IndexFreshness(ready_keys=set(), scope_resolved=False)

    from .store import EmbeddingStore

    space = EmbeddingStore(project).get_space(index["space_id"])
    if space is None:
        return IndexFreshness(ready_keys=set(), scope_resolved=False)
    payloads = build_source_payloads(project, index, _safe_columns(index), scope_rows)
    try:
        _recipe, planned = plan_named_index_items(
            project, index=index, space=space, payloads=payloads
        )
    except (KeyError, TypeError, ValueError):
        return IndexFreshness(ready_keys=set(), scope_resolved=False)

    current = {
        item.row_id: (item.part_id, item.representation_id, item.source_key)
        for item in planned
    }
    with RetrievalStore(project) as retrieval:
        stored = dataset_rows(retrieval, str(index["id"]))
    ready_keys = {
        source_key
        for row_id, (part, representation, source_key) in current.items()
        if stored.get(row_id) == (part, representation)
    }
    stale_keys = sorted(
        source_key
        for row_id, (_part, _representation, source_key) in current.items()
        if row_id in stored and source_key not in ready_keys
    )
    missing_keys = sorted(
        source_key
        for row_id, (_part, _representation, source_key) in current.items()
        if row_id not in stored
    )
    return IndexFreshness(
        ready_keys=ready_keys,
        stale_keys=stale_keys,
        missing_keys=missing_keys,
        scope_resolved=True,
        target_count=len(current),
    )


def stale_ready_source_keys(
    project: Project, index: Any, source_columns: list[str] | None = None
) -> list[str]:
    del source_columns
    return index_freshness(project, index).stale_keys
