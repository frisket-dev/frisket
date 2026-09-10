"""Shared project-run queue reconciliation for HTTP status surfaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, MutableMapping

from frisket.authoring import actions as action_contract
from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.contracts.action import ActionError
from frisket.engine.executor.project_run_terminalization import (
    ProjectRunTerminalizationResult,
)
from frisket.engine.executor.queued_actions import (
    queued_v1_terminal_receipt_transition,
)
from frisket.engine.jobs.projection import job_queue_payload, job_run_id
from frisket.engine.jobs.queue import Job, JobQueue
from frisket.engine.jobs.runs import RUN_PROJECT_KIND
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


RunStatus = Literal[
    "queued",
    "running",
    "stalled",
    "orphaned",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "needs_user_action",
]


@dataclass(frozen=True)
class ProjectRunStatus:
    row: Any
    progress: Any | None
    job: Job | None
    public_status: RunStatus
    queue_payload: dict[str, Any] | None = None
    stalled_reason: str | None = None
    job_error: str | None = None


@dataclass(frozen=True)
class ProjectRunCancelResult:
    row: Any
    job: Job | None
    queue_cancelled: bool
    disposition: Literal[
        "terminalized",
        "already_terminal",
        "cancel_pending",
        "reconciliation_required",
        "conflict",
    ]
    cancel_requested: bool
    terminalization: ProjectRunTerminalizationResult | None = None


def _parse_status_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        out = value
    else:
        try:
            out = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out.astimezone(UTC)


def _status_time(value: Any) -> str | None:
    parsed = _parse_status_time(value)
    return parsed.isoformat() if parsed is not None else None


def seconds_between(start: Any, end: Any) -> float | None:
    start_dt = _parse_status_time(start)
    end_dt = _parse_status_time(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _rounded_seconds(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def duration_stats(values: list[float | None]) -> dict[str, Any]:
    clean = sorted(v for v in values if v is not None and v >= 0)

    def percentile(pct: float) -> float | None:
        if not clean:
            return None
        if len(clean) == 1:
            return clean[0]
        pos = (len(clean) - 1) * pct
        low = int(pos)
        high = min(low + 1, len(clean) - 1)
        fraction = pos - low
        return clean[low] + (clean[high] - clean[low]) * fraction

    return {
        "count": len(clean),
        "min_seconds": _rounded_seconds(clean[0] if clean else None),
        "p50_seconds": _rounded_seconds(percentile(0.50)),
        "p90_seconds": _rounded_seconds(percentile(0.90)),
        "max_seconds": _rounded_seconds(clean[-1] if clean else None),
    }


def run_timing_payload(
    row: Any,
    *,
    now: datetime,
    job: Job | None = None,
    public_status: str | None = None,
) -> dict[str, Any]:
    status = public_status or row["status"]
    finished_at = _parse_status_time(row["finished_at"])
    run_end = finished_at or now
    elapsed = seconds_between(row["started_at"], run_end)
    total_rows = int(row["total_rows"] or 0)
    completed_rows = int(row["completed_rows"] or 0)
    failed_rows = int(row["failed_rows"] or 0)
    processed_rows = completed_rows + failed_rows
    remaining_rows = max(total_rows - processed_rows, 0)
    rows_per_second = (
        processed_rows / elapsed
        if elapsed is not None and elapsed > 0 and processed_rows > 0
        else None
    )
    active_statuses = {"queued", "running", "stalled", "orphaned"}
    eta_seconds = None
    estimated_finish_at = None
    if status in active_statuses and remaining_rows == 0 and total_rows > 0:
        eta_seconds = 0.0
        estimated_finish_at = now.isoformat()
    elif (
        status in active_statuses
        and remaining_rows > 0
        and rows_per_second is not None
        and rows_per_second > 0
    ):
        eta_seconds = remaining_rows / rows_per_second
        estimated_finish_at = (now + timedelta(seconds=eta_seconds)).isoformat()

    queue_wait = None
    job_elapsed = None
    if job is not None:
        job_start_for_wait = job.started_at or (now if job.status == "queued" else None)
        queue_wait = seconds_between(job.created_at, job_start_for_wait)
        job_end = job.finished_at or (now if job.started_at is not None else None)
        job_elapsed = seconds_between(job.started_at, job_end)

    return {
        "started_at": _status_time(row["started_at"]),
        "finished_at": _status_time(row["finished_at"]),
        "elapsed_seconds": _rounded_seconds(elapsed),
        "processed_rows": processed_rows,
        "remaining_rows": remaining_rows,
        "processed_rows_per_second": (
            round(rows_per_second, 6) if rows_per_second is not None else None
        ),
        "eta_seconds": _rounded_seconds(eta_seconds),
        "estimated_finish_at": estimated_finish_at,
        "queue_wait_seconds": _rounded_seconds(queue_wait),
        "job_elapsed_seconds": _rounded_seconds(job_elapsed),
    }


def project_run_status_payload(
    status: ProjectRunStatus,
    *,
    run_id: int,
    now: datetime,
    project: Project | None = None,
) -> dict[str, Any]:
    row = status.row
    payload = {
        "run_id": run_id,
        **action_metadata_for_action_kind(run_row_action_kind(row)),
        "status": status.public_status,
        "total": row["total_rows"],
        "completed": row["completed_rows"],
        "failed": row["failed_rows"],
        "cost": row["cost_actual"],
        "live": (
            status.public_status == "running"
            and status.progress is not None
            and not status.progress.done
        ),
        "timing": run_timing_payload(
            row,
            now=now,
            job=status.job,
            public_status=status.public_status,
        ),
    }
    if status.queue_payload is not None:
        payload["queue"] = status.queue_payload
    if status.stalled_reason:
        payload["stalled_reason"] = status.stalled_reason
    if status.job_error:
        payload["error"] = status.job_error
    # A typed recipe/session halt (e.g. Parakeet's local_artifact_unavailable)
    # finalizes as a RESUMABLE 'cancelled' by design, but the reason lives only
    # in the run params — so the run reads as an indistinguishable bare
    # "cancelled". Surface it so the UI can say WHY it stopped and that a retry
    # is the fix, rather than looking like an operator cancel.
    halted_code, halted_reason = _run_halt(row)
    if halted_code:
        payload["halted_code"] = halted_code
    if halted_reason:
        payload["halted_reason"] = halted_reason
    # This dict is the wire shape both the
    # jobs/errors bottom-dock (via getRunProgress -> RunProgress.rowErrors)
    # and action_run_status_payload's public_status consume — one query here
    # covers both surfaces. `project` is optional (some callers build a
    # ProjectRunStatus without one) so it stays additive-only.
    if project is not None:
        row_errors = RunResultStore(project).row_error_summary(run_id)
        if row_errors is not None:
            payload["row_errors"] = row_errors
    return payload


def project_timing_payload(
    project: Project,
    jobs: list[Job],
    *,
    now: datetime,
) -> dict[str, Any]:
    run_rows = project.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
    run_durations: list[float | None] = []
    run_rps: list[float | None] = []
    by_action: dict[str, dict[str, Any]] = {}
    for row in run_rows:
        action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
        action_bucket = by_action.setdefault(
            action_metadata["action_kind"],
            {
                "action_name": action_metadata["action_name"],
                "count": 0,
                "durations": [],
                "rps": [],
            },
        )
        action_bucket["count"] = int(action_bucket["count"]) + 1
        elapsed = seconds_between(row["started_at"], row["finished_at"])
        if elapsed is None:
            continue
        processed = int(row["completed_rows"] or 0) + int(row["failed_rows"] or 0)
        rps = processed / elapsed if elapsed > 0 and processed > 0 else None
        run_durations.append(elapsed)
        run_rps.append(rps)
        action_bucket["durations"].append(elapsed)
        action_bucket["rps"].append(rps)

    job_waits: list[float | None] = []
    job_durations: list[float | None] = []
    by_kind: dict[str, dict[str, list[float | None] | int]] = {}
    for job in jobs:
        wait = seconds_between(job.created_at, job.started_at)
        duration = seconds_between(job.started_at, job.finished_at)
        job_waits.append(wait)
        job_durations.append(duration)
        bucket = by_kind.setdefault(
            job.kind, {"count": 0, "waits": [], "durations": []}
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["waits"].append(wait)
        bucket["durations"].append(duration)

    active_runs = []
    jobs_by_run_id: dict[int, Job] = {}
    for job in jobs:
        if job.kind != RUN_PROJECT_KIND:
            continue
        run_id = job_run_id(job)
        if run_id is not None:
            jobs_by_run_id[run_id] = job
    for row in run_rows:
        if row["status"] != "running":
            continue
        job = jobs_by_run_id.get(int(row["id"]))
        public_status = (
            "queued" if job is not None and job.status == "queued" else "running"
        )
        action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
        active_runs.append(
            {
                "run_id": row["id"],
                **action_metadata,
                "status": public_status,
                "timing": run_timing_payload(
                    row, now=now, job=job, public_status=public_status
                ),
            }
        )

    return {
        "runs": {
            "count": len(run_rows),
            "duration_seconds": duration_stats(run_durations),
            "rows_per_second": duration_stats(run_rps),
            "by_action": {
                action_kind: {
                    "action_name": str(bucket["action_name"]),
                    "count": int(bucket["count"]),
                    "duration_seconds": duration_stats(bucket["durations"]),
                    "rows_per_second": duration_stats(bucket["rps"]),
                }
                for action_kind, bucket in sorted(by_action.items())
            },
        },
        "jobs": {
            "count": len(jobs),
            "queue_wait_seconds": duration_stats(job_waits),
            "duration_seconds": duration_stats(job_durations),
            "by_kind": {
                kind: {
                    "count": int(bucket["count"]),
                    "queue_wait_seconds": duration_stats(bucket["waits"]),
                    "duration_seconds": duration_stats(bucket["durations"]),
                }
                for kind, bucket in sorted(by_kind.items())
            },
        },
        "active_runs": active_runs,
    }


def find_project_run_job(
    queue: JobQueue,
    project_id: str,
    run_id: int,
    *,
    storage_org_id: int | None = None,
) -> Job | None:
    """Best-effort recovery path for server restarts or old run mappings."""
    return queue.get_project_run_job(
        project_id,
        run_id,
        storage_org_id=storage_org_id,
    )


def _run_age_seconds(row: Any, now: datetime) -> float | None:
    started = row["started_at"]
    if started is None:
        return None
    if isinstance(started, datetime):
        started_at = started
    else:
        try:
            started_at = datetime.fromisoformat(str(started))
        except ValueError:
            return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - started_at.astimezone(now.tzinfo)).total_seconds())


def _run_has_no_progress(row: Any) -> bool:
    return int(row["completed_rows"] or 0) == 0 and int(row["failed_rows"] or 0) == 0


def _run_halt(row: Any) -> tuple[str | None, str | None]:
    """The typed halt (code, reason) stored in a run's params on a
    recipe/session-halted cancel, or (None, None). Stored in params rather
    than a dedicated column (finalization.py), so it is parsed lazily here."""
    try:
        params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        return None, None
    if not isinstance(params, dict):
        return None, None
    code = params.get("halted_code")
    reason = params.get("halted_reason")
    return (
        code if isinstance(code, str) and code else None,
        reason if isinstance(reason, str) and reason else None,
    )


def _project_run_job(
    *,
    queue: JobQueue,
    run_jobs: MutableMapping[tuple[str, int], int],
    project_id: str,
    run_id: int,
    storage_org_id: int | None = None,
) -> tuple[int | None, Job | None]:
    key = (project_id, run_id)
    job_id = run_jobs.get(key)
    job = queue.get(job_id) if job_id is not None else None
    if (
        job is not None
        and storage_org_id is not None
        and job.storage_org_id != storage_org_id
    ):
        job = None
    if job is None:
        job = find_project_run_job(
            queue,
            project_id,
            run_id,
            storage_org_id=storage_org_id,
        )
        if job is not None:
            job_id = job.id
            run_jobs[key] = job.id
    return job_id, job


def _run_action_kind(row: Any, job: Job | None = None) -> str:
    if job is not None and job.action_kind:
        return job.action_kind
    # Durable receipt identity must preserve per-plugin/custom kinds that the
    # static public metadata catalog intentionally presents as ``unknown``.
    return run_row_action_kind(row)


def _terminalize_v1_queued_receipt(
    *,
    project: Project,
    project_id: str,
    run_id: int,
    row: Any,
    status: Literal["failed", "cancelled"],
    job: Job | None = None,
    code: str | None = None,
    message: str | None = None,
    receipt_only: bool = False,
    require_cancel_intent: bool = False,
    include_terminal_receipt_lookup: bool = False,
) -> ProjectRunTerminalizationResult:
    error = None
    if status == "failed":
        action_kind = _run_action_kind(row, job)
        error = ActionError(
            code=code or "queue_job_failed",
            message=message or "queued project run job failed",
            action_kind=action_kind,
            details={
                "run_id": run_id,
                **({"job_id": job.id} if job is not None else {}),
            },
        )
    claimless_direct_effect = _run_action_kind(row, job) in {
        "embedding.index_refresh",
        "reduce.group_summary",
    }
    transition = queued_v1_terminal_receipt_transition(
        project,
        job.payload if job is not None else None,
        project_id=project_id,
        run_id=run_id,
        status=status,
        error=error,
        receipt_id=job.receipt_id if job is not None else None,
        action_kind=_run_action_kind(row, job),
        job_id=job.id if job is not None else None,
        receipt_only=receipt_only,
        require_cancel_intent=require_cancel_intent,
        include_terminal_receipt_lookup=include_terminal_receipt_lookup,
        claimless_direct_effect=claimless_direct_effect,
        raise_on_conflict=False,
    )
    return transition.terminalization


def _queued_age_seconds(job: Job, now: datetime) -> float | None:
    return seconds_between(job.created_at, now)


def reconcile_project_run_status(
    *,
    project: Project,
    queue: JobQueue,
    active_runs: MutableMapping[tuple[str, int], Any],
    run_jobs: MutableMapping[tuple[str, int], int],
    project_id: str,
    run_id: int,
    row: Any,
    now: datetime,
    stale_run_grace_seconds: float,
    worker_liveness_window_seconds: float = 90.0,
    queue_timeout_seconds: float | None = None,
    storage_org_id: int | None = None,
) -> ProjectRunStatus:
    key = (project_id, run_id)
    progress = active_runs.get(key)
    job_error = None
    stalled_reason = None
    queue_payload = None
    job: Job | None = None
    public_status = row["status"]

    if row["status"] == "running":
        job_id, job = _project_run_job(
            queue=queue,
            run_jobs=run_jobs,
            project_id=project_id,
            run_id=run_id,
            storage_org_id=storage_org_id,
        )
        queue_payload = (
            job_queue_payload(job, now=now)
            if job is not None
            else {"job_id": job_id, "status": "missing"}
        )
        run_age = _run_age_seconds(row, now)
        grace_elapsed = run_age is not None and run_age >= stale_run_grace_seconds
        no_progress = _run_has_no_progress(row)
        if job is not None and job.status == "failed":
            terminal = _terminalize_v1_queued_receipt(
                project=project,
                project_id=project_id,
                run_id=run_id,
                row=row,
                status="failed",
                job=job,
                code="queue_job_failed",
                message=job.error or "queued project run job failed",
            )
            row = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is not None and row["status"] != "running":
                active_runs.pop(key, None)
                progress = None
                public_status = row["status"]
                job_error = job.error
                queue_payload = job_queue_payload(job, now=now)
            elif terminal.disposition == "reconciliation_required" or (
                terminal.disposition == "conflict"
                and (terminal.reason or "").startswith(
                    "live_writer_requires_owner_terminalization:"
                )
            ):
                public_status = "stalled"
                stalled_reason = "effect_reconciliation_required"
        elif job is not None and job.status == "cancelled":
            RunResultStore(project).request_cancel(run_id)
            terminal = _terminalize_v1_queued_receipt(
                project=project,
                project_id=project_id,
                run_id=run_id,
                row=row,
                status="cancelled",
                job=job,
                require_cancel_intent=True,
            )
            row = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is not None and row["status"] != "running":
                active_runs.pop(key, None)
                progress = None
                public_status = row["status"]
            elif terminal.disposition == "reconciliation_required":
                public_status = "stalled"
                stalled_reason = "effect_reconciliation_required"
            queue_payload = job_queue_payload(job, now=now)
        elif job is not None and job.status == "done":
            if grace_elapsed:
                public_status = "orphaned"
                stalled_reason = "job_done_but_run_still_running"
            else:
                public_status = "running"
            queue_payload = job_queue_payload(job, now=now)
        elif job is not None and job.status == "queued":
            public_status = "queued"
            queued_age = _queued_age_seconds(job, now)
            live_workers = queue.count_live_workers(
                within_seconds=worker_liveness_window_seconds, now=now
            )
            no_live_worker = live_workers == 0
            queue_payload["no_live_worker"] = no_live_worker
            queue_payload["live_workers"] = live_workers
            queue_payload["queued_seconds"] = (
                round(queued_age, 3) if queued_age is not None else None
            )
            timed_out = (
                queue_timeout_seconds is not None
                and queue_timeout_seconds > 0
                and queued_age is not None
                and queued_age >= queue_timeout_seconds
            )
            if timed_out:
                # Fail loudly instead of hanging forever: a queued job that
                # outlived the timeout is terminalized on the queue, the run,
                # and its v1 receipt.
                detail = (
                    f"queued {int(queued_age)}s exceeded the "
                    f"{int(queue_timeout_seconds)}s queue timeout"
                    + (" with no live worker" if no_live_worker else "")
                )
                stored_error = f"queue_timeout: {detail}"
                queue.fail_queued(job.id, stored_error)
                _terminalize_v1_queued_receipt(
                    project=project,
                    project_id=project_id,
                    run_id=run_id,
                    row=row,
                    status="failed",
                    job=job,
                    code="queue_timeout",
                    message=detail,
                )
                row = project.db.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is not None and row["status"] != "running":
                    active_runs.pop(key, None)
                    progress = None
                    public_status = row["status"]
                job_error = stored_error
                refreshed = queue.get(job.id)
                if refreshed is not None:
                    job = refreshed
                    queue_payload = job_queue_payload(job, now=now)
                    queue_payload["no_live_worker"] = no_live_worker
            elif (
                no_live_worker
                and queued_age is not None
                and queued_age >= worker_liveness_window_seconds
            ):
                public_status = "stalled"
                stalled_reason = "no_live_worker"
            elif no_progress and grace_elapsed:
                public_status = "stalled"
                stalled_reason = "queued_after_grace"
        elif job is not None and job.status == "running":
            public_status = "running"
            if queue_payload.get("lease_expired"):
                public_status = "stalled"
                stalled_reason = "lease_expired"
        elif job is None:
            if job_id is None and progress is not None and not progress.done:
                public_status = "running"
            elif no_progress and grace_elapsed:
                public_status = "orphaned"
                stalled_reason = "queue_job_missing"
                _terminalize_v1_queued_receipt(
                    project=project,
                    project_id=project_id,
                    run_id=run_id,
                    row=row,
                    status="failed",
                    code="queue_job_missing",
                    message="queued project run job is missing",
                    receipt_only=True,
                )

    if row["status"] != "running":
        active_runs.pop(key, None)
        run_jobs.pop(key, None)
        progress = None

    return ProjectRunStatus(
        row=row,
        progress=progress,
        job=job,
        public_status=public_status,
        queue_payload=queue_payload,
        stalled_reason=stalled_reason,
        job_error=job_error,
    )


def action_run_status_payload(
    *,
    project_id: str,
    project: Project,
    queue: JobQueue,
    active_runs: MutableMapping[tuple[str, int], Any],
    run_jobs: MutableMapping[tuple[str, int], int],
    run_id: int,
    now: datetime,
    stale_run_grace_seconds: float,
    worker_liveness_window_seconds: float = 90.0,
    queue_timeout_seconds: float | None = None,
    storage_org_id: int | None = None,
) -> dict[str, Any]:
    payload = action_contract.run_status(project_id, project, run_id)
    row = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"no run '{run_id}'")
    status = reconcile_project_run_status(
        project=project,
        queue=queue,
        active_runs=active_runs,
        run_jobs=run_jobs,
        project_id=project_id,
        run_id=run_id,
        row=row,
        now=now,
        stale_run_grace_seconds=stale_run_grace_seconds,
        worker_liveness_window_seconds=worker_liveness_window_seconds,
        queue_timeout_seconds=queue_timeout_seconds,
        storage_org_id=storage_org_id,
    )
    public_status = project_run_status_payload(
        status, run_id=run_id, now=now, project=project
    )
    payload["run"]["status"] = public_status["status"]
    payload["run"]["public_status"] = public_status
    return payload


def cancel_project_run(
    *,
    project: Project,
    queue: JobQueue,
    active_runs: MutableMapping[tuple[str, int], Any],
    run_jobs: MutableMapping[tuple[str, int], int],
    project_id: str,
    run_id: int,
    storage_org_id: int | None = None,
) -> ProjectRunCancelResult:
    key = (project_id, run_id)
    run_store = RunResultStore(project)
    row = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"no run '{run_id}'")

    progress = active_runs.get(key)
    _job_id, job = _project_run_job(
        queue=queue,
        run_jobs=run_jobs,
        project_id=project_id,
        run_id=run_id,
        storage_org_id=storage_org_id,
    )

    initial_status = str(row["status"])
    if initial_status not in {"running", "cancelled"}:
        return ProjectRunCancelResult(
            row=row,
            job=job,
            queue_cancelled=False,
            disposition="conflict",
            cancel_requested=row["cancel_requested_at"] is not None,
        )

    queue_cancelled = False
    if job is not None and job.status == "queued":
        queue_cancelled = queue.cancel(job.id)

    cancel_requested = bool(row["cancel_requested_at"] is not None)
    if initial_status == "running":
        cancel_requested = run_store.request_cancel(run_id)
    # The in-memory flag is only a latency optimization and is set after the
    # durable request lands. Mark its origin so the queued runner leaves the
    # exact writer attempt open for the shared terminal transaction.
    if (
        initial_status == "running"
        and cancel_requested
        and progress is not None
        and not progress.done
    ):
        if hasattr(progress, "cancel_requested"):
            progress.cancel_requested = True
        progress.cancel()
    row = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"no run '{run_id}'")
    terminal_repair = str(row["status"]) == "cancelled"
    if not cancel_requested and not terminal_repair:
        return ProjectRunCancelResult(
            row=row,
            job=job,
            queue_cancelled=queue_cancelled,
            disposition="conflict",
            cancel_requested=row["cancel_requested_at"] is not None,
        )

    terminalization: ProjectRunTerminalizationResult | None = None
    try:
        terminalization = _terminalize_v1_queued_receipt(
            project=project,
            project_id=project_id,
            run_id=run_id,
            row=row,
            status="cancelled",
            job=job,
            require_cancel_intent=not terminal_repair,
            include_terminal_receipt_lookup=terminal_repair,
        )
    except ValueError as exc:
        # A project-run family without its durable receipt has no complete
        # terminal tuple to authorize. Keep the intent and report a conflict;
        # do not turn a missing projection into a partial terminal write.
        if "has no receipt id" not in str(exc):
            raise

    row = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"no run '{run_id}'")

    terminal_tuple_closed = bool(
        terminalization is not None
        and terminalization.disposition in {"terminalized", "already_terminal"}
    )
    if row["status"] == "cancelled" and terminal_tuple_closed:
        disposition = (
            "already_terminal"
            if terminalization is not None
            and terminalization.disposition == "already_terminal"
            else "terminalized"
        )
        active_runs.pop(key, None)
        run_jobs.pop(key, None)
    elif (
        terminalization is not None
        and terminalization.disposition == "reconciliation_required"
    ):
        disposition = "reconciliation_required"
    else:
        live_writer = (
            row["current_attempt_id"] is not None
            or project.db.execute(
                "SELECT 1 FROM execution_attempts "
                "WHERE run_id=? AND state='dispatching' LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )
        owner_must_terminalize = bool(
            terminalization is not None
            and terminalization.disposition == "conflict"
            and (terminalization.reason or "").startswith(
                "live_writer_requires_owner_terminalization:"
            )
        )
        disposition = (
            "cancel_pending" if live_writer and owner_must_terminalize else "conflict"
        )

    return ProjectRunCancelResult(
        row=row,
        job=job,
        queue_cancelled=queue_cancelled,
        disposition=disposition,
        cancel_requested=row["cancel_requested_at"] is not None,
        terminalization=terminalization,
    )
