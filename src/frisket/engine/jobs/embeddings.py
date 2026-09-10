"""Queued embedding refresh + on_source_append trigger pilot.

The product path for recurring/source-triggered embedding work is the queue, not
inline execution: a source poll / import that appends rows must only *enqueue*
refresh work, never call a provider. This module is that substrate, reusing the
generic JobQueue/Worker (NOT the MapRunner/output-column queue):

- ``enqueue_source_append_refreshes`` finds the sheet's indexes whose maintenance
  policy enables ``on_source_append`` and enqueues one ``embedding.index_refresh``
  job per index, scoped to the appended rows, deduped by (index, trigger).
- ``register_embedding_refresh_handler`` runs the EXISTING refresh action inside a
  worker, scoped to the appended rows via a ``row_ids`` row_scope, under the same
  index-level claim and remote-egress gate. Transient errors (busy / provider)
  retry with backoff; non-retryable blocks (remote confirmation, unsupported
  source) complete with a typed ``blocked`` result so they are replayable without
  burning retries.

Not in scope: QuerySpec similarity, UI, watchlists, clustering, or a generic
automation_jobs framework. This is one narrow, embedding-owned pilot.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from frisket.ai.embeddings import EmbeddingStore, canonical_json
from frisket.contracts.action import ActionResult
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    launch_queued_action_job,
    reserve_typed_action_job,
)
from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project

from .queue import ACTION_RUN_KIND, JobQueue
from .schedule import _parse_time, _schedule_interval
from .project_opener import (
    ProjectOpener,
    require_opener_storage_org_id,
)
from .worker import HandlerRegistry


def _claimed_storage_org_id(project: Project) -> int | None:
    """Read the storage org from the claimed project location, typed and fail-closed."""
    parent = project.path.parent.name
    if not parent.isdigit():
        return None
    try:
        key = ProjectStorageKey(
            storage_org_id=int(parent), project_slug=project.path.stem
        )
    except ValueError:
        return None
    return key.storage_org_id


EMBEDDING_INDEX_REFRESH_ACTION_KIND = "embedding.index_refresh"
EMBEDDING_REFRESH_ENQUEUE_KIND = ACTION_RUN_KIND
# Public compatibility constant: native tests and callers import this as the
# product refresh job kind. New jobs are generic action.run jobs.
EMBEDDING_REFRESH_KIND = EMBEDDING_REFRESH_ENQUEUE_KIND


def maintenance_enables_source_append(policy: Any) -> bool:
    """An index refreshes on source append when its maintenance policy says
    ``mode == 'on_source_append'`` or sets ``on_source_append: true``."""
    if not isinstance(policy, dict):
        return False
    if policy.get("on_source_append") is True:
        return True
    return str(policy.get("mode") or "").strip().lower() == "on_source_append"


def find_on_source_append_indexes(project: Project, sheet_id: int) -> list[Any]:
    """Indexes on ``sheet_id`` whose maintenance policy enables on_source_append."""
    store = EmbeddingStore(project)
    out: list[Any] = []
    for idx in store.list_indexes(sheet_id):
        try:
            policy = json.loads(idx["maintenance_policy_json"] or "{}")
        except (TypeError, ValueError):
            policy = {}
        if maintenance_enables_source_append(policy):
            out.append(idx)
    return out


def refresh_dedupe_key(
    index_id: str, trigger_ref: dict[str, Any] | None, row_ids: list[int]
) -> str:
    """A stable key per (index, trigger). A source run dedupes by its run id; an
    ad-hoc append dedupes by the hash of the appended row set, so two triggers for
    the same rows do not create duplicate pending work."""
    trigger_ref = trigger_ref or {}
    trigger_kind = str(
        trigger_ref.get("trigger_kind") or trigger_ref.get("kind") or "source_append"
    )
    source_run_id = trigger_ref.get("source_run_id") or trigger_ref.get("run_id")
    if source_run_id is not None:
        return f"{index_id}:{trigger_kind}:run-{source_run_id}"
    scope_hash = hashlib.sha256(
        canonical_json(sorted(int(r) for r in row_ids)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{index_id}:{trigger_kind}:scope-{scope_hash}"


def _refresh_action_body(
    *,
    index_id: str,
    mode: str,
    trigger_ref: dict[str, Any] | None,
    dedupe_key: str,
    row_ids: list[int] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "index_id": index_id,
        "mode": mode,
        "trigger_ref": trigger_ref or None,
    }
    if row_ids:
        params["row_scope"] = {
            "kind": "row_ids",
            "row_ids": sorted({int(r) for r in row_ids}),
        }
    return {
        "action_id": EMBEDDING_INDEX_REFRESH_ACTION_KIND,
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": f"emb_refresh_job:{dedupe_key or index_id}",
    }


def _enqueue_refresh_action_run(
    queue: JobQueue,
    *,
    project: Project,
    project_id: str,
    workspace_root: str | Path,
    storage_org_id: int | None,
    index_id: str,
    sheet_id: int,
    mode: str,
    trigger_ref: dict[str, Any] | None,
    dedupe_key: str,
    row_ids: list[int] | None = None,
    max_attempts: int = 3,
) -> int | None:
    action_body = _refresh_action_body(
        index_id=index_id,
        mode=mode,
        trigger_ref=trigger_ref,
        dedupe_key=dedupe_key,
        row_ids=row_ids,
    )
    envelope = reserve_typed_action_job(
        project,
        project_id,
        typed_action_for_request(action_body),
    )
    if isinstance(envelope, ActionResult):
        return envelope.job_id
    payload_extra: dict[str, Any] = {
        # Flat fields are refs/diagnostics for queue indexes, old native tests,
        # and job projections. The executable contract is the action_job envelope.
        "index_id": index_id,
        "sheet_id": int(sheet_id),
        "trigger_ref": trigger_ref or {},
        "dedupe_key": dedupe_key,
        "mode": mode,
        "workspace_root": str(workspace_root),
        **({"storage_org_id": storage_org_id} if storage_org_id is not None else {}),
    }
    if row_ids:
        payload_extra["row_ids"] = sorted({int(r) for r in row_ids})
    result = launch_queued_action_job(
        envelope=envelope,
        queue=queue,
        job_kind=EMBEDDING_REFRESH_ENQUEUE_KIND,
        max_attempts=max_attempts,
        payload_extra=payload_extra,
        project=project,
    )
    return result.job_id


def _refresh_already_pending(
    queue: JobQueue,
    project_id: str,
    dedupe_key: str,
    *,
    storage_org_id: int | None,
) -> bool:
    # Indexed, unbounded lookup on the dedupe_key ref column (idx_jobs_dedupe) so
    # an older queued/running job can never be missed behind a result cap — the
    # job's dedupe_key is also persisted in payload for replay/inspection.
    return (
        queue.find_job_by_refs(
            EMBEDDING_REFRESH_ENQUEUE_KIND,
            statuses=("queued", "running"),
            project_id=project_id,
            storage_org_id=storage_org_id,
            dedupe_key=dedupe_key,
        )
        is not None
    )


# A scheduled job's dedupe_key encodes its cadence-window bucket, so "any job with
# this key, in ANY terminal or live state" means "this cadence attempt already
# ran". Unlike source-append (pending-only), a blocked/failed/done scheduled
# attempt must NOT be retried until the next window — otherwise a remote index
# whose refresh blocks on a cost gate would re-enqueue every scan.
_ALL_JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")


def _scheduled_attempt_already_seen(
    queue: JobQueue,
    project_id: str,
    dedupe_key: str,
    *,
    storage_org_id: int | None,
) -> bool:
    return (
        queue.find_job_by_refs(
            EMBEDDING_REFRESH_ENQUEUE_KIND,
            statuses=_ALL_JOB_STATUSES,
            project_id=project_id,
            storage_org_id=storage_org_id,
            dedupe_key=dedupe_key,
        )
        is not None
    )


def enqueue_source_append_refreshes(
    queue: JobQueue | None,
    *,
    project: Project,
    project_id: str,
    workspace_root: str | Path,
    storage_org_id: int | None = None,
    sheet_id: int,
    row_ids: list[int],
    trigger_ref: dict[str, Any] | None = None,
) -> list[int]:
    """Enqueue an incremental refresh per on_source_append index for the appended
    rows. Enqueue-only — never calls a provider. Deduped by (index, trigger) so a
    repeated trigger does not pile up duplicate pending jobs."""
    if queue is None or not row_ids:
        return []
    indexes = find_on_source_append_indexes(project, sheet_id)
    if not indexes:
        return []
    appended = sorted({int(r) for r in row_ids})
    job_ids: list[int] = []
    for idx in indexes:
        index_id = idx["id"]
        dedupe_key = refresh_dedupe_key(index_id, trigger_ref, appended)
        if _refresh_already_pending(
            queue,
            project_id,
            dedupe_key,
            storage_org_id=storage_org_id,
        ):
            continue
        job_id = _enqueue_refresh_action_run(
            queue,
            project=project,
            project_id=project_id,
            workspace_root=workspace_root,
            storage_org_id=storage_org_id,
            index_id=index_id,
            sheet_id=int(sheet_id),
            row_ids=appended,
            trigger_ref=trigger_ref or {},
            dedupe_key=dedupe_key,
            mode="incremental",
            max_attempts=3,
        )
        if job_id is not None:
            job_ids.append(job_id)
    return job_ids


def maintenance_enables_scheduled(policy: Any) -> bool:
    """An index refreshes on a cadence when its maintenance policy says
    ``mode == 'scheduled'`` or sets ``scheduled: true``."""
    if not isinstance(policy, dict):
        return False
    if policy.get("scheduled") is True:
        return True
    return str(policy.get("mode") or "").strip().lower() == "scheduled"


def maintenance_schedule(policy: Any) -> str | None:
    """The cadence string (`@hourly`, `daily`, `1h`, `*/30 * * * *`, ...) for a
    scheduled maintenance policy, or None."""
    if not isinstance(policy, dict):
        return None
    schedule = policy.get("schedule")
    return schedule if isinstance(schedule, str) and schedule.strip() else None


def find_scheduled_indexes(project: Project) -> list[Any]:
    """All indexes (any sheet) whose maintenance policy enables scheduled refresh."""
    store = EmbeddingStore(project)
    out: list[Any] = []
    for idx in store.list_indexes():
        try:
            policy = json.loads(idx["maintenance_policy_json"] or "{}")
        except (TypeError, ValueError):
            policy = {}
        if maintenance_enables_scheduled(policy):
            out.append(idx)
    return out


def _schedule_due(index: Any, *, now: datetime) -> tuple[bool, timedelta | None]:
    """Is the index due for a scheduled refresh, and on what interval. Due when
    ``last_refreshed_at`` is unset (never refreshed) or one interval has elapsed.
    Returns (False, None) for an unparseable / absent schedule."""
    try:
        policy = json.loads(index["maintenance_policy_json"] or "{}")
    except (TypeError, ValueError):
        return (False, None)
    schedule = maintenance_schedule(policy)
    try:
        interval = _schedule_interval(schedule)
    except ValueError:
        return (False, None)
    if interval is None:
        return (False, None)
    last = _parse_time(index["last_refreshed_at"])
    return (last is None or last + interval <= now, interval)


def scheduled_refresh_dedupe_key(
    index_id: str, interval: timedelta, *, now: datetime
) -> str:
    """A key stable WITHIN one cadence window (the interval bucket), so two
    scheduler passes in the same window enqueue at most one refresh."""
    bucket = int(now.timestamp() // max(interval.total_seconds(), 1))
    return f"{index_id}:scheduled:{bucket}"


def enqueue_due_scheduled_refreshes(
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    storage_org_id: int | None = None,
    now: datetime | None = None,
    max_attempts: int = 3,
    project_opener: ProjectOpener | None = None,
) -> list[dict[str, int | str]]:
    """Enqueue one incremental ``embedding.index_refresh`` job per due scheduled
    index across the workspace. Enqueue-only — never calls a provider; the worker
    applies the remote/cost gates. Deduped per (index, cadence window). The job
    carries NO row_ids, so the refresh runs over the index's full stored scope."""
    root = Path(workspace_root)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    enqueued: list[dict[str, int | str]] = []
    for bundle in sorted(root.glob("*.frisket")):
        if not (bundle / "manifest.json").exists():
            continue
        project_id = bundle.stem
        if project_opener is None:
            project = Project(bundle)
        else:
            # A scan that injects an opener must key every open by the root's
            # DECLARED storage org (never the directory name), and refuse the
            # whole scan if that identity is missing — see
            # frisket.jobs.project_opener.
            project = project_opener(
                ProjectStorageKey(
                    storage_org_id=require_opener_storage_org_id(storage_org_id),
                    project_slug=project_id,
                ),
                bundle,
            )
        try:
            for idx in find_scheduled_indexes(project):
                due, interval = _schedule_due(idx, now=now)
                if not due or interval is None:
                    continue
                index_id = idx["id"]
                dedupe_key = scheduled_refresh_dedupe_key(index_id, interval, now=now)
                if _scheduled_attempt_already_seen(
                    queue,
                    project_id,
                    dedupe_key,
                    storage_org_id=storage_org_id,
                ):
                    continue
                trigger_ref = {
                    "trigger_kind": "schedule",
                    "schedule": maintenance_schedule(
                        json.loads(idx["maintenance_policy_json"] or "{}")
                    ),
                    "schedule_tick": dedupe_key.rsplit(":", 1)[-1],
                }
                job_id = _enqueue_refresh_action_run(
                    queue,
                    project=project,
                    project_id=project_id,
                    workspace_root=root,
                    storage_org_id=storage_org_id,
                    index_id=index_id,
                    sheet_id=int(idx["sheet_id"]),
                    trigger_ref=trigger_ref,
                    dedupe_key=dedupe_key,
                    mode="incremental",
                    max_attempts=max_attempts,
                )
                if job_id is None:
                    continue
                enqueued.append(
                    {
                        "job_id": job_id,
                        "project_id": project_id,
                        "index_id": index_id,
                        "dedupe_key": dedupe_key,
                    }
                )
        finally:
            project.close()
    return enqueued


