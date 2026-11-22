"""Read-only feed health projection.

The health payload is deliberately derived from existing source lifecycle
tables and the workspace queue. It does not create monitoring state, and it
redacts config/cursor/provider details by default.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from frisket.engine.jobs.projection import (
    job_is_stalled,
    job_result_summary,
    status_time,
)
from frisket.engine.jobs.schedule import _parse_time, _schedule_interval
from frisket.redaction import redact_text
from frisket.engine.store.feeds import FeedStore

FEED_HEALTH_SCHEMA_VERSION = "frisket.feed_health.v1"
FEED_CHECKS_PAGE_SCHEMA_VERSION = "frisket.feed_checks_page.v1"
_MAX_JOB_SUMMARIES = 20
_MAX_ERROR_CHARS = 240

_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|authorization|password|raw|body|cookie|session|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|signature)"
)


class FeedHealthNotFound(LookupError):
    """Raised when a project feed id does not exist."""


def build_feed_health(
    project: Any,
    feed_id: int,
    *,
    runs_offset: int = 0,
    runs_limit: int = 20,
    jobs: Iterable[Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    feed_store = FeedStore(project)
    feed_row = feed_store.get_feed(feed_id)
    if feed_row is None:
        raise FeedHealthNotFound(feed_id)

    now = (now or datetime.now(UTC)).astimezone(UTC)
    feed = _public_feed(feed_row)
    run_rows = list(
        feed_store.feed_checks_page(feed_id, offset=runs_offset, limit=runs_limit)
    )
    total_runs = feed_store.feed_checks_total(feed_id)
    warnings = _feed_check_warnings(run_rows)

    latest_run = _fetch_latest_run(project, feed_id)
    last_success = _fetch_latest_status_run(project, feed_id, "ok")
    last_failure = _fetch_latest_status_run(project, feed_id, "error")
    consecutive_failures = _consecutive_failures(project, feed_id)
    last_success_at = _run_timestamp(last_success)
    last_failure_at = _run_timestamp(last_failure)

    status = _health_status(
        feed_row,
        total_runs=total_runs,
        latest_run=latest_run,
        consecutive_failures=consecutive_failures,
        last_success_at=last_success_at,
        now=now,
        warnings=warnings,
    )
    runs = [_public_run(row) for row in run_rows]
    deltas = _recent_deltas(run_rows)
    costs = _costs(run_rows)
    downstream_jobs = _job_summaries(jobs or (), feed_id=feed_id, now=now)

    return {
        "schema_version": FEED_HEALTH_SCHEMA_VERSION,
        "feed": feed,
        "summary": {
            "status": status,
            "last_success_at": last_success_at,
            "last_failure_at": last_failure_at,
            "consecutive_failures": consecutive_failures,
            "new_rows_total": _int_value(_row_get(feed_row, "new_rows_total"), 0),
            "new_rows_recent": deltas["new_rows"],
            "changed_rows_recent": deltas["changed_rows"],
            "skipped_rows_recent": deltas["skipped_rows"],
            "revisions_recent": deltas["revisions"],
            "recent_run_count": len(run_rows),
            "last_cursor_summary": _cursor_summary(feed_row, latest_run),
        },
        "runs_page": _page_meta(
            offset=runs_offset,
            limit=runs_limit,
            total=total_runs,
            item_count=len(runs),
        ),
        "runs": runs,
        "downstream_jobs": downstream_jobs,
        "costs": costs,
        "alerts": [],
        "warnings": warnings,
    }


def _public_feed(row: Any) -> dict[str, Any]:
    url, url_redacted = _redact_url(_row_get(row, "url"))
    redactions = ["config", "cursor"]
    if url_redacted:
        redactions.append("url_query")
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "kind": row["kind"],
        "url": url,
        "enabled": bool(row["enabled"]),
        "schedule": _row_get(row, "schedule"),
        "sheet_id": _row_get(row, "sheet_id"),
        "created_at": _row_get(row, "created_at"),
        "redactions": redactions,
    }


def _public_run(row: Any) -> dict[str, Any]:
    summary = _decode_json_object(_row_get(row, "summary_json"), {})
    return {
        "id": int(row["id"]),
        "feed_id": int(row["feed_id"]),
        "status": row["status"],
        "started_at": _row_get(row, "started_at"),
        "finished_at": _row_get(row, "finished_at"),
        "receipt_id": _row_get(row, "receipt_id"),
        "change_id": _row_get(row, "change_id"),
        "new_rows": _int_value(_row_get(row, "new_rows"), 0),
        "skipped_rows": _int_value(_row_get(row, "skipped_rows"), 0),
        "changed_rows": _int_value(_row_get(row, "changed_rows"), 0),
        "revisions": _int_value(_row_get(row, "revisions"), 0),
        "duration_ms": _row_get(row, "duration_ms"),
        "warning_count": _int_value(_row_get(row, "warning_count"), 0),
        "cost_micro": _int_value(_row_get(row, "cost_micro"), 0),
        "error_summary": _redact_text(_row_get(row, "error")),
        "cursor_before_present": bool(_row_get(row, "cursor_before")),
        "cursor_after_present": bool(_row_get(row, "cursor_after")),
        "summary_present": bool(summary),
    }


def _page_meta(
    *,
    offset: int,
    limit: int,
    total: int,
    item_count: int,
) -> dict[str, Any]:
    next_offset = offset + item_count
    has_more = next_offset < total
    return {
        "schema_version": FEED_CHECKS_PAGE_SCHEMA_VERSION,
        "order": "desc",
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }


def _health_status(
    feed_row: Any,
    *,
    total_runs: int,
    latest_run: Any | None,
    consecutive_failures: int,
    last_success_at: str | None,
    now: datetime,
    warnings: list[dict[str, Any]],
) -> str:
    if not bool(_row_get(feed_row, "enabled")):
        return "disabled"
    if total_runs == 0 or latest_run is None:
        return "never_run"
    if consecutive_failures > 0 or _row_get(feed_row, "last_status") == "error":
        return "failing"
    if _feed_stale(
        feed_row,
        last_success_at=last_success_at,
        now=now,
        warnings=warnings,
    ):
        return "stale"
    return "healthy"


def _feed_stale(
    feed_row: Any,
    *,
    last_success_at: str | None,
    now: datetime,
    warnings: list[dict[str, Any]],
) -> bool:
    schedule = _row_get(feed_row, "schedule")
    if not schedule or last_success_at is None:
        return False
    try:
        interval = _schedule_interval(schedule)
    except (TypeError, ValueError):
        warnings.append(
            {
                "code": "unsupported_feed_schedule",
                "message": "Feed schedule could not be parsed for stale detection.",
            }
        )
        return False
    if interval is None:
        return False
    try:
        last_success = _parse_time(last_success_at)
    except (TypeError, ValueError):
        return False
    return last_success is not None and last_success + (interval * 2) <= now


def _recent_deltas(rows: list[Any]) -> dict[str, int]:
    return {
        "new_rows": sum(_int_value(_row_get(row, "new_rows"), 0) for row in rows),
        "changed_rows": sum(
            _int_value(_row_get(row, "changed_rows"), 0) for row in rows
        ),
        "skipped_rows": sum(
            _int_value(_row_get(row, "skipped_rows"), 0) for row in rows
        ),
        "revisions": sum(_int_value(_row_get(row, "revisions"), 0) for row in rows),
    }


def _costs(rows: list[Any]) -> dict[str, Any]:
    return {
        "recent_actual_micro": sum(
            _int_value(_row_get(row, "cost_micro"), 0) for row in rows
        ),
        "recent_estimated_micro": 0,
        "basis": "feed_checks.cost_micro for loaded checks; downstream costs unknown-safe",
    }


def _feed_check_warnings(rows: list[Any]) -> list[dict[str, Any]]:
    sparse_ids = [
        int(row["id"])
        for row in rows
        if _row_get(row, "receipt_id") is None
        or (
            _row_get(row, "summary_json") in (None, "", "{}")
            and _row_get(row, "duration_ms") is None
        )
    ]
    warnings: list[dict[str, Any]] = []
    if sparse_ids:
        warnings.append(
            {
                "code": "sparse_feed_check_metadata",
                "message": (
                    "Some loaded feed checks lack v1 runtime metadata; row deltas "
                    "and costs may be incomplete."
                ),
                "run_ids": sparse_ids[:20],
            }
        )
    return warnings


def _job_summaries(
    jobs: Iterable[Any],
    *,
    feed_id: int,
    now: datetime,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for job in jobs:
        if _job_feed_id(job) != feed_id:
            continue
        refs = {"feed_id": feed_id}
        feed_check_id = _job_feed_check_id(job)
        if feed_check_id is not None:
            refs["feed_check_id"] = feed_check_id
        sheet_id = getattr(job, "sheet_id", None) or _mapping(job.payload).get(
            "sheet_id"
        )
        if isinstance(sheet_id, int):
            refs["sheet_id"] = sheet_id
        out.append(
            {
                "job_id": job.id,
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "created_at": status_time(job.created_at, zulu=True),
                "started_at": status_time(job.started_at, zulu=True),
                "finished_at": status_time(job.finished_at, zulu=True),
                "refs": refs,
                "result_summary": job_result_summary(
                    job.kind,
                    job.result,
                    sanitize_value=_sanitize_public_value,
                ),
                "error_summary": _redact_text(job.error),
                "stalled": job_is_stalled(job, now=now),
            }
        )
        if len(out) >= _MAX_JOB_SUMMARIES:
            break
    return out


def _job_feed_id(job: Any) -> int | None:
    direct = getattr(job, "feed_id", None)
    if isinstance(direct, int):
        return direct
    payload = _mapping(job.payload)
    payload_feed_id = payload.get("feed_id")
    if isinstance(payload_feed_id, int):
        return payload_feed_id
    trigger_ref = _mapping(payload.get("trigger_ref"))
    trigger_feed_id = trigger_ref.get("feed_id")
    if isinstance(trigger_feed_id, int):
        return trigger_feed_id
    result = _mapping(job.result)
    result_feed_id = result.get("feed_id")
    if isinstance(result_feed_id, int):
        return result_feed_id
    return None


def _job_feed_check_id(job: Any) -> int | None:
    payload = _mapping(job.payload)
    trigger_ref = _mapping(payload.get("trigger_ref"))
    for value in (
        payload.get("feed_check_id"),
        trigger_ref.get("feed_check_id"),
        _mapping(job.result).get("run_id"),
        _mapping(job.result).get("feed_check_id"),
    ):
        if isinstance(value, int):
            return value
    return None


def _cursor_summary(feed_row: Any, latest_run: Any | None) -> str:
    if _row_get(feed_row, "cursor"):
        return "stored"
    if latest_run is not None and _row_get(latest_run, "cursor_after"):
        return "run_cursor_available"
    if latest_run is not None and _row_get(latest_run, "cursor_before"):
        return "run_cursor_before_only"
    return "not_recorded"


def _fetch_latest_run(project: Any, feed_id: int) -> Any | None:
    return FeedStore(project).latest_feed_check(feed_id)


def _fetch_latest_status_run(project: Any, feed_id: int, status: str) -> Any | None:
    return FeedStore(project).latest_feed_check_with_status(feed_id, status)


def _consecutive_failures(project: Any, feed_id: int) -> int:
    count = 0
    for status in FeedStore(project).feed_check_statuses_desc(feed_id):
        if status == "error":
            count += 1
            continue
        if status == "ok":
            break
    return count


def _run_timestamp(row: Any | None) -> str | None:
    if row is None:
        return None
    return _row_get(row, "finished_at") or _row_get(row, "started_at")


def _redact_url(value: Any) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    raw = str(value)
    if not raw:
        return None, False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[redacted]", True
    redacted = bool(
        parsed.query or parsed.fragment or parsed.username or parsed.password
    )
    if not parsed.scheme or not parsed.netloc:
        return "[redacted]", True
    host = parsed.hostname or ""
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", "")), redacted


def _redact_text(value: Any, *, max_chars: int = _MAX_ERROR_CHARS) -> str | None:
    if value is None:
        return None
    return redact_text(value, max_chars=max_chars, one_line=False).replace(
        "[REDACTED]", "[redacted]"
    )


def _sanitize_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_public_value(nested)
            for key, nested in value.items()
            if _public_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value[:10]]
    return str(type(value).__name__)


def _public_key(key: str) -> bool:
    return _SECRET_KEY_RE.search(key) is None


def _decode_json_object(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return dict(decoded) if isinstance(decoded, Mapping) else default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if key not in row.keys():
            return default
    except AttributeError:
        return getattr(row, key, default)
    return row[key]


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
