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

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from frisket.engine.store import Project
from frisket.engine.jobs.notifications_delivery import (
    enqueue_notification_emit_result,
)
from frisket.features.watchlists.service import WatchBindingError, run_watch_evaluation

from .queue import JobQueue, TERMINAL, claimed_project_location
from .project_opener import (
    project_bundle_paths,
    ProjectOpener,
    claimed_opener_key,
)
from .project_scan import bundle_opener, scan_projects
from .schedule import _parse_time, _schedule_interval
from .ports import JobHandlerContext
from .worker import HandlerRegistration, HandlerRegistry

if TYPE_CHECKING:
    from frisket.retrieval.preparation import PreparationEncoder


WATCH_EVALUATE_KIND = "watch.evaluate"


def _watch_query(row: Any) -> dict[str, Any]:
    from frisket.features.watchlists.service import watch_query

    try:
        return watch_query(row)
    except WatchBindingError:
        return {}


def find_embedding_similarity_watches(project: Project, index_id: str) -> list[Any]:
    """Enabled embedding_similarity watches bound to ``index_id``."""
    out: list[Any] = []
    for watch in project.watches():
        if not bool(watch["enabled"]) or watch["schedule"] is None:
            continue
        query = _watch_query(watch).get("similarity") or {}
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


def enqueue_due_watch_evaluations(
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    storage_org_id: int | None = None,
    now: datetime | None = None,
    project_opener: ProjectOpener | None = None,
) -> list[dict[str, int | str]]:
    """Schedule observations of current data, independently of source writes.

    Reuse the watch worker and queue cadence; never prepare model inputs here.
    Failed checks are attempts too, while their last successful baseline stays
    untouched. A pending trigger/index continuation suppresses another check.
    """
    root = Path(workspace_root)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    enqueued = []

    def visit(project_id: str, project: Project) -> None:
        for watch in project.watches():
            if not watch["enabled"]:
                continue
            interval = _schedule_interval(watch["schedule"])
            if interval is None:
                continue
            watch_id = int(watch["id"])
            prefix = f"watch:{watch_id}:eval:"
            if (
                queue.find_job_by_refs(
                    WATCH_EVALUATE_KIND,
                    project_id=project_id,
                    storage_org_id=storage_org_id,
                    dedupe_key_prefix=prefix,
                )
                is not None
            ):
                continue
            prior_job = queue.find_job_by_refs(
                WATCH_EVALUATE_KIND,
                statuses=TERMINAL,
                project_id=project_id,
                storage_org_id=storage_org_id,
                dedupe_key_prefix=prefix,
            )
            prior_run = project.watch_latest_run(watch_id)
            attempts = [
                value
                for value in (
                    _parse_time(prior_run["finished_at"] or prior_run["started_at"])
                    if prior_run is not None
                    else None,
                    (
                        prior_job.finished_at
                        or prior_job.started_at
                        or prior_job.created_at
                    )
                    if prior_job is not None
                    else None,
                )
                if value is not None
            ]
            if attempts and max(attempts) + interval > now:
                continue
            trigger = {
                "trigger_kind": "schedule",
                "schedule": watch["schedule"],
                "schedule_tick": int(now.timestamp() // interval.total_seconds()),
            }
            key = _watch_eval_dedupe_key(watch_id, trigger)
            job_id = queue.enqueue(
                WATCH_EVALUATE_KIND,
                {
                    "project_id": project_id,
                    "watch_id": watch_id,
                    "workspace_root": str(root),
                    "trigger_ref": trigger,
                    "dedupe_key": key,
                    **(
                        {"storage_org_id": storage_org_id}
                        if storage_org_id is not None
                        else {}
                    ),
                },
                max_attempts=3,
            )
            enqueued.append(
                {"project_id": project_id, "watch_id": watch_id, "job_id": job_id}
            )

    scan_projects(
        project_bundle_paths(root, require_slug=project_opener is not None),
        bundle_opener(Project, project_opener, storage_org_id),
        visit,
        failure_event="watch_project_schedule_failed",
    )
    return enqueued


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
        if (
            queue.find_job_by_refs(
                WATCH_EVALUATE_KIND,
                project_id=project_id,
                storage_org_id=storage_org_id,
                dedupe_key=dedupe_key,
            )
            is not None
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


def enqueue_keyword_watch_evaluations(
    queue: JobQueue,
    *,
    watches: list[Any],
    project_id: str,
    workspace_root: str | Path,
    storage_org_id: int | None,
    trigger_ref: dict[str, Any],
) -> list[int]:
    """Enqueue affected FTS watches without evaluating on the caller thread."""
    job_ids = []
    for watch in watches:
        watch_id = int(watch["id"])
        dedupe_key = _watch_eval_dedupe_key(watch_id, trigger_ref)
        if (
            queue.find_job_by_refs(
                WATCH_EVALUATE_KIND,
                project_id=project_id,
                storage_org_id=storage_org_id,
                dedupe_key=dedupe_key,
            )
            is not None
        ):
            continue
        job_ids.append(
            queue.enqueue(
                WATCH_EVALUATE_KIND,
                {
                    "project_id": project_id,
                    "watch_id": watch_id,
                    "trigger_ref": trigger_ref,
                    "dedupe_key": dedupe_key,
                    "workspace_root": str(workspace_root),
                    **(
                        {"storage_org_id": storage_org_id}
                        if storage_org_id is not None
                        else {}
                    ),
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
    embedding_encoder: PreparationEncoder | None = None,
) -> HandlerRegistration:
    """Register the worker handler that runs one queued watch evaluation."""
    root = Path(workspace_root)

    def handle(payload: dict, context: JobHandlerContext) -> dict:
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

        def continue_after_indexing(processed: int) -> dict:
            from .search_index import index_work_marker

            continuation = {
                "project_id": project_id,
                "watch_id": watch_id,
                "trigger_ref": payload.get("trigger_ref") or {},
                "workspace_root": str(project_root),
            }
            if delivery_storage_org_id is not None:
                continuation["storage_org_id"] = delivery_storage_org_id
            if isinstance(context.trusted_job_org_id, int):
                continuation["org_id"] = context.trusted_job_org_id
            marker = index_work_marker(project, include_preparation=False) or "repair"
            trigger_key = _watch_eval_dedupe_key(watch_id, continuation["trigger_ref"])
            queue.enqueue(
                WATCH_EVALUATE_KIND,
                {**continuation, "dedupe_key": f"{trigger_key}:index:{marker}"},
                max_attempts=3,
            )
            return {
                "project_id": project_id,
                "watch_id": watch_id,
                "status": "indexing",
                "indexed": processed,
            }

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
            if row["schedule"] is None and (payload.get("trigger_ref") or {}).get(
                "trigger_kind"
            ):
                return {
                    "project_id": project_id,
                    "watch_id": watch_id,
                    "skipped": True,
                    "reason": "manual_only",
                }
            if _watch_query(row).get("search"):
                from .search_index import index_work_marker, process_index_batches

                # Keyword watches depend only on lexical publication. Running
                # optional semantic preparation here could let contention for
                # the shared encoder delay or exhaust an otherwise-ready watch.
                processed, _complete = process_index_batches(
                    project, embedding_encoder=None
                )
                # A source write can enqueue a fresh scope after the indexer
                # reports its quantum complete. Re-read durable work before
                # evaluating so that run cannot advance past an unseen row.
                # Semantic preparation may remain after the lexical dependency
                # is ready and must not delay a keyword-only watch.
                if index_work_marker(project, include_preparation=False) is not None:
                    return continue_after_indexing(processed)
            try:
                evaluation = run_watch_evaluation(project, row)
            except WatchBindingError as exc:
                if exc.code != "search_index_not_ready":
                    raise
                return continue_after_indexing(0)
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
