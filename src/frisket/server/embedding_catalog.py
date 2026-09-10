"""Embedding provider/model picker contract.

A narrow projection of ``frisket.embeddings.embedding_capabilities`` for a
provider/model picker: runnable engines are surfaced as available or
disabled-with-a-reason, while reserved engines that have no implementation are
omitted. Remote engines carry their privacy/egress note so the picker can warn
before a remote index is created.

Backend/contract only — no UI. The helper is route-free so it is testable without
a TestClient; the server route just wraps it with the per-project router.
"""

from __future__ import annotations

import json
from typing import Any

from frisket.ai.embeddings import (
    REMOTE_PROVIDER_KINDS,
    EmbeddingStore,
    embedding_capabilities,
)
from frisket.ai.embeddings.freshness import freshness_reason, index_freshness

EMBEDDING_PROVIDER_CATALOG_SCHEMA = "frisket.embedding_provider_catalog.v1"
EMBEDDING_INDEX_LIST_SCHEMA = "frisket.embedding_index_list.v1"

# A source column's storage type implies the embedding modality the picker should
# offer. Text-ish columns embed as text; media columns embed in their modality.
SOURCE_COLUMN_TYPE_MODALITY: dict[str, str] = {
    "text": "text",
    "category": "text",
    "link": "text",
    "url": "text",
    "markdown": "text",
    "json": "text",
    "integer": "text",
    "number": "text",
    "boolean": "text",
    "date": "text",
    "image": "image",
    "audio": "audio",
    "video": "video",
    "file": "file",
}


def embedding_provider_catalog_payload(
    *,
    router: Any = None,
    env: dict[str, str] | None = None,
    local_available: bool | None = None,
    modality: str | None = None,
    source_column_type: str | None = None,
) -> dict[str, Any]:
    """Compatible embedding providers/models for a modality (or a source column
    type). ``modality`` wins over ``source_column_type`` when both are given.
    Each provider entry is an EmbeddingCapability plus a ``disabled_reason`` alias
    (= ``error`` when not available, else None)."""
    resolved_modality = modality
    if resolved_modality is None and source_column_type is not None:
        resolved_modality = SOURCE_COLUMN_TYPE_MODALITY.get(source_column_type)

    caps = embedding_capabilities(
        router=router,
        env=env,
        local_available=local_available,
        include_unbuilt=False,
    )
    # Show ALL models, but mark the ones incompatible with the column's modality as
    # disabled (the UI greys them out with a reason) rather than hiding them — so the
    # picker is honest about what exists and why it can't be used here.
    providers = []
    for cap in caps:
        compatible = resolved_modality is None or resolved_modality in cap["modalities"]
        if not cap["available"]:
            reason = cap["error"]
        elif not compatible:
            reason = f"this model can't embed {resolved_modality} columns"
        else:
            reason = None
        providers.append(
            {**cap, "disabled_reason": reason, "modality_compatible": compatible}
        )
    return {
        "schema_version": EMBEDDING_PROVIDER_CATALOG_SCHEMA,
        "modality": resolved_modality,
        "source_column_type": source_column_type,
        "providers": providers,
    }


def _safe_obj(value: Any) -> dict[str, Any]:
    """Parse a stored JSON object, defaulting to {} on malformed/non-object data —
    one corrupt index row must not 500 the whole list endpoint."""
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _pending_refresh_job_id(
    queue: Any,
    project_id: str,
    index_id: str,
    *,
    storage_org_id: int | None = None,
) -> int | None:
    """The id of a queued/running embedding.index_refresh job for this index, if
    any (the dedupe_key embeds index+trigger, so filter by the payload index_id)."""
    if queue is None or not project_id:
        return None
    from frisket.engine.jobs import EMBEDDING_REFRESH_KIND

    try:
        jobs = queue.list_project_jobs(
            project_id,
            storage_org_id=storage_org_id,
            kind=EMBEDDING_REFRESH_KIND,
            limit=1000,
        )
    except Exception:  # noqa: BLE001 — a read for UI status must not 500 the list
        return None
    for job in jobs:
        if job.status in ("queued", "running") and (
            (job.payload or {}).get("index_id") == index_id
        ):
            return job.id
    return None


