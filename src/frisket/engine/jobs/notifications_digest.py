"""Queued notification digest composition jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from frisket.engine.jobs.notifications_delivery import enqueue_notification_delivery
from frisket.engine.jobs.queue import JobQueue, claimed_project_location
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.worker import HandlerRegistration, HandlerRegistry
from frisket.server.notifications.digests import (
    NotificationDigestWindow,
    compose_notification_digest,
    due_digest_window_for_route,
)
from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project

from .project_opener import (
    ProjectOpener,
    claimed_opener_key,
    require_opener_storage_org_id,
)

NOTIFICATION_DIGEST_KIND = "notification.digest"


def notification_digest_dedupe_key(route_id: int, window_key: str) -> str:
    return f"notification.digest:route:{int(route_id)}:window:{window_key}"


def enqueue_due_notification_digests(
    queue: JobQueue,
    *,
    workspace_root: str | Path,
    project_id: str | None = None,
    storage_org_id: int | None = None,
    now: datetime | None = None,
    project_opener: ProjectOpener | None = None,
) -> int:
    workspace = Path(workspace_root)
    project_paths = (
        [workspace / f"{project_id}.frisket"]
        if project_id is not None
        else sorted(workspace.glob("*.frisket"))
    )
    enqueued = 0
    for project_path in project_paths:
        if not (project_path / "project.db").exists():
            continue
        project = (
            Project(project_path)
            if project_opener is None
            else project_opener(
                ProjectStorageKey(
                    storage_org_id=require_opener_storage_org_id(storage_org_id),
                    project_slug=project_path.stem,
                ),
                project_path,
            )
        )
        try:
            for route in project.notification_routes(enabled_only=True):
                if str(route["delivery_mode"]) != "digest":
                    continue
                window = due_digest_window_for_route(route, now=now)
                if window is None:
                    continue
                if project.notification_digest_run_by_route_window(
                    route_id=int(route["id"]),
                    window_key=window.window_key,
                ):
                    continue
                dedupe_key = notification_digest_dedupe_key(
                    int(route["id"]),
                    window.window_key,
                )
                existing = queue.find_job_by_refs(
                    NOTIFICATION_DIGEST_KIND,
                    statuses=("queued", "running"),
                    project_id=project_path.stem,
                    storage_org_id=storage_org_id,
                    dedupe_key=dedupe_key,
                )
                if existing is not None:
                    continue
                queue.enqueue(
                    NOTIFICATION_DIGEST_KIND,
                    {
                        "workspace_root": str(workspace),
                        "project_id": project_path.stem,
                        **(
                            {"storage_org_id": storage_org_id}
                            if storage_org_id is not None
                            else {}
                        ),
                        "route_id": int(route["id"]),
                        "dedupe_key": dedupe_key,
                        **_window_payload(window),
                    },
                )
                enqueued += 1
        finally:
            project.close()
    return enqueued


def register_notification_digest_handlers(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue | None = None,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    workspace = Path(workspace_root)

    def _handle(payload: dict[str, Any], _context: JobHandlerContext) -> dict[str, Any]:
        if project_opener is None:
            # Local/team tier: the trusted operator's own worker, so the
            # payload path is theirs to choose. Unchanged direct open.
            handler_workspace_root: str | Path = (
                payload.get("workspace_root") or workspace
            )
            handler_project_id = str(payload["project_id"])
            handler_storage_org_id = (
                int(payload["storage_org_id"])
                if payload.get("storage_org_id") is not None
                else None
            )
        else:
            # With an injected opener, the hosted composition derives the
            # project location only from the claimed queue
            # identity (reconstructed by the worker from trusted queue columns),
            # never the caller-controlled payload workspace_root/project_id.
            # claimed_opener_key + claimed_project_location(require_storage_
            # identity=True) FAIL CLOSED when no claimed key is present rather
            # than falling back to the payload-derived path.
            claimed = claimed_opener_key(payload)
            handler_project_id, handler_project_root, _bundle = (
                claimed_project_location(
                    payload,
                    workspace_root=workspace,
                    require_storage_identity=True,
                    workspace_root_storage_org_id=workspace_root_storage_org_id,
                )
            )
            handler_workspace_root = handler_project_root
            handler_storage_org_id = claimed.storage_org_id
        return compose_notification_digest_job(
            workspace_root=handler_workspace_root,
            project_id=handler_project_id,
            storage_org_id=handler_storage_org_id,
            route_id=int(payload["route_id"]),
            window=_window_from_payload(payload),
            queue=queue,
            project_opener=project_opener,
        )

    return registry.add(
        NOTIFICATION_DIGEST_KIND,
        _handle,
        origin="frisket.production.notification_digest",
    )


def compose_notification_digest_job(
    *,
    workspace_root: str | Path,
    project_id: str,
    storage_org_id: int | None = None,
    route_id: int,
    window: NotificationDigestWindow,
    queue: JobQueue | None = None,
    project_opener: ProjectOpener | None = None,
) -> dict[str, Any]:
    bundle = Path(workspace_root) / f"{project_id}.frisket"
    project = (
        Project(bundle)
        if project_opener is None
        else project_opener(
            ProjectStorageKey(
                storage_org_id=require_opener_storage_org_id(storage_org_id),
                project_slug=project_id,
            ),
            bundle,
        )
    )
    try:
        run = compose_notification_digest(project, route_id, window=window)
        request_id = run["delivery_request_id"]
    finally:
        project.close()
    delivery_job_id = None
    if queue is not None and request_id is not None:
        delivery_job_id = enqueue_notification_delivery(
            queue,
            workspace_root=workspace_root,
            project_id=project_id,
            storage_org_id=storage_org_id,
            request_id=int(request_id),
            # The nested delivery enqueue reopens the project; hand it the SAME
            # opener rather than letting it fall back to a direct open.
            project_opener=project_opener,
        )
    return {
        "digest_run_id": run["id"],
        "status": run["status"],
        "item_count": run["item_count"],
        "delivery_request_id": request_id,
        "delivery_job_id": delivery_job_id,
    }


def _window_payload(window: NotificationDigestWindow) -> dict[str, Any]:
    return {
        "cadence": window.cadence,
        "window_key": window.window_key,
        "window_start_at": window.start_at.isoformat(),
        "window_end_at": window.end_at.isoformat(),
    }


def _window_from_payload(payload: dict[str, Any]) -> NotificationDigestWindow:
    return NotificationDigestWindow(
        cadence=str(payload["cadence"]),
        window_key=str(payload["window_key"]),
        start_at=_parse_utc(str(payload["window_start_at"])),
        end_at=_parse_utc(str(payload["window_end_at"])),
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