def register_embedding_refresh_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    router: Any | None = None,
    gateway: Any | None = None,
    queue: Any | None = None,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> None:
    """Register embedding refresh execution for generic ``action.run`` jobs.

    Refresh work is enqueued as ACTION_RUN_KIND with an ActionJobEnvelope;
    this function keeps the old name for callers/tests.
    """
    root = Path(workspace_root)

    from frisket.engine.jobs.runs import register_action_run_handler

    if registry.get(ACTION_RUN_KIND) is None:
        register_action_run_handler(registry, workspace_root=root, router=router)

    def _enqueue_dependent_watches(
        project: Project,
        *,
        project_id: str,
        project_root: Path,
        storage_org_id: int | None,
        index_id: str,
        trigger_ref: dict[str, Any] | None,
    ) -> int:
        if queue is None:
            return 0
        from frisket.engine.jobs.watches import enqueue_index_watch_evaluations

        watch_jobs = enqueue_index_watch_evaluations(
            queue,
            project=project,
            project_id=project_id,
            workspace_root=project_root,
            storage_org_id=storage_org_id,
            index_id=index_id,
            trigger_ref=trigger_ref,
        )
        return len(watch_jobs)

    def _action_job_executor(project: Project, env: ActionJobEnvelope) -> ActionResult:
        from frisket.engine.executor.action_families.embeddings import (
            run_index_refresh_action_job,
        )

        result = run_index_refresh_action_job(
            project,
            env,
            router=router,
            embedding_gateway=gateway,
        )
        if result.status == "completed":
            output_ref = result.outputs[0].ref if result.outputs else {}
            index_id = str(output_ref.get("index_id") or "")
            if index_id:
                _enqueue_dependent_watches(
                    project,
                    project_id=env.project_id,
                    project_root=root,
                    # The worker routed this job to its claimed physical project,
                    # so read the storage org from that claimed location as a
                    # typed identity rather than parsing a raw directory name.
                    storage_org_id=_claimed_storage_org_id(project),
                    index_id=index_id,
                    trigger_ref=output_ref.get("trigger_ref"),
                )
        return result

    registry.register_action_executor(
        EMBEDDING_INDEX_REFRESH_ACTION_KIND, _action_job_executor
    )