def _index_payload(
    store: EmbeddingStore,
    index: Any,
    *,
    queue: Any = None,
    project_id: str | None = None,
    storage_org_id: int | None = None,
) -> dict[str, Any]:
    space = store.get_space(index["space_id"])
    provider_kind = space["provider_kind"] if space else None
    policy = _safe_obj(index["provider_policy_json"])
    maintenance = _safe_obj(index["maintenance_policy_json"])
    source_columns = [str(c) for c in _safe_list(index["source_columns_json"])]
    error_items = index["error_items"]
    ready_items = index["ready_items"]
    # Freshness is judged by recomputing the current source hash + comparing the
    # stored vectors against the CURRENT source scope (the same definition
    # refresh/similarity/export use), NOT the stale_items counter: a source cell
    # edited after embedding leaves the item 'ready' with an old hash, and a newly
    # appended row is simply missing a vector.
    fresh = index_freshness(store.project, index)
    stale_source = len(fresh.stale_keys)
    missing_source = len(fresh.missing_keys)
    ready_keys = len(fresh.ready_keys)
    maintenance_mode = maintenance.get("mode", "manual")
    reason = freshness_reason(index, fresh)
    # First-class freshness STATE contract: counts + reason code +
    # last/pending refresh job + maintenance mode. The top-level *_items /
    # refresh_needed below remain for back-compat.
    freshness_block = {
        "reason": reason,
        "refresh_needed": reason != "fresh",
        # ready = have a vector; current = vector still matches source.
        "ready": ready_keys,
        "current": ready_keys - stale_source,
        "missing": missing_source,
        "stale": stale_source,
        "error": error_items,
        # the CURRENT source scope target, not the last materialized total_items
        # (which is stale after an append/delete).
        "total": fresh.target_count,
        "scope_resolved": fresh.scope_resolved,
        "maintenance_mode": maintenance_mode,
        "last_refreshed_at": index["last_refreshed_at"],
        "last_refresh_job_id": index["last_refresh_job_id"],
        "last_refresh_receipt_id": index["last_refresh_receipt_id"],
        "pending_refresh_job_id": _pending_refresh_job_id(
            queue,
            project_id or "",
            index["id"],
            storage_org_id=storage_org_id,
        ),
    }
    return {
        "freshness": freshness_block,
        "index_id": index["id"],
        "name": index["name"],
        "sheet_id": index["sheet_id"],
        # space_id kept for provenance/details, not the primary UI surface.
        "space_id": index["space_id"],
        "modality": space["modality"] if space else None,
        "provider_id": space["provider_id"] if space else None,
        "provider_kind": provider_kind,
        "model_id": space["actual_model_id"] if space else None,
        "dimension": space["dimension"] if space else None,
        "distance_metric": space["distance_metric"] if space else None,
        "source_columns": source_columns,
        "status": index["status"],
        # total = how many rows the CURRENT source scope says should be embedded
        # (so the UI doesn't show "2/2 ready" right after a 3rd row was appended).
        "total_items": fresh.target_count,
        "ready_items": ready_items,
        "stale_items": index["stale_items"],
        # ready items whose current source content no longer matches their vector.
        "stale_source_items": stale_source,
        # embeddable rows in the source scope with no vector yet (e.g. appended).
        "missing_source_items": missing_source,
        "error_items": error_items,
        # a refresh is "needed" when source content drifted from a stored vector, a
        # new embeddable row is unembedded, any item errored, or nothing is
        # materialized yet (never refreshed).
        "refresh_needed": bool(
            stale_source
            or missing_source
            or error_items
            or index["last_refreshed_at"] is None
        ),
        "last_refreshed_at": index["last_refreshed_at"],
        "remote": provider_kind in REMOTE_PROVIDER_KINDS,
        # Known leaves stay verbatim when present, even if a legacy row has a
        # corrupt type. The client can then repair them explicitly; coercing a
        # truthy string here could falsely represent consent. Unknown keys stay
        # private to the stored JSON object.
        "provider_policy": {
            "allow_remote": policy.get("allow_remote", False),
            "allow_remote_automatic_refresh": policy.get(
                "allow_remote_automatic_refresh", False
            ),
            "max_cost_usd_per_refresh": policy.get("max_cost_usd_per_refresh", None),
        },
        "maintenance": {
            "mode": maintenance.get("mode", "manual"),
            "schedule": maintenance.get("schedule", None),
        },
    }


def embedding_index_list_payload(
    project: Any,
    sheet_id: int | None = None,
    *,
    queue: Any = None,
    project_id: str | None = None,
    storage_org_id: int | None = None,
) -> dict[str, Any]:
    """Existing embedding indexes (optionally scoped to one sheet) for the UI:
    provider/model + a first-class freshness state block + refresh-needed state.
    Pass ``queue`` + ``project_id`` to surface a pending refresh job. space_id is
    present for provenance/details but is not the primary surface."""
    store = EmbeddingStore(project)
    indexes = [
        _index_payload(
            store,
            idx,
            queue=queue,
            project_id=project_id,
            storage_org_id=storage_org_id,
        )
        for idx in store.list_indexes(sheet_id)
    ]
    return {
        "schema_version": EMBEDDING_INDEX_LIST_SCHEMA,
        "sheet_id": sheet_id,
        "indexes": indexes,
    }
