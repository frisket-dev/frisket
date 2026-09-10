"""Queued ``watch.evaluate`` jobs — dependency-aware watch evaluation.

The source-append pipeline must sequence: source poll -> embedding refresh -> watch
evaluation. The source poll evaluates non-embedding watches inline, but an
``embedding_similarity`` watch must wait until its index's refresh job COMPLETES (or
it blocks on ``embedding_index_incomplete`` because the appended rows aren't
embedded yet, and never re-fires). So the embedding-refresh worker enqueues one
``watch.evaluate`` job per dependent watch on completion, and this handler runs it.

Reuses the generic SqliteJobQueue — no new automation framework. The watch
evaluator itself never calls a provider (row anchors only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.store import Project
from frisket.engine.jobs.notifications_delivery import (
    enqueue_notification_emit_result,
)
from frisket.features.watchlists.service import run_watch_evaluation

from .queue import JobQueue, claimed_project_location
from .project_opener import ProjectOpener, claimed_opener_key
from .ports import JobHandlerContext
from .worker import HandlerRegistration, HandlerRegistry

WATCH_EVALUATE_KIND = "watch.evaluate"


def _watch_query(row: Any) -> dict[str, Any]:
    raw = row["query"]
    if isinstance(raw, dict):
        return raw
    try:
        query = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return query if isinstance(query, dict) else {}


def find_embedding_similarity_watches(project: Project, index_id: str) -> list[Any]:
    """Enabled embedding_similarity watches bound to ``index_id``."""
    out: list[Any] = []
    for watch in project.watches():
        if not bool(watch["enabled"]):
            continue
        query = _watch_query(watch)
        if str(query.get("kind") or "").strip().lower() != "embedding_similarity":
            continue
        if str(query.get("embedding_index_id") or "") == index_id:
            out.append(watch)
    return out


def _watch_eval_dedupe_key(watch_id: int, trigger_ref: dict[str, Any] | None) -> str:
    """Stable per (watch, trigger) so a refresh completion evaluates each dependent
    watch at most once per source run / schedule tick (-> notify exactly once). Each
    trigger kind gets a PREFIXED tag so a source_run_id and a schedule_tick (both
    bare ints) can never collide in the same key namespace."""
    trigger_ref = trigger_ref or {}
    if trigger_ref.get("source_run_id") is not None:
        tag = f"run-{trigger_ref['source_run_id']}"
    elif trigger_ref.get("schedule_tick") is not None:
        tag = f"sched-{trigger_ref['schedule_tick']}"
    elif trigger_ref.get("dedupe_key"):
        tag = f"dk-{trigger_ref['dedupe_key']}"
    else:
        tag = "manual"
    return f"watch:{watch_id}:eval:{tag}"


def _eval_already_pending(
    queue: JobQueue,
    project_id: str,
    dedupe_key: str,
    *,
    storage_org_id: int | None,
) -> bool:
    return (
        queue.find_job_by_refs(
            WATCH_EVALUATE_KIND,
            statuses=("queued", "running"),
            project_id=project_id,
            storage_org_id=storage_org_id,
            dedupe_key=dedupe_key,
        )
        is not None
    )


def enqueue_index_watch_evaluations(
    queue: JobQueue | None,
    *,
    project: Project,
    project_id: str,
    workspace_root: str | Path,
    storage_org_id: int | None = None,
    index_id: str,
    trigger_ref: dict[str, Any] | None = None,
) -> list[int]:
    """Enqueue one ``watch.evaluate`` job per dependent embedding_similarity watch,
    deduped per (watch, trigger). Enqueue-only — never evaluates inline."""
    if queue is None:
        return []
    job_ids: list[int] = []
    for watch in find_embedding_similarity_watches(project, index_id):
        watch_id = int(watch["id"])
        dedupe_key = _watch_eval_dedupe_key(watch_id, trigger_ref)
        if _eval_already_pending(
            queue,
            project_id,
            dedupe_key,
            storage_org_id=storage_org_id,
        ):
            continue
        job_ids.append(
            queue.enqueue(
                WATCH_EVALUATE_KIND,
                {
                    "project_id": project_id,
                    **(
                        {"storage_org_id": storage_org_id}
                        if storage_org_id is not None
                        else {}
                    ),
                    "watch_id": watch_id,
                    "index_id": index_id,
                    "trigger_ref": trigger_ref or {},
                    "dedupe_key": dedupe_key,
                    "workspace_root": str(workspace_root),
                },
                max_attempts=3,
            )
        )
    return job_ids


def register_watch_evaluate_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    """Register the worker handler that runs one queued watch evaluation."""
    root = Path(workspace_root)

    def handle(payload: dict, _context: JobHandlerContext) -> dict:
        project_id = str(payload["project_id"])
        watch_id = int(payload["watch_id"])
        if project_opener is None:
            project_root = Path(payload.get("workspace_root") or root)
            project = Project(project_root / f"{project_id}.frisket")
            delivery_storage_org_id = payload.get(
                "storage_org_id", workspace_root_storage_org_id
            )
        else:
            project_id, project_root, project_path = claimed_project_location(
                payload,
                workspace_root=root,
                workspace_root_storage_org_id=workspace_root_storage_org_id,
                require_storage_identity=True,
            )
            claimed = claimed_opener_key(payload)
            project = project_opener(claimed, project_path)
            delivery_storage_org_id = claimed.storage_org_id
        try:
            row = project.get_watch(watch_id)
            if row is None:
                return {
                    "project_id": project_id,
                    "watch_id": watch_id,
                    "skipped": True,
                    "reason": "not_found",
                }
            if not bool(row["enabled"]):
                return {
                    "project_id": project_id,
                    "watch_id": watch_id,
                    "skipped": True,
                    "reason": "disabled",
                }
            evaluation = run_watch_evaluation(project, row)
            enqueue_notification_emit_result(
                queue,
                workspace_root=project_root,
                project_id=project_id,
                storage_org_id=delivery_storage_org_id,
                result=evaluation["notification"],
                project_opener=project_opener,
            )
            run = evaluation["run"]
            return {
                "project_id": project_id,
                "watch_id": watch_id,
                "run_id": evaluation["run_id"],
                "status": run.get("status"),
                "error_code": run.get("error_code"),
            }
        finally:
            project.close()

    return registry.add(
        WATCH_EVALUATE_KIND,
        handle,
        origin="frisket.production.watch_evaluate",
    )
