"""Safe, public projections for queue jobs.

Queue payloads carry internal execution details such as runner specs,
workspace roots, hosted funding snapshots, and raw provider inputs. Product and
admin routes should publish bounded DTOs derived from those payloads instead of
leaking the raw job envelope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.engine.jobs.queue import ACTION_RUN_KIND, Job
from frisket.engine.jobs.runs import RUN_PROJECT_KIND


ValueSanitizer = Callable[[Any], Any]
MessageSanitizer = Callable[[str | None], str | None]

ADMIN_JOB_REF_KEYS = (
    "org_id",
    "project_id",
    "run_id",
    "source_id",
    "sheet_id",
    "trace_id",
)
INTEGER_JOB_REF_KEYS = frozenset({"org_id", "run_id", "source_id", "sheet_id"})
ADMIN_JOB_DIAGNOSTIC_KEYS = (
    "total_rows",
    "completed_rows",
    "failed_rows",
    "processed_rows",
    "remaining_rows",
    "cost_estimate",
    "cost_actual",
    "provider",
    "model",
    "engine",
)
SOURCE_POLL_KIND = "source.poll"
ENCLOSURE_DOWNLOAD_KIND = "enclosure.download"

# runs-are-jobs-parity-v1: a run whose action placement is INLINE never gets a
# queue `Job` row — it executes
# synchronously inside the request that launched it. Its `runs` table row is the
# only durable record, so the jobs LISTING projects it directly from that row
# instead of double-writing a queue job. `RUN_INLINE_KIND` marks these
# synthesized entries so callers can distinguish them from real queue jobs.
RUN_INLINE_KIND = "run.inline"

# runs.status ('running'|'completed'|'failed'|'cancelled', store/schema.py)
# mapped onto the queue Job status vocabulary (STATUSES in jobs/queue.py) so a
# run-projected entry renders through the exact same job-status UI as a real
# queue job. INLINE runs never observe 'queued' — they start executing the
# instant the `runs` row is created.
_RUN_STATUS_TO_JOB_STATUS = {
    "running": "running",
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}

DEFAULT_JOB_RESULT_SUMMARY_KEYS = ("status",)
JOB_RESULT_SUMMARY_KEYS_BY_KIND = {
    RUN_PROJECT_KIND: (
        "status",
        "total",
        "completed",
        "completed_rows",
        "failed",
        "failed_rows",
        "skipped",
        "action_kind",
        "receipt_id",
    ),
    SOURCE_POLL_KIND: (
        "skipped",
        "reason",
        "action_kind",
        "receipt_id",
        "run_id",
        "new_rows",
        "revisions",
        "sheet_id",
        "op_id",
        "enclosure_jobs",
    ),
    ENCLOSURE_DOWNLOAD_KIND: (
        "status",
        "row_id",
        "op_id",
        "error",
    ),
    ACTION_RUN_KIND: (
        "status",
        "action_kind",
        "receipt_id",
        "index_id",
        "total_items",
        "refreshed",
        "skipped_current",
        "ready_items",
        "error_code",
        "blocked",
    ),
}


def _identity(value: Any) -> Any:
    return value


def _identity_message(value: str | None) -> str | None:
    return value


def parse_status_time(value: Any) -> datetime | None:
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


def status_time(value: Any, *, zulu: bool = False) -> str | None:
    parsed = parse_status_time(value)
    if parsed is None:
        return None
    out = parsed.isoformat()
    return out.replace("+00:00", "Z") if zulu else out


def job_is_stalled(job: Job, *, now: datetime) -> bool:
    lease_expires_at = parse_status_time(job.lease_expires_at)
    return (
        job.status == "running"
        and lease_expires_at is not None
        and lease_expires_at < now
    )


def job_project_id(job: Job) -> str | None:
    raw = (
        job.project_id if job.project_id is not None else job.payload.get("project_id")
    )
    return str(raw) if raw is not None else None


def job_run_id(job: Job) -> int | None:
    try:
        raw = job.run_id if job.run_id is not None else job.payload.get("run_id")
        return int(raw)
    except (TypeError, ValueError):
        return None


def action_metadata_for_job(job: Job) -> dict[str, str]:
    action_kind = job.action_kind
    spec = job.payload.get("spec")
    if job.kind == ACTION_RUN_KIND:
        envelope = job.payload.get("action_job")
        if isinstance(envelope, dict):
            snapshot = envelope.get("resolved_snapshot")
            if isinstance(snapshot, Mapping):
                plugin_kind = snapshot.get("action_kind") or envelope.get("action_kind")
                if isinstance(plugin_kind, str) and plugin_kind:
                    title = str(snapshot.get("title") or plugin_kind)
                    return {
                        "action_kind": plugin_kind,
                        "action_name": title,
                    }
    if action_kind is None and isinstance(spec, dict):
        action_kind = spec.get("kind") or spec.get("action_kind")
    action_kind = action_kind or job.payload.get("action_kind")
    if action_kind is None and job.kind == ACTION_RUN_KIND:
        envelope = job.payload.get("action_job")
        if isinstance(envelope, dict):
            action_kind = envelope.get("action_kind")
            snapshot = envelope.get("resolved_snapshot")
            if isinstance(snapshot, Mapping) and isinstance(action_kind, str):
                title = str(snapshot.get("title") or action_kind)
                return {
                    "action_kind": action_kind,
                    "action_name": title,
                }
    if job.kind != RUN_PROJECT_KIND and action_kind is None:
        return {}
    if action_kind is None and isinstance(spec, dict):
        action_kind = spec.get("action_kind")
    if action_kind is None:
        return action_metadata_for_action_kind(None)
    return action_metadata_for_action_kind(action_kind)


def job_queue_payload(job: Job, *, now: datetime) -> dict[str, Any]:
    lease_expires_at = parse_status_time(job.lease_expires_at)
    lease_expired = (
        job.status == "running"
        and lease_expires_at is not None
        and lease_expires_at <= now
    )
    payload = {
        "job_id": job.id,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "locked_by": job.locked_by,
        "locked_at": status_time(job.locked_at),
        "lease_expires_at": status_time(job.lease_expires_at),
        "lease_expired": lease_expired,
        "created_at": status_time(job.created_at),
        "started_at": status_time(job.started_at),
        "finished_at": status_time(job.finished_at),
        "error": job.error,
    }
    payload.update(action_metadata_for_job(job))
    return payload


def job_payload_ref(job_kind: str, refs: Mapping[str, Any]) -> dict[str, Any]:
    if refs.get("run_id") is not None:
        return {"kind": "run", "run_id": refs["run_id"]}
    if refs.get("source_id") is not None:
        return {"kind": "source", "source_id": refs["source_id"]}
    if refs.get("project_id") is not None:
        return {"kind": "project", "project_id": refs["project_id"]}
    return {"kind": job_kind}


def project_action_job_payload(
    job: Job, *, project_id: str, now: datetime
) -> dict[str, Any]:
    queue_payload = job_queue_payload(job, now=now)
    run_id = job_run_id(job)
    result_summary = job_result_summary(job.kind, job.result)
    receipt_id = (
        job.receipt_id
        or job.payload.get("v1_receipt_id")
        or result_summary.get("receipt_id")
    )
    payload_ref = (
        {"kind": "run", "run_id": run_id} if run_id is not None else {"kind": job.kind}
    )
    return {
        "schema_version": "frisket.job.v1",
        "project_id": project_id,
        "job_id": queue_payload["job_id"],
        "kind": job.kind,
        "run_id": run_id,
        "receipt_id": str(receipt_id) if receipt_id is not None else None,
        "payload_ref": payload_ref,
        "status": queue_payload["status"],
        "action_kind": queue_payload.get("action_kind"),
        "action_name": queue_payload.get("action_name"),
        "result_summary": result_summary,
        "attempts": queue_payload["attempts"],
        "max_attempts": queue_payload["max_attempts"],
        "lease": {
            "locked_by": queue_payload["locked_by"],
            "locked_at": queue_payload["locked_at"],
            "lease_expires_at": queue_payload["lease_expires_at"],
            "lease_expired": queue_payload["lease_expired"],
        },
        "timing": {
            "created_at": queue_payload["created_at"],
            "started_at": queue_payload["started_at"],
            "finished_at": queue_payload["finished_at"],
        },
        "error": queue_payload["error"],
    }


def run_inline_job_id(run_id: int) -> int:
    """The synthetic job id for an INLINE run's projected jobs-list entry.

    Negative sentinel — real queue job ids are a positive autoincrement, so
    this can never collide. Mirrors the existing client-side convention
    (workbench/dockJobSummary.ts's `syntheticErrorJobFromRun`:
    ``jobId: -(Math.abs(runId))``) so a run that surfaces both server-projected
    (this) and client-synthesized (the active run, before its job row exists)
    resolves to the SAME id.
    """

    return -abs(int(run_id))


def run_id_from_inline_job_id(job_id: int) -> int | None:
    """Inverse of `run_inline_job_id`, or None for a real (positive) job id."""

    return -job_id if job_id < 0 else None


def run_inline_job_payload(
    row: Mapping[str, Any], *, project_id: str, now: datetime
) -> dict[str, Any]:
    """Project one `runs` table row into the same `frisket.job.v1` shape a real
    queue job renders as (project_action_job_payload's sibling), for a run whose
    action placement never wrote a queue Job row (runs-are-jobs-parity-v1)."""

    run_id = int(row["id"])
    run_status = str(row["status"] or "running")
    status = _RUN_STATUS_TO_JOB_STATUS.get(run_status, run_status)
    metadata = action_metadata_for_action_kind(run_row_action_kind(row))
    total = row["total_rows"]
    completed = row["completed_rows"]
    failed = row["failed_rows"]
    result_summary: dict[str, Any] = {"status": run_status}
    if total is not None:
        result_summary["total"] = int(total)
    if completed is not None:
        result_summary["completed"] = int(completed)
        result_summary["completed_rows"] = int(completed)
    if failed is not None:
        result_summary["failed"] = int(failed)
        result_summary["failed_rows"] = int(failed)
    result_summary["action_kind"] = metadata["action_kind"]
    started_at = status_time(row["started_at"])
    finished_at = status_time(row["finished_at"])
    return {
        "schema_version": "frisket.job.v1",
        "project_id": project_id,
        "job_id": run_inline_job_id(run_id),
        "kind": RUN_INLINE_KIND,
        "run_id": run_id,
        "receipt_id": None,
        "payload_ref": {"kind": "run", "run_id": run_id},
        "status": status,
        "action_kind": metadata["action_kind"],
        "action_name": metadata["action_name"],
        "result_summary": result_summary,
        "attempts": 1,
        "max_attempts": 1,
        "lease": {
            "locked_by": row["worker_version"],
            "locked_at": None,
            "lease_expires_at": None,
            "lease_expired": False,
        },
        "timing": {
            "created_at": started_at,
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "error": None,
    }


def projected_job_values(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    sanitize_value: ValueSanitizer = _identity,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        out[key] = sanitize_value(value)
    return out


def _projected_ref_value(
    key: str,
    value: Any,
    *,
    sanitize_value: ValueSanitizer,
) -> Any:
    sanitized = sanitize_value(value)
    if key not in INTEGER_JOB_REF_KEYS or isinstance(sanitized, bool):
        return sanitized
    try:
        return int(sanitized)
    except (TypeError, ValueError):
        return sanitized


def projected_job_refs(
    payload: Mapping[str, Any],
    *,
    sanitize_value: ValueSanitizer = _identity,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ADMIN_JOB_REF_KEYS:
        value = payload.get(key)
        if value is None or value == "":
            continue
        out[key] = _projected_ref_value(
            key,
            value,
            sanitize_value=sanitize_value,
        )
    return out


def overlay_job_ref_fields(
    refs: dict[str, Any],
    job: Job,
    *,
    sanitize_value: ValueSanitizer = _identity,
) -> None:
    for key in ADMIN_JOB_REF_KEYS:
        value = getattr(job, key, None)
        if value is not None and value != "":
            refs[key] = _projected_ref_value(
                key,
                value,
                sanitize_value=sanitize_value,
            )


def job_result_summary(
    job_kind: str,
    result: Any,
    *,
    sanitize_value: ValueSanitizer = _identity,
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    keys = JOB_RESULT_SUMMARY_KEYS_BY_KIND.get(
        job_kind,
        DEFAULT_JOB_RESULT_SUMMARY_KEYS,
    )
    return projected_job_values(
        result,
        keys,
        sanitize_value=sanitize_value,
    )


def admin_job_payload(
    job: Job,
    *,
    now: datetime,
    sanitize_value: ValueSanitizer = _identity,
) -> dict[str, Any]:
    refs = projected_job_refs(
        job.payload or {},
        sanitize_value=sanitize_value,
    )
    overlay_job_ref_fields(refs, job, sanitize_value=sanitize_value)
    return {
        "schema_version": "frisket.admin_job.v1",
        "id": job.id,
        "kind": job.kind,
        **action_metadata_for_job(job),
        "refs": refs,
        "payload_ref": job_payload_ref(job.kind, refs),
        "diagnostics": projected_job_values(
            job.payload or {},
            ADMIN_JOB_DIAGNOSTIC_KEYS,
            sanitize_value=sanitize_value,
        ),
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "locked_by": job.locked_by,
        "locked_at": status_time(job.locked_at, zulu=True),
        "lease_expires_at": status_time(job.lease_expires_at, zulu=True),
        "available_at": status_time(job.available_at, zulu=True),
        "created_at": status_time(job.created_at, zulu=True),
        "started_at": status_time(job.started_at, zulu=True),
        "finished_at": status_time(job.finished_at, zulu=True),
        "result_summary": job_result_summary(
            job.kind,
            job.result,
            sanitize_value=sanitize_value,
        ),
        "error": job.error,
        "stalled": job_is_stalled(job, now=now),
    }


def diagnostic_job_context(
    job: Job,
    *,
    now: datetime,
    sanitize_value: ValueSanitizer = _identity,
) -> dict[str, Any]:
    refs = projected_job_refs(
        job.payload or {},
        sanitize_value=sanitize_value,
    )
    overlay_job_ref_fields(refs, job, sanitize_value=sanitize_value)
    return {
        "schema_version": "frisket.job_diagnostic.v1",
        "job_id": job.id,
        "kind": job.kind,
        **action_metadata_for_job(job),
        "refs": refs,
        "payload_ref": job_payload_ref(job.kind, refs),
        "diagnostics": projected_job_values(
            job.payload or {},
            ADMIN_JOB_DIAGNOSTIC_KEYS,
            sanitize_value=sanitize_value,
        ),
        "status": "stalled" if job_is_stalled(job, now=now) else job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "locked_by": sanitize_value(job.locked_by),
        "lease": {
            "locked_at": status_time(job.locked_at, zulu=True),
            "lease_expires_at": status_time(job.lease_expires_at, zulu=True),
            "lease_expired": job_is_stalled(job, now=now),
        },
        "timing": {
            "created_at": status_time(job.created_at, zulu=True),
            "started_at": status_time(job.started_at, zulu=True),
            "finished_at": status_time(job.finished_at, zulu=True),
        },
        "result_summary": job_result_summary(
            job.kind,
            job.result,
            sanitize_value=sanitize_value,
        ),
    }


def diagnostic_job_event(
    job: Job,
    *,
    now: datetime,
    sanitize_value: ValueSanitizer = _identity,
    sanitize_message: MessageSanitizer = _identity_message,
) -> dict[str, Any] | None:
    stalled = job_is_stalled(job, now=now)
    if job.status != "failed" and not stalled:
        return None
    refs = projected_job_refs(
        job.payload or {},
        sanitize_value=sanitize_value,
    )
    overlay_job_ref_fields(refs, job, sanitize_value=sanitize_value)
    metadata = action_metadata_for_job(job)
    message = job.error or (
        "worker lease expired" if stalled else f"job is {job.status}"
    )
    return {
        "id": f"job:{job.id}",
        "kind": "job",
        "source": "worker",
        "severity": "error",
        "name": metadata.get("action_name") or job.kind,
        "message": sanitize_message(message),
        "stack": None,
        "route": None,
        "org_id": refs.get("org_id"),
        "user_id": None,
        "project_id": refs.get("project_id"),
        "sheet_id": refs.get("sheet_id"),
        "run_id": refs.get("run_id"),
        "job_id": str(job.id),
        "trace_id": refs.get("trace_id"),
        **metadata,
        "context": diagnostic_job_context(
            job,
            now=now,
            sanitize_value=sanitize_value,
        ),
        "at": status_time(
            job.finished_at or job.lease_expires_at or job.created_at,
            zulu=True,
        )
        or status_time(now, zulu=True),
    }
