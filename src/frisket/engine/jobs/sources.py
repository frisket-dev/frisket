"""Queue job handlers and scheduling helpers for live sources.

``source.poll`` is the worker-owned version of a source check/scheduled poll:
it reopens the project bundle, checks the source is still enabled, and runs the
public typed ``source.poll`` action. Job ids stay internal, and
sources/source_runs remain the public monitoring surface.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from frisket.ops.enclosures import ENCLOSURE_DOWNLOAD_KIND, mark_enclosure_queued
from frisket.engine.executor import run_action_spec
from frisket.engine.jobs.queue import JobQueue
from frisket.server.sources.rss import ensure_rss_poller
from frisket.server.sources.runtime import get_source_poller
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.sources import SourceStore
from frisket.operability.structured_logging import correlation_log_payload
from frisket.features.watchlists.triggers import trigger_completed_source_poll_watches

from .embeddings import enqueue_source_append_refreshes
from .notifications_delivery import enqueue_notification_emit_result
from .schedule import _parse_time, _schedule_interval
from frisket.project_identity import ProjectStorageKey

from .project_opener import (
    ProjectOpener,
    open_payload_project,
    require_opener_storage_org_id,
)
from .ports import JobHandlerContext
from .worker import HandlerRegistration, HandlerRegistry

SOURCE_POLL_KIND = "source.poll"


def _source_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    try:
        d["config"] = json.loads(d.get("config") or "{}")
    except (TypeError, ValueError):
        d["config"] = {}
    d["enabled"] = bool(d.get("enabled"))
    return d


def register_source_poll_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue | None = None,
    fetch: Callable[[str], str] | None = None,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    root = Path(workspace_root)

    def handle(payload: dict, _context: JobHandlerContext) -> dict:
        project_id = str(payload["project_id"])
        source_id = int(payload["source_id"])
        if project_opener is None:
            project_root = Path(payload.get("workspace_root") or root)
            project = Project(project_root / f"{project_id}.frisket")
        else:
            project_id, project = open_payload_project(
                payload,
                workspace_root=root,
                workspace_root_storage_org_id=workspace_root_storage_org_id,
                project_opener=project_opener,
            )
            # The claimed location decided WHERE, so the follow-up jobs this
            # poll enqueues travel with that directory — never the payload's.
            project_root = project.path.parent
        try:
            source_store = SourceStore(project)
            row = source_store.get_source(source_id)
            if row is None:
                return {
                    "project_id": project_id,
                    "source_id": source_id,
                    "skipped": True,
                    "reason": "not_found",
                }
            src = _source_dict(row)
            if not src["enabled"]:
                return {
                    "project_id": project_id,
                    "source_id": source_id,
                    "skipped": True,
                    "reason": "disabled",
                }
            if not _source_kind_supported(src["kind"]):
                return {
                    "project_id": project_id,
                    "source_id": source_id,
                    "skipped": True,
                    "reason": f"unsupported_kind:{src['kind']}",
                }
            identity = _source_poll_job_identity(queue, payload)
            result = run_action_spec(
                project,
                _source_poll_action(
                    source_id=source_id,
                    project_id=project_id,
                    identity=identity,
                ),
                project_id=project_id,
                rss_fetcher=fetch,
            )
            if result.status != "completed":
                _emit_source_health_notification(
                    project,
                    project_id=project_id,
                    source=src,
                    status="failed",
                    result=result,
                )
                message = "; ".join(error.message for error in result.errors) or (
                    f"source.poll returned {result.status}"
                )
                raise RuntimeError(message)
            summary = _source_poll_summary_from_result(project, result.receipt_id)
            source_after = _source_dict(source_store.get_source(source_id) or src)
            if str(src.get("last_status") or "never") not in {"never", "ok"}:
                _emit_source_health_notification(
                    project,
                    project_id=project_id,
                    source=source_after,
                    status="recovered",
                    result=result,
                    source_run_id=int(summary["run_id"]),
                )
            jobs = enqueue_enclosure_downloads(
                queue,
                project=project,
                project_id=project_id,
                workspace_root=project_root,
                storage_org_id=_coerce_storage_org_id(payload),
                source=source_after,
                sheet_id=int(summary["sheet_id"]),
                row_ids=list(summary.get("enclosure_row_ids") or []),
            )
            triggered_watch_runs = trigger_completed_source_poll_watches(
                project,
                project_id=project_id,
                receipt_id=result.receipt_id,
            )
            if queue is not None:
                for evaluation in triggered_watch_runs:
                    enqueue_notification_emit_result(
                        queue,
                        workspace_root=project_root,
                        project_id=project_id,
                        storage_org_id=_coerce_storage_org_id(payload),
                        result=evaluation["notification"],
                        project_opener=project_opener,
                    )
            # on_source_append: enqueue (never run inline) an incremental embedding
            # refresh for the appended rows on any index that opted in.
            embedding_jobs = enqueue_source_append_refreshes(
                queue,
                project=project,
                project_id=project_id,
                workspace_root=project_root,
                storage_org_id=_coerce_storage_org_id(payload),
                sheet_id=int(summary["sheet_id"]),
                row_ids=[int(r) for r in summary.get("row_ids") or []],
                trigger_ref={
                    "trigger_kind": "source_run_completed",
                    "source_id": source_id,
                    "source_run_id": summary["run_id"],
                    "receipt_id": result.receipt_id,
                    "sheet_id": summary["sheet_id"],
                },
            )
            return {
                "project_id": project_id,
                "source_id": source_id,
                "action_kind": "source.poll",
                "receipt_id": result.receipt_id,
                "run_id": summary["run_id"],
                "new_rows": summary["new_rows"],
                "revisions": summary["revisions"],
                "sheet_id": summary["sheet_id"],
                "op_id": summary["op_id"],
                "enclosure_jobs": len(jobs),
                "embedding_refresh_jobs": len(embedding_jobs),
                "triggered_watch_runs": len(triggered_watch_runs),
            }
        finally:
            project.close()

    return registry.add(
        SOURCE_POLL_KIND,
        handle,
        origin="frisket.production.source_poll",
    )


def _source_poll_action(
    *,
    source_id: int,
    project_id: str,
    identity: str,
) -> dict[str, Any]:
    basis = json.dumps(
        {"project_id": project_id, "source_id": source_id, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": {"source": source_id},
        "idempotency_key": f"source.poll@sha256:{digest}",
    }


def _coerce_storage_org_id(payload: dict[str, Any]) -> int | None:
    value = payload.get("storage_org_id")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _source_poll_job_identity(queue: JobQueue | None, payload: dict[str, Any]) -> str:
    poll_id = payload.get("poll_id")
    if queue is not None:
        project_id = str(payload.get("project_id"))
        source_id = int(payload.get("source_id"))
        workspace_root = (
            str(payload["workspace_root"]) if payload.get("workspace_root") else None
        )
        if workspace_root is not None:
            job = queue.find_job_by_refs(
                SOURCE_POLL_KIND,
                statuses=("running",),
                project_id=project_id,
                storage_org_id=_coerce_storage_org_id(payload),
                source_id=source_id,
                workspace_root=workspace_root,
            )
            if job is not None:
                if isinstance(poll_id, str) and poll_id:
                    # JobQueue retries increment attempts before handler execution;
                    # source.poll retry identity must still replay attempt 1.
                    return f"{poll_id}:attempt:1"
                return f"job:{job.id}"
    if isinstance(poll_id, str) and poll_id:
        return poll_id
    return (
        "payload:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    )


def _source_poll_summary_from_result(
    project: Project,
    receipt_id: str | None,
) -> dict[str, Any]:
    if not receipt_id:
        raise RuntimeError("source.poll did not return a receipt id")
    body = ReceiptStore(project).body_by_id(receipt_id)
    if body is None:
        raise RuntimeError("source.poll receipt was not persisted")
    receipt = json.loads(body)
    refs = [
        item.get("ref") or {}
        for item in [*receipt.get("outputs", []), *receipt.get("evidence", [])]
        if isinstance(item, dict)
    ]
    source_run = next(
        (ref for ref in refs if ref.get("kind") == "source_poll_run"),
        None,
    )
    rows = next(
        (ref for ref in refs if ref.get("kind") == "source_poll_rows"),
        {},
    )
    enclosures = next(
        (ref for ref in refs if ref.get("kind") == "source_poll_enclosure_pointers"),
        {},
    )
    if source_run is None:
        raise RuntimeError("source.poll receipt lacks source run output")
    return {
        "run_id": source_run.get("source_run_id"),
        "new_rows": int(source_run.get("new_rows") or 0),
        "revisions": int(source_run.get("revisions") or 0),
        "sheet_id": source_run.get("sheet_id"),
        "op_id": source_run.get("op_id"),
        "row_ids": list(rows.get("row_ids") or []),
        "enclosure_row_ids": list(enclosures.get("row_ids") or []),
    }


def _emit_source_health_notification(
    project: Project,
    *,
    project_id: str,
    source: dict[str, Any],
    status: str,
    result: Any | None = None,
    source_run_id: int | None = None,
    stale_reason: str | None = None,
) -> None:
    try:
        from frisket.server.notifications.producers import (
            source_health_notification_candidate,
        )
        from frisket.server.notifications.service import emit_notification_candidate

        if source_run_id is None and getattr(result, "receipt_id", None):
            try:
                source_run_id = int(
                    _source_poll_summary_from_result(project, result.receipt_id)[
                        "run_id"
                    ]
                )
            except Exception:  # noqa: BLE001 - notification detail is best effort
                source_run_id = None
        error = "; ".join(error.message for error in getattr(result, "errors", []))
        candidate = source_health_notification_candidate(
            project_id=project_id,
            source_id=int(source["id"]),
            source_name=str(source.get("name") or f"Source {source['id']}"),
            source_run_id=source_run_id,
            status=status,
            error=error or None,
            stale_reason=stale_reason,
            schedule=source.get("schedule"),
        )
        if candidate is not None:
            emit_notification_candidate(project, candidate)
    except Exception:  # noqa: BLE001 - source poll outcome must remain authoritative
        return


def _download_enclosures_enabled(source: dict[str, Any]) -> bool:
    config = source.get("config") or {}
    return config.get("download_enclosures", "manual") == "queued"


def enqueue_enclosure_downloads(
    queue: JobQueue | None,
    *,
    project: Project,
    project_id: str,
    workspace_root: Path,
    storage_org_id: int | None = None,
    source: dict[str, Any],
    sheet_id: int,
    row_ids: list[int],
) -> list[int]:
    if queue is None or not row_ids or not _download_enclosures_enabled(source):
        return []
    job_ids: list[int] = []
    for row_id in row_ids:
        if _enclosure_download_already_pending(
            queue,
            project_id=project_id,
            sheet_id=sheet_id,
            row_id=row_id,
            workspace_root=workspace_root,
            storage_org_id=storage_org_id,
        ) or _enclosure_row_already_queued_or_downloaded(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
        ):
            continue
        mark_enclosure_queued(project, sheet_id=sheet_id, row_id=row_id)
        job_ids.append(
            queue.enqueue(
                ENCLOSURE_DOWNLOAD_KIND,
                {
                    "project_id": project_id,
                    **(
                        {"storage_org_id": storage_org_id}
                        if storage_org_id is not None
                        else {}
                    ),
                    "sheet_id": sheet_id,
                    "row_id": row_id,
                    "workspace_root": str(workspace_root),
                    **correlation_log_payload(),
                },
                max_attempts=1,
            )
        )
    return job_ids


def _enclosure_download_already_pending(
    queue: JobQueue,
    *,
    project_id: str,
    storage_org_id: int | None,
    sheet_id: int,
    row_id: int,
    workspace_root: Path,
) -> bool:
    return (
        queue.find_job_by_refs(
            ENCLOSURE_DOWNLOAD_KIND,
            statuses=("queued", "running"),
            project_id=project_id,
            storage_org_id=storage_org_id,
            sheet_id=sheet_id,
            row_id=row_id,
            workspace_root=str(workspace_root),
        )
        is not None
    )


def _enclosure_row_already_queued_or_downloaded(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
) -> bool:
    status = _source_poll_cell_value(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_name="media_status",
    )
    if status in {"queued", "running", "downloaded"}:
        return True
    media = _source_poll_cell_value(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_name="media",
    )
    return isinstance(media, dict) and bool(media.get("blob"))


def _source_poll_cell_value(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    column_name: str,
) -> Any:
    column_id = next(
        (
            int(column["id"])
            for column in project.columns(sheet_id)
            if column["name"] == column_name and not column["hidden"]
        ),
        None,
    )
    if column_id is None:
        return None
    return project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)


def _source_due(row: Any, *, now: datetime) -> bool:
    src = _source_dict(row)
    if not src["enabled"]:
        return False
    if not _source_kind_supported(src["kind"]):
        return False
    interval = _schedule_interval(src.get("schedule"))
    if interval is None:
        return False
    last = _parse_time(src.get("last_checked_at"))
    return last is None or last + interval <= now


def _source_kind_supported(kind: Any) -> bool:
    ensure_rss_poller()
    source_kind = str(kind or "").strip()
    return get_source_poller(source_kind) is not None


def _source_stale_for_notification(row: Any, *, now: datetime) -> bool:
    interval = _schedule_interval(row.get("schedule"))
    if interval is None:
        return False
    last = _parse_time(row.get("last_checked_at"))
    return last is not None and last + (interval * 2) <= now


def _poll_already_pending(
    queue: JobQueue,
    *,
    project_id: str,
    storage_org_id: int | None,
    source_id: int,
    workspace_root: Path,
) -> bool:
    return (
        queue.find_job_by_refs(
            SOURCE_POLL_KIND,
            statuses=("queued", "running"),
            project_id=project_id,
            storage_org_id=storage_org_id,
            source_id=source_id,
            workspace_root=str(workspace_root),
        )
        is not None
    )


def enqueue_due_source_polls(
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    storage_org_id: int | None = None,
    now: datetime | None = None,
    max_attempts: int = 3,
    project_opener: ProjectOpener | None = None,
) -> list[dict[str, int | str]]:
    """Enqueue one ``source.poll`` job for each due enabled scheduled source.

    The helper is deliberately internal and conservative: it scans workspace
    bundles, skips manual/disabled/fresh sources, and avoids duplicate queued or
    running polls for the same source.
    """
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
            for row in SourceStore(project).sources():
                source_id = int(row["id"])
                try:
                    due = _source_due(row, now=now)
                except ValueError:
                    continue
                if not due:
                    continue
                if _poll_already_pending(
                    queue,
                    project_id=project_id,
                    storage_org_id=storage_org_id,
                    source_id=source_id,
                    workspace_root=root,
                ):
                    continue
                src = _source_dict(row)
                if _source_stale_for_notification(src, now=now):
                    _emit_source_health_notification(
                        project,
                        project_id=project_id,
                        source=src,
                        status="stale",
                        stale_reason="missed_two_intervals",
                    )
                job_id = queue.enqueue(
                    SOURCE_POLL_KIND,
                    {
                        "project_id": project_id,
                        **(
                            {"storage_org_id": storage_org_id}
                            if storage_org_id is not None
                            else {}
                        ),
                        "source_id": source_id,
                        "poll_id": (
                            f"{project_id}:{source_id}:"
                            f"{now.isoformat(timespec='microseconds')}"
                        ),
                        "workspace_root": str(root),
                    },
                    max_attempts=max_attempts,
                )
                enqueued.append(
                    {
                        "job_id": job_id,
                        "project_id": project_id,
                        "source_id": source_id,
                    }
                )
        finally:
            project.close()
    return enqueued
