"""Notification candidate producers for operational domain transitions."""

from __future__ import annotations

from typing import Any

from frisket.notifications.candidates import (
    NotificationCandidate,
    validate_notification_candidate,
)


_RUN_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "stalled",
    "orphaned",
    "needs_user_action",
}
_PROGRESS_STATUSES = {"queued", "running"}
_FEED_HEALTH_STATUSES = {"failed", "stale", "recovered"}
_EXPORT_STATUSES = {"ready", "failed"}
_DELIVERY_HEALTH_STATUSES = {"failed", "cancelled", "skipped"}

NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND = "notification_delivery"
RUN_SOURCE_KIND = "run"
IMPORT_SOURCE_KIND = "import"


def requires_explicit_route(source_kind: str, severity: str) -> bool:
    """Whether only a route naming this source kind may receive the item.

    Info-level run and import outcomes (clean completions, plain
    cancellations) are opt-in: a person who runs many actions or imports
    receives them only by routing that source kind explicitly. A route for
    every source kind never receives them, and no item is stored unless such
    a route asks for it.
    """
    return source_kind in {RUN_SOURCE_KIND, IMPORT_SOURCE_KIND} and severity == "info"


def import_preparation_notification_candidate(
    *,
    project_id: str,
    import_ref: str,
    sheet_id: int,
    read_pages: int,
    failed_pages: int,
) -> NotificationCandidate | None:
    """The outcome of reading an import's page text, once every page settled."""
    if read_pages < 0 or failed_pages < 0 or read_pages + failed_pages == 0:
        return None
    pages = _count_label(failed_pages, "page")
    candidate: dict[str, Any] = {
        "source_kind": IMPORT_SOURCE_KIND,
        "source_ref": {
            "project_id": project_id,
            "import_ref": import_ref,
            "sheet_id": int(sheet_id),
        },
        "dedupe_key": f"import:{project_id}:{import_ref}:{read_pages}:{failed_pages}",
        "title": (
            f"Import finished with {pages} not read"
            if failed_pages
            else "Import text is ready"
        ),
        "summary": (
            f"Text could not be read from {pages}. Retry them from the import "
            "progress on the sheet."
            if failed_pages
            else f"Text was read from {_count_label(read_pages, 'page')}."
        ),
        "severity": "warning" if failed_pages else "info",
        "deep_link": {
            "kind": "sheet",
            "project_id": project_id,
            "sheet_id": int(sheet_id),
        },
        "payload": {"read_pages": int(read_pages), "failed_pages": int(failed_pages)},
    }
    return validate_notification_candidate(candidate)


