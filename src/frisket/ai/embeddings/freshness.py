"""Embedding index freshness: is an index fully current for its source scope?

Two ways a stored vector set drifts from the source:
- a READY row's source content CHANGED (its stored ``source_hash`` is now stale);
- a row entered the index's source scope and was never embedded (MISSING).

Both are judged with the SAME source payload/hash definition as refresh /
similarity / export (``build_source_payloads``) and the same stored-scope
resolution the refresh action uses (``resolve_index_scope_rows``), so a "refresh
needed" list view and a watch never disagree with what a refresh would do.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from frisket.ai.embeddings.scope import IndexScopeError, resolve_index_scope_rows
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.ai.embeddings.vector_backend import VectorBackend
from frisket.engine.store import Project


# refresh_needed reason codes, highest-priority first. 'fresh' means the stored
# vectors fully cover the current source scope.
FRESHNESS_FRESH = "fresh"
FRESHNESS_SCOPE_INVALID = "scope_invalid"
FRESHNESS_NEVER_REFRESHED = "never_refreshed"
FRESHNESS_MISSING_ROWS = "missing_rows"
FRESHNESS_STALE_ROWS = "stale_rows"
FRESHNESS_ERRORED_ITEMS = "errored_items"


@dataclass(frozen=True)
class IndexFreshness:
    ready_keys: set[str]
    # ready items whose CURRENT source content no longer matches their vector
    # (changed, or content now empty).
    stale_keys: list[str] = field(default_factory=list)
    # embeddable rows in the stored source scope that are NOT yet ready.
    missing_keys: list[str] = field(default_factory=list)
    # False when the stored source_query could not be resolved (corrupt scope).
    scope_resolved: bool = True
    # how many rows SHOULD be embedded right now = embeddable rows in the current
    # source scope (not the last materialized total_items, which is stale after an
    # append/delete). Falls back to the ready count when the scope can't resolve.
    target_count: int = 0


def freshness_reason(index: Any, fresh: IndexFreshness) -> str:
    """One reason code for why an index does (not) need a refresh, in priority
    order: an unresolvable scope or a never-refreshed index dominate; then missing
    rows, then stale rows, then errored items; else fresh."""
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
    """The typed error code an export / download should surface for a freshness
    reason, or None when the index is fresh (proceed). Shared so the export action
    and the download route agree on the contract.

    - changed ready rows -> embedding_source_stale (preserve existing behavior);
    - missing/errored/never-refreshed rows the artifact would omit ->
      embedding_index_incomplete;
    - an unresolvable stored scope -> embedding_index_scope_invalid.
    """
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
    project: Project, index_id: Any, *, backend: VectorBackend | None = None
) -> tuple[str, str] | None:
    """Freshness gate for PRODUCT-FACING embedding_similarity SEARCH paths (the
    query-preview spine, a lens resolve, and the Show-Similar route). Returns
    ``(error_code, message)`` when the index is unknown or not fresh enough to search
    — so Show Similar / a saved lens never silently searches a PARTIAL/stale index —
    else None when it's safe to resolve.

    Codes align with the export/download product contract via ``freshness_error_code``:
    unknown -> ``embedding_index_not_found``; unresolvable scope ->
    ``embedding_index_scope_invalid``; missing/never-refreshed/errored rows ->
    ``embedding_index_incomplete``; changed ready rows -> ``embedding_source_stale``.
    This is a PRODUCT-PATH gate, deliberately NOT inside the low-level resolver.
    """
    from frisket.ai.embeddings.store import EmbeddingStore  # local: avoid import cycle

    index = EmbeddingStore(project).get_index(index_id)
    if index is None:
        return ("embedding_index_not_found", f"no embedding index {index_id!r}")
    reason = freshness_reason(index, index_freshness(project, index, backend=backend))
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
    return [str(c) for c in parsed] if isinstance(parsed, list) else []


def _ready_sidecar_hashes(
    project: Project, index_id: str, *, backend: VectorBackend | None = None
) -> dict[str, str]:
    owned_backend = backend is None
    backend = backend or VectorBackend(project)
    if not backend.db_path.exists():
        return {}
    try:
        rows = backend.db.execute(
            "SELECT source_key, source_hash FROM embedding_items "
            "WHERE index_id=? AND status='ready'",
            (index_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        if owned_backend:
            backend.close()
    return {row["source_key"]: row["source_hash"] for row in rows}


def _row_ids(keys: list[str]) -> list[int]:
    out: list[int] = []
    for key in keys:
        try:
            out.append(int(key))
        except (TypeError, ValueError):
            continue
    return out


def index_freshness(
    project: Project, index: Any, *, backend: VectorBackend | None = None
) -> IndexFreshness:
    """Compare an index's READY sidecar vectors against the CURRENT source scope.

    The STALE check needs only the ready keys (recompute their current hash) and
    never raises. The MISSING check needs the stored scope; if that can't resolve
    (corrupt source_query) ``scope_resolved`` is False and ``missing_keys`` is left
    empty rather than guessing."""
    source_columns = _safe_columns(index)
    ready = _ready_sidecar_hashes(project, index["id"], backend=backend)
    ready_keys = set(ready)

    # stale: ready (row) keys whose current content hash drifted from the stored
    # one (a None current hash = content now empty = stale). A HIDDEN ready row is
    # NOT stale — it is dormant (search/scope already exclude it) and its vector
    # still matches its present content; build_source_payloads omits hidden rows, so
    # without this guard a hidden ready row would look stale and wrongly block.
    sheet_id_for_vis = index["sheet_id"]
    visible_rows = (
        set(project.visible_row_ids(sheet_id_for_vis))
        if sheet_id_for_vis is not None
        else None
    )

    def _key_visible(key: str) -> bool:
        if visible_rows is None:
            return True
        try:
            return int(key) in visible_rows
        except (TypeError, ValueError):
            return True

    current_for_ready = {
        payload["source_key"]: payload["source_hash"]
        for payload in build_source_payloads(
            project, index, source_columns, _row_ids(list(ready_keys))
        )
    }
    stale = sorted(
        key
        for key in (str(rid) for rid in _row_ids(list(ready_keys)))
        if _key_visible(key) and current_for_ready.get(key) != ready.get(key)
    )

    # missing: embeddable rows in the stored scope with no ready vector yet.
    # target_count: how many rows the current scope says SHOULD be embedded.
    missing: list[str] = []
    scope_resolved = True
    target_count = len(ready_keys)
    try:
        scope_rows = resolve_index_scope_rows(project, index)
        current_keys = {
            payload["source_key"]
            for payload in build_source_payloads(
                project, index, source_columns, scope_rows
            )
        }
        missing = sorted(current_keys - ready_keys)
        target_count = len(current_keys)
    except IndexScopeError:
        scope_resolved = False

    return IndexFreshness(
        ready_keys=ready_keys,
        stale_keys=stale,
        missing_keys=missing,
        scope_resolved=scope_resolved,
        target_count=target_count,
    )


def stale_ready_source_keys(
    project: Project, index: Any, source_columns: list[str] | None = None
) -> list[str]:
    """Back-compat: just the CHANGED-ready keys (use ``index_freshness`` for the
    full picture incl. missing rows). ``source_columns`` is ignored — read from the
    index for one definition."""
    return index_freshness(project, index).stale_keys
