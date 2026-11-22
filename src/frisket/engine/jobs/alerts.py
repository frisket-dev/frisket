"""Queued ``alert.evaluate`` jobs — dependency-aware alert evaluation.

The source-append pipeline must sequence: source poll -> embedding refresh -> alert
evaluation. The source poll evaluates non-embedding alerts inline, but an
``embedding_similarity`` alert must wait until its index's refresh job COMPLETES (or
it blocks on ``embedding_index_incomplete`` because the appended rows aren't
embedded yet, and never re-fires). So the embedding-refresh worker enqueues one
``alert.evaluate`` job per dependent alert on completion, and this handler runs it.

Reuses the generic SqliteJobQueue — no new automation framework. The alert
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
from frisket.alerts.service import AlertBindingError, check_alert
from frisket.engine.views.specs import is_local_retrieval_similarity

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


ALERT_EVALUATE_KIND = "alert.evaluate"
_MAX_INDEX_CONTINUATIONS = 64


def _alert_query(row: Any) -> dict[str, Any]:
    from frisket.alerts.service import alert_query

    try:
        return alert_query(row)
    except AlertBindingError:
        return {}


def find_embedding_similarity_alerts(project: Project, index_id: str) -> list[Any]:
    """Enabled embedding_similarity alerts bound to ``index_id``."""
    out: list[Any] = []
    for alert in project.alerts():
        if not bool(alert["enabled"]) or alert["schedule"] is None:
            continue
        query = _alert_query(alert).get("similarity") or {}
        if str(query.get("embedding_index_id") or "") == index_id:
            out.append(alert)
    return out


def _alert_eval_dedupe_key(alert_id: int, trigger_ref: dict[str, Any] | None) -> str:
    """Stable per (alert, trigger) so a refresh completion evaluates each dependent
    alert at most once per feed check / schedule tick (-> notify exactly once). Each
    trigger kind gets a PREFIXED tag so a feed_check_id and a schedule_tick (both
    bare ints) can never collide in the same key namespace."""
    trigger_ref = trigger_ref or {}
    if trigger_ref.get("feed_check_id") is not None:
        tag = f"run-{trigger_ref['feed_check_id']}"
    elif trigger_ref.get("schedule_tick") is not None:
        tag = f"sched-{trigger_ref['schedule_tick']}"
    elif trigger_ref.get("dedupe_key"):
        tag = f"dk-{trigger_ref['dedupe_key']}"
    else:
        tag = "manual"
    return f"alert:{alert_id}:eval:{tag}"


def enqueue_due_alert_evaluations(
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    storage_organization_id: int | None = None,
    now: datetime | None = None,
    project_opener: ProjectOpener | None = None,
) -> list[dict[str, int | str]]:
    """Schedule observations of current data, independently of source writes.

    Reuse the alert worker and queue cadence; never prepare model inputs here.
    Failed checks are attempts too, while their last successful baseline stays
    untouched. A pending trigger/index continuation suppresses another check.
    """
    root = Path(workspace_root)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    enqueued = []

    def visit(project_id: str, project: Project) -> None:
        for alert in project.alerts():
            if not alert["enabled"]:
                continue
            interval = _schedule_interval(alert["schedule"])
            if interval is None:
                continue
            alert_id = int(alert["id"])
            prefix = f"alert:{alert_id}:eval:"
            if (
                queue.find_job_by_refs(
                    ALERT_EVALUATE_KIND,
                    project_id=project_id,
                    storage_organization_id=storage_organization_id,
                    dedupe_key_prefix=prefix,
                )
                is not None
            ):
                continue
            prior_job = queue.find_job_by_refs(
                ALERT_EVALUATE_KIND,
                statuses=TERMINAL,
                project_id=project_id,
                storage_organization_id=storage_organization_id,
                dedupe_key_prefix=prefix,
            )
            prior_run = project.alert_latest_run(alert_id)
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
                "schedule": alert["schedule"],
                "schedule_tick": int(now.timestamp() // interval.total_seconds()),
            }
            key = _alert_eval_dedupe_key(alert_id, trigger)
            job_id = queue.enqueue(
                ALERT_EVALUATE_KIND,
                {
                    "project_id": project_id,
                    "alert_id": alert_id,
                    "workspace_root": str(root),
                    "trigger_ref": trigger,
                    "dedupe_key": key,
                    **(
                        {"storage_organization_id": storage_organization_id}
                        if storage_organization_id is not None
                        else {}
                    ),
                },
                max_attempts=3,
            )
            enqueued.append(
                {"project_id": project_id, "alert_id": alert_id, "job_id": job_id}
            )

    scan_projects(
        project_bundle_paths(root, require_slug=project_opener is not None),
        bundle_opener(Project, project_opener, storage_organization_id),
        visit,
        failure_event="alert_project_schedule_failed",
    )
    return enqueued


def enqueue_index_alert_evaluations(
    queue: JobQueue | None,
    *,
    project: Project,
    project_id: str,
    workspace_root: str | Path,
    storage_organization_id: int | None = None,
    index_id: str,
    trigger_ref: dict[str, Any] | None = None,
) -> list[int]:
    """Enqueue one ``alert.evaluate`` job per dependent embedding_similarity alert,
    deduped per (alert, trigger). Enqueue-only — never evaluates inline."""
    if queue is None:
        return []
    job_ids: list[int] = []
    for alert in find_embedding_similarity_alerts(project, index_id):
        alert_id = int(alert["id"])
        dedupe_key = _alert_eval_dedupe_key(alert_id, trigger_ref)
        if (
            queue.find_job_by_refs(
                ALERT_EVALUATE_KIND,
                project_id=project_id,
                storage_organization_id=storage_organization_id,
                dedupe_key=dedupe_key,
            )
            is not None
        ):
            continue
        job_ids.append(
            queue.enqueue(
                ALERT_EVALUATE_KIND,
                {
                    "project_id": project_id,
                    **(
                        {"storage_organization_id": storage_organization_id}
                        if storage_organization_id is not None
                        else {}
                    ),
                    "alert_id": alert_id,
                    "index_id": index_id,
                    "trigger_ref": trigger_ref or {},
                    "dedupe_key": dedupe_key,
                    "workspace_root": str(workspace_root),
                },
                max_attempts=3,
            )
        )
    return job_ids


def enqueue_keyword_alert_evaluations(
    queue: JobQueue,
    *,
    alerts: list[Any],
    project_id: str,
    workspace_root: str | Path,
    storage_organization_id: int | None,
    trigger_ref: dict[str, Any],
) -> list[int]:
    """Enqueue affected FTS alerts without evaluating on the caller thread."""
    job_ids = []
    for alert in alerts:
        alert_id = int(alert["id"])
        dedupe_key = _alert_eval_dedupe_key(alert_id, trigger_ref)
        if (
            queue.find_job_by_refs(
                ALERT_EVALUATE_KIND,
                project_id=project_id,
                storage_organization_id=storage_organization_id,
                dedupe_key=dedupe_key,
            )
            is not None
        ):
            continue
        job_ids.append(
            queue.enqueue(
                ALERT_EVALUATE_KIND,
                {
                    "project_id": project_id,
                    "alert_id": alert_id,
                    "trigger_ref": trigger_ref,
                    "dedupe_key": dedupe_key,
                    "workspace_root": str(workspace_root),
                    **(
                        {"storage_organization_id": storage_organization_id}
                        if storage_organization_id is not None
                        else {}
                    ),
                },
                max_attempts=3,
            )
        )
    return job_ids


def _advance_alert_index(
    project: Project,
    query: dict[str, Any],
    *,
    embedding_encoder: PreparationEncoder | None,
    continuation_step: int,
) -> tuple[bool, int]:
    """Run one bounded indexing quantum; return (should_continue, processed)."""
    needs_local_retrieval = is_local_retrieval_similarity(query)
    if not query.get("search") and not needs_local_retrieval:
        return False, 0
    from .search_index import index_work_marker, process_index_batches

    active_encoder = embedding_encoder if needs_local_retrieval else None
    if needs_local_retrieval and (
        active_encoder is None or not active_encoder.available()
    ):
        return False, 0
    before_marker = index_work_marker(
        project, include_preparation=needs_local_retrieval
    )
    processed, _complete = process_index_batches(
        project, embedding_encoder=active_encoder
    )
    remaining = index_work_marker(project, include_preparation=needs_local_retrieval)
    made_progress = remaining != before_marker or processed > 0
    should_continue = (
        remaining is not None
        and continuation_step < _MAX_INDEX_CONTINUATIONS
        and (made_progress or not needs_local_retrieval)
    )
    return should_continue, processed


def register_alert_evaluate_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    workspace_root_storage_organization_id: int | None = None,
    project_opener: ProjectOpener | None = None,
    embedding_encoder: PreparationEncoder | None = None,
) -> HandlerRegistration:
    """Register the worker handler that runs one queued alert evaluation."""
    root = Path(workspace_root)

    def handle(payload: dict, context: JobHandlerContext) -> dict:
        project_id = str(payload["project_id"])
        alert_id = int(payload["alert_id"])
        if project_opener is None:
            project_root = Path(payload.get("workspace_root") or root)
            project = Project(project_root / f"{project_id}.frisket")
            delivery_storage_organization_id = payload.get(
                "storage_organization_id", workspace_root_storage_organization_id
            )
        else:
            project_id, project_root, project_path = claimed_project_location(
                payload,
                workspace_root=root,
                workspace_root_storage_organization_id=workspace_root_storage_organization_id,
                require_storage_identity=True,
            )
            claimed = claimed_opener_key(payload)
            project = project_opener(claimed, project_path)
            delivery_storage_organization_id = claimed.storage_organization_id

        def continue_after_indexing(
            processed: int, *, include_preparation: bool = False
        ) -> dict:
            from .search_index import index_work_marker

            next_step = int(payload.get("index_continuation_step") or 0) + 1
            continuation = {
                "project_id": project_id,
                "alert_id": alert_id,
                "trigger_ref": payload.get("trigger_ref") or {},
                "workspace_root": str(project_root),
                "index_continuation_step": next_step,
            }
            if delivery_storage_organization_id is not None:
                continuation["storage_organization_id"] = (
                    delivery_storage_organization_id
                )
            if isinstance(context.trusted_job_organization_id, int):
                continuation["organization_id"] = context.trusted_job_organization_id
            marker = (
                index_work_marker(project, include_preparation=include_preparation)
                or "repair"
            )
            trigger_key = _alert_eval_dedupe_key(alert_id, continuation["trigger_ref"])
            queue.enqueue(
                ALERT_EVALUATE_KIND,
                {
                    **continuation,
                    "dedupe_key": f"{trigger_key}:index:{next_step}:{marker}",
                },
                max_attempts=3,
            )
            return {
                "project_id": project_id,
                "alert_id": alert_id,
                "status": "indexing",
                "indexed": processed,
            }

        try:
            row = project.get_alert(alert_id)
            if row is None:
                return {
                    "project_id": project_id,
                    "alert_id": alert_id,
                    "skipped": True,
                    "reason": "not_found",
                }
            if not bool(row["enabled"]):
                return {
                    "project_id": project_id,
                    "alert_id": alert_id,
                    "skipped": True,
                    "reason": "disabled",
                }
            if row["schedule"] is None and (payload.get("trigger_ref") or {}).get(
                "trigger_kind"
            ):
                return {
                    "project_id": project_id,
                    "alert_id": alert_id,
                    "skipped": True,
                    "reason": "manual_only",
                }
            query = _alert_query(row)
            needs_local_retrieval = is_local_retrieval_similarity(query)
            should_continue, processed = _advance_alert_index(
                project,
                query,
                embedding_encoder=embedding_encoder,
                continuation_step=int(payload.get("index_continuation_step") or 0),
            )
            if should_continue:
                return continue_after_indexing(
                    processed, include_preparation=needs_local_retrieval
                )
            try:
                evaluation = check_alert(
                    project, row, embedding_encoder=embedding_encoder
                )
            except AlertBindingError as exc:
                if exc.code != "search_index_not_ready":
                    raise
                return continue_after_indexing(
                    0, include_preparation=needs_local_retrieval
                )
            enqueue_notification_emit_result(
                queue,
                workspace_root=project_root,
                project_id=project_id,
                storage_organization_id=delivery_storage_organization_id,
                result=evaluation["notification"],
                project_opener=project_opener,
            )
            run = evaluation["run"]
            return {
                "project_id": project_id,
                "alert_id": alert_id,
                "run_id": evaluation["run_id"],
                "status": run.get("status"),
                "error_code": run.get("error_code"),
            }
        finally:
            project.close()

    return registry.add(
        ALERT_EVALUATE_KIND,
        handle,
        origin="frisket.production.alert_evaluate",
    )
