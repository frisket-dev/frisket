"""Notification candidate producers for operational domain transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from frisket.server.notifications.candidates import (
    NotificationCandidate,
    validate_notification_candidate,
)

if TYPE_CHECKING:
    from frisket.engine.jobs.queue import Job


_RUN_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "stalled",
    "orphaned",
    "needs_user_action",
}
_PROGRESS_STATUSES = {"queued", "running", "partial"}
_SOURCE_HEALTH_STATUSES = {"failed", "stale", "recovered"}
_EXPORT_STATUSES = {"ready", "failed"}
_DELIVERY_HEALTH_STATUSES = {"failed", "cancelled", "skipped"}

NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND = "notification_delivery"


def run_transition_notification_candidate(
    *,
    project_id: str,
    run_id: int,
    status: str,
    action_kind: str | None = None,
    stalled_reason: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    completed_rows: int | None = None,
    failed_rows: int | None = None,
    payload: dict[str, Any] | None = None,
) -> NotificationCandidate | None:
    normalized_status = _normalize_status(status)
    if normalized_status in _PROGRESS_STATUSES:
        return None
    if normalized_status not in _RUN_STATUSES:
        return None
    reason_key = _reason_key(
        normalized_status,
        stalled_reason=stalled_reason,
        reason=reason,
    )
    source_ref: dict[str, Any] = {
        "project_id": project_id,
        "run_id": int(run_id),
        "status": normalized_status,
    }
    if stalled_reason:
        source_ref["stalled_reason"] = stalled_reason
    if reason:
        source_ref["reason"] = reason
    candidate_payload: dict[str, Any] = dict(payload or {})
    if action_kind:
        candidate_payload["action_kind"] = action_kind
    if error:
        candidate_payload["error"] = error
    if completed_rows is not None:
        candidate_payload["completed_rows"] = int(completed_rows)
    if failed_rows is not None:
        candidate_payload["failed_rows"] = int(failed_rows)
    candidate = {
        "source_kind": "run",
        "source_ref": source_ref,
        "dedupe_key": f"run:{project_id}:{int(run_id)}:{reason_key}",
        "title": _run_title(normalized_status, run_id=int(run_id)),
        "summary": _run_summary(
            normalized_status,
            run_id=int(run_id),
            stalled_reason=stalled_reason,
            reason=reason,
            error=error,
        ),
        "severity": _run_severity(normalized_status, stalled_reason=stalled_reason),
        "deep_link": {"kind": "run", "project_id": project_id, "run_id": int(run_id)},
        "payload": candidate_payload,
    }
    return validate_notification_candidate(candidate)


def job_transition_notification_candidate(
    job: Job,
    *,
    public_status: str | None = None,
    stalled_reason: str | None = None,
    prefer_run_candidate: bool = True,
) -> NotificationCandidate | None:
    reason = _job_reason(
        job, public_status=public_status, stalled_reason=stalled_reason
    )
    if reason is None:
        return None
    job_run_id = _job_run_id(job)
    if prefer_run_candidate and job.kind == "project.run" and job_run_id is not None:
        return None
    workspace_root = job.workspace_root or str(job.payload.get("workspace_root") or "")
    source_ref: dict[str, Any] = {
        "job_id": job.id,
        "job_kind": job.kind,
        "status": reason.split(":", 1)[0],
    }
    if job.project_id:
        source_ref["project_id"] = job.project_id
    if job_run_id is not None:
        source_ref["run_id"] = job_run_id
    if job.source_id is not None:
        source_ref["source_id"] = job.source_id
    if workspace_root:
        source_ref["workspace_root"] = workspace_root
    if stalled_reason:
        source_ref["stalled_reason"] = stalled_reason
    payload: dict[str, Any] = {
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
    }
    if job.error:
        payload["error"] = job.error
    dedupe_root = workspace_root or (job.project_id or "workspace")
    candidate = {
        "source_kind": "job",
        "source_ref": source_ref,
        "dedupe_key": f"job:{dedupe_root}:{job.id}:{job.kind}:{reason}",
        "title": _job_title(job, reason),
        "summary": _job_summary(job, reason, stalled_reason=stalled_reason),
        "severity": _job_severity(reason),
        "deep_link": {
            "kind": "job",
            "project_id": job.project_id,
            "job_id": job.id,
        },
        "payload": payload,
    }
    return validate_notification_candidate(candidate)


def source_health_notification_candidate(
    *,
    project_id: str,
    source_id: int,
    status: str,
    source_name: str | None = None,
    source_run_id: int | None = None,
    stale_reason: str | None = None,
    error: str | None = None,
    schedule: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NotificationCandidate | None:
    normalized_status = _normalize_source_health_status(status)
    if normalized_status not in _SOURCE_HEALTH_STATUSES:
        return None
    source_ref: dict[str, Any] = {
        "project_id": project_id,
        "source_id": int(source_id),
        "status": normalized_status,
    }
    if source_run_id is not None:
        source_ref["source_run_id"] = int(source_run_id)
    if stale_reason:
        source_ref["stale_reason"] = stale_reason
    candidate_payload: dict[str, Any] = dict(payload or {})
    if error:
        candidate_payload["error"] = error
    if schedule:
        candidate_payload["schedule"] = schedule
    label = source_name or f"Source {int(source_id)}"
    candidate = {
        "source_kind": "source",
        "source_ref": source_ref,
        "dedupe_key": (
            f"source:{project_id}:{int(source_id)}:"
            f"{_source_health_reason_key(normalized_status, source_run_id, stale_reason)}"
        ),
        "title": _source_health_title(normalized_status, label),
        "summary": _source_health_summary(
            normalized_status,
            label,
            stale_reason=stale_reason,
            error=error,
        ),
        "severity": "info" if normalized_status == "recovered" else "warning",
        "deep_link": {
            "kind": "source",
            "project_id": project_id,
            "source_id": int(source_id),
        },
        "payload": candidate_payload,
    }
    return validate_notification_candidate(candidate)


def export_notification_candidate(
    *,
    project_id: str,
    export_kind: str,
    status: str,
    receipt_id: str | None = None,
    artifact_ref: dict[str, Any] | None = None,
    error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NotificationCandidate | None:
    normalized_status = _normalize_export_status(status)
    if normalized_status not in _EXPORT_STATUSES:
        return None
    source_ref: dict[str, Any] = {
        "project_id": project_id,
        "export_kind": export_kind,
        "status": normalized_status,
    }
    if receipt_id:
        source_ref["receipt_id"] = receipt_id
    candidate_payload: dict[str, Any] = dict(payload or {})
    if artifact_ref:
        candidate_payload["artifact_ref"] = artifact_ref
    if error:
        candidate_payload["error"] = error
    candidate = {
        "source_kind": "export",
        "source_ref": source_ref,
        "dedupe_key": (
            f"export:{project_id}:{export_kind}:"
            f"{receipt_id or _artifact_dedupe_ref(artifact_ref)}:{normalized_status}"
        ),
        "title": _export_title(export_kind, normalized_status),
        "summary": _export_summary(export_kind, normalized_status, error=error),
        "severity": "warning" if normalized_status == "failed" else "info",
        "deep_link": {
            "kind": "export",
            "project_id": project_id,
            "export_kind": export_kind,
            "receipt_id": receipt_id,
        },
        "payload": candidate_payload,
    }
    return validate_notification_candidate(candidate)


def delivery_health_notification_candidate(
    *,
    project_id: str,
    request_id: int,
    status: str,
    route_id: int | None = None,
    channel_id: int | None = None,
    notification_id: int | None = None,
    job_id: int | None = None,
    error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NotificationCandidate | None:
    normalized_status = _normalize_status(status)
    if normalized_status not in _DELIVERY_HEALTH_STATUSES:
        return None
    source_ref: dict[str, Any] = {
        "project_id": project_id,
        "request_id": int(request_id),
        "status": normalized_status,
    }
    for key, value in (
        ("route_id", route_id),
        ("channel_id", channel_id),
        ("notification_id", notification_id),
        ("job_id", job_id),
    ):
        if value is not None:
            source_ref[key] = int(value)
    candidate_payload: dict[str, Any] = dict(payload or {})
    if error:
        candidate_payload["error"] = error
    candidate = {
        "source_kind": NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND,
        "source_ref": source_ref,
        "dedupe_key": (
            f"notification_delivery:{project_id}:{int(request_id)}:{normalized_status}"
        ),
        "title": f"Notification delivery {normalized_status}",
        "summary": _delivery_health_summary(
            request_id=int(request_id),
            status=normalized_status,
            error=error,
        ),
        "severity": "warning" if normalized_status == "failed" else "info",
        "deep_link": {
            "kind": "notification_delivery_request",
            "project_id": project_id,
            "request_id": int(request_id),
        },
        "payload": candidate_payload,
    }
    return validate_notification_candidate(candidate)


def _normalize_status(status: str) -> str:
    text = str(status or "").strip().lower()
    return "cancelled" if text == "canceled" else text


def _normalize_source_health_status(status: str) -> str:
    text = str(status or "").strip().lower()
    if text in {"error", "failing", "failure"}:
        return "failed"
    if text in {"ok", "healthy", "recovered"}:
        return "recovered"
    return text


def _normalize_export_status(status: str) -> str:
    text = str(status or "").strip().lower()
    if text in {"completed", "done", "sent"}:
        return "ready"
    if text in {"error", "failed"}:
        return "failed"
    return text


def _reason_key(
    status: str,
    *,
    stalled_reason: str | None,
    reason: str | None,
) -> str:
    if status in {"stalled", "orphaned"} and stalled_reason:
        return f"{status}:{stalled_reason}"
    if status == "needs_user_action" and reason:
        return f"{status}:{reason}"
    return status


def _source_health_reason_key(
    status: str,
    source_run_id: int | None,
    stale_reason: str | None,
) -> str:
    if source_run_id is not None:
        return f"{status}:{int(source_run_id)}"
    if stale_reason:
        return f"{status}:{stale_reason}"
    return status


def _source_health_title(status: str, label: str) -> str:
    if status == "failed":
        return f"{label} poll failed"
    if status == "stale":
        return f"{label} is stale"
    return f"{label} recovered"


def _source_health_summary(
    status: str,
    label: str,
    *,
    stale_reason: str | None,
    error: str | None,
) -> str:
    if status == "failed" and error:
        return f"{label} source poll failed: {error}"
    if status == "stale" and stale_reason:
        return f"{label} source is stale: {stale_reason}"
    if status == "recovered":
        return f"{label} source recovered after a prior failure or stale interval."
    return f"{label} source is {status}."


def _artifact_dedupe_ref(artifact_ref: dict[str, Any] | None) -> str:
    if not artifact_ref:
        return "artifact"
    for key in ("sha256", "path", "url", "id"):
        value = artifact_ref.get(key)
        if value:
            return str(value)
    return "artifact"


def _export_title(export_kind: str, status: str) -> str:
    label = export_kind.replace(".", " ").replace("_", " ")
    return f"{label.title()} {'failed' if status == 'failed' else 'ready'}"


def _export_summary(export_kind: str, status: str, *, error: str | None) -> str:
    if status == "failed" and error:
        return f"{export_kind} export failed: {error}"
    if status == "failed":
        return f"{export_kind} export failed."
    return f"{export_kind} export is ready."


def _delivery_health_summary(
    *,
    request_id: int,
    status: str,
    error: str | None,
) -> str:
    if error:
        return f"Notification delivery request {request_id} is {status}: {error}"
    return f"Notification delivery request {request_id} is {status}."


def _run_severity(status: str, *, stalled_reason: str | None) -> str:
    if status == "orphaned" and stalled_reason == "queue_job_missing":
        return "critical"
    if status in {"failed", "stalled", "orphaned", "needs_user_action"}:
        return "warning"
    return "info"


def _run_title(status: str, *, run_id: int) -> str:
    labels = {
        "completed": "Run completed",
        "failed": "Run failed",
        "cancelled": "Run cancelled",
        "stalled": "Run stalled",
        "orphaned": "Run needs inspection",
        "needs_user_action": "Run needs user action",
    }
    return f"{labels.get(status, 'Run updated')} #{run_id}"


def _run_summary(
    status: str,
    *,
    run_id: int,
    stalled_reason: str | None,
    reason: str | None,
    error: str | None,
) -> str:
    if status == "failed" and error:
        return f"Run {run_id} failed: {error}"
    if status in {"stalled", "orphaned"} and stalled_reason:
        return f"Run {run_id} is {status}: {stalled_reason}"
    if status == "needs_user_action" and reason:
        return f"Run {run_id} needs user action: {reason}"
    return f"Run {run_id} is {status}."


def _job_reason(
    job: Job,
    *,
    public_status: str | None,
    stalled_reason: str | None,
) -> str | None:
    if public_status:
        normalized = _normalize_status(public_status)
        if normalized == "stalled" and stalled_reason:
            return f"stalled:{stalled_reason}"
        if normalized in {"orphaned", "stalled"}:
            return normalized
    status = _normalize_status(job.status)
    if status == "done":
        return "completed"
    if status in {"failed", "cancelled"}:
        return status
    return None


def _job_run_id(job: Job) -> int | None:
    if job.run_id is not None:
        return int(job.run_id)
    try:
        return int(job.payload.get("run_id"))
    except (TypeError, ValueError):
        return None


def _job_severity(reason: str) -> str:
    if reason.startswith("orphaned"):
        return "critical"
    if reason.startswith(("failed", "stalled")):
        return "warning"
    return "info"


def _job_title(job: Job, reason: str) -> str:
    label = reason.split(":", 1)[0].replace("_", " ")
    return f"Job {label} #{job.id}"


def _job_summary(
    job: Job,
    reason: str,
    *,
    stalled_reason: str | None,
) -> str:
    if reason.startswith("failed") and job.error:
        return f"{job.kind} job {job.id} failed: {job.error}"
    if reason.startswith("stalled") and stalled_reason:
        return f"{job.kind} job {job.id} is stalled: {stalled_reason}"
    return f"{job.kind} job {job.id} is {reason.split(':', 1)[0]}."