def _count_label(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


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
    finished_at: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NotificationCandidate | None:
    normalized_status = _normalize_status(status)
    failed_row_count = _completed_failed_row_count(normalized_status, failed_rows)
    if failed_row_count is not None:
        normalized_status = "completed"
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
    candidate_payload = _run_payload(
        payload,
        action_kind=action_kind,
        error=error,
        completed_rows=completed_rows,
        failed_rows=failed_rows,
    )
    candidate: dict[str, Any] = {
        "source_kind": RUN_SOURCE_KIND,
        "source_ref": source_ref,
        "dedupe_key": f"run:{project_id}:{int(run_id)}:{reason_key}",
        "title": (
            _run_title(normalized_status, run_id=int(run_id))
            if failed_row_count is None
            else f"Run completed with {_failed_rows_label(failed_row_count)} #{run_id}"
        ),
        "summary": (
            _run_summary(
                normalized_status,
                run_id=int(run_id),
                stalled_reason=stalled_reason,
                reason=reason,
                error=error,
            )
            if failed_row_count is None
            else f"Run {run_id} completed with {_failed_rows_label(failed_row_count)}."
        ),
        "severity": _run_severity(
            normalized_status,
            stalled_reason=stalled_reason,
            with_failed_rows=failed_row_count is not None,
        ),
        "deep_link": {"kind": "run", "project_id": project_id, "run_id": int(run_id)},
        "payload": candidate_payload,
    }
    if finished_at:
        candidate["occurred_at"] = finished_at
    return validate_notification_candidate(candidate)


def feed_health_notification_candidate(
    *,
    project_id: str,
    feed_id: int,
    status: str,
    feed_name: str | None = None,
    feed_check_id: int | None = None,
    stale_reason: str | None = None,
    error: str | None = None,
    schedule: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NotificationCandidate | None:
    normalized_status = _normalize_feed_health_status(status)
    if normalized_status not in _FEED_HEALTH_STATUSES:
        return None
    source_ref: dict[str, Any] = {
        "project_id": project_id,
        "feed_id": int(feed_id),
        "status": normalized_status,
    }
    if feed_check_id is not None:
        source_ref["feed_check_id"] = int(feed_check_id)
    if stale_reason:
        source_ref["stale_reason"] = stale_reason
    candidate_payload: dict[str, Any] = dict(payload or {})
    if error:
        candidate_payload["error"] = error
    if schedule:
        candidate_payload["schedule"] = schedule
    label = feed_name or f"Feed {int(feed_id)}"
    candidate = {
        "source_kind": "feed",
        "source_ref": source_ref,
        "dedupe_key": (
            f"feed:{project_id}:{int(feed_id)}:"
            f"{_feed_health_reason_key(normalized_status, feed_check_id, stale_reason)}"
        ),
        "title": _feed_health_title(normalized_status, label),
        "summary": _feed_health_summary(
            normalized_status,
            label,
            stale_reason=stale_reason,
            error=error,
        ),
        "severity": "info" if normalized_status == "recovered" else "warning",
        "deep_link": {
            "kind": "feed",
            "project_id": project_id,
            "feed_id": int(feed_id),
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


def _normalize_feed_health_status(status: str) -> str:
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


def _feed_health_reason_key(
    status: str,
    feed_check_id: int | None,
    stale_reason: str | None,
) -> str:
    if feed_check_id is not None:
        return f"{status}:{int(feed_check_id)}"
    if stale_reason:
        return f"{status}:{stale_reason}"
    return status


def _feed_health_title(status: str, label: str) -> str:
    if status == "failed":
        return f"{label} check failed"
    if status == "stale":
        return f"{label} is stale"
    return f"{label} recovered"


def _feed_health_summary(
    status: str,
    label: str,
    *,
    stale_reason: str | None,
    error: str | None,
) -> str:
    if status == "failed" and error:
        return f"{label} feed check failed: {error}"
    if status == "stale" and stale_reason:
        return f"{label} feed is stale: {stale_reason}"
    if status == "recovered":
        return f"{label} feed recovered after a prior failure or stale interval."
    return f"{label} feed is {status}."


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


def _run_severity(
    status: str, *, stalled_reason: str | None, with_failed_rows: bool
) -> str:
    if status == "orphaned" and stalled_reason == "queue_job_missing":
        return "critical"
    if status in {"failed", "stalled", "orphaned", "needs_user_action"}:
        return "warning"
    if with_failed_rows:
        return "warning"
    return "info"


def _run_payload(
    payload: dict[str, Any] | None,
    *,
    action_kind: str | None,
    error: str | None,
    completed_rows: int | None,
    failed_rows: int | None,
) -> dict[str, Any]:
    candidate_payload: dict[str, Any] = dict(payload or {})
    if action_kind:
        candidate_payload["action_kind"] = action_kind
    if error:
        candidate_payload["error"] = error
    if completed_rows is not None:
        candidate_payload["completed_rows"] = int(completed_rows)
    if failed_rows is not None:
        candidate_payload["failed_rows"] = int(failed_rows)
    return candidate_payload


def _completed_failed_row_count(status: str, failed_rows: int | None) -> int | None:
    """Failed rows of a completion that is not clean, else None.

    A partial result is a completion in which some rows failed, even when the
    count is unknown (reported as 0).
    """
    count = int(failed_rows or 0)
    if status == "partial" or (status == "completed" and count > 0):
        return count
    return None


def _failed_rows_label(count: int) -> str:
    if count <= 0:
        return "failed rows"
    return f"{count} failed row{'s' if count != 1 else ''}"


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
