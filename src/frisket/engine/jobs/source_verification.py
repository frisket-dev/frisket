"""On-demand source verification on the existing project job queue."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.consumed_sources import (
    capture_revisions,
    verify_bundle,
)

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import SOURCE_VERIFY_KIND, JobQueue, claimed_project_location
from .worker import HandlerRegistration, HandlerRegistry


def enqueue_source_verification(
    project: Project, queue: JobQueue, payload: dict, bundle_id: int
) -> int | None:
    bundle = project.db.execute(
        "SELECT capture_json,input_fingerprint FROM consumed_source_bundles WHERE id=?",
        (bundle_id,),
    ).fetchone()
    if bundle is None:
        return None
    revisions = capture_revisions(project, json.loads(bundle["capture_json"]))
    revision_key = hashlib.sha256(
        json.dumps(revisions, sort_keys=True).encode()
    ).hexdigest()
    dedupe_key = f"source-verify:{bundle['input_fingerprint']}:{revision_key}"
    terminal = queue.find_job_by_refs(
        SOURCE_VERIFY_KIND,
        statuses=("done", "failed", "cancelled"),
        project_id=payload["project_id"],
        storage_org_id=payload.get("storage_org_id"),
        workspace_root=payload.get("workspace_root"),
        dedupe_key=dedupe_key,
    )
    if terminal is not None and (
        terminal.status in {"failed", "cancelled"}
        or (
            isinstance(terminal.result, dict)
            and terminal.result.get("state") == "unknown"
        )
    ):
        # Polling an open view does not restart a cancelled/failed check. A
        # changed relevant revision gives a new explicit verification request.
        return None
    return queue.enqueue(
        SOURCE_VERIFY_KIND,
        {
            **payload,
            "bundle_id": int(bundle_id),
            "dedupe_key": dedupe_key,
        },
    )


def register_source_verification_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    workspace_root_storage_organization_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, context: JobHandlerContext) -> dict:
        project_id, _root, path = claimed_project_location(
            payload,
            workspace_root=workspace_root,
            workspace_root_storage_organization_id=workspace_root_storage_organization_id,
            require_storage_identity=project_opener is not None,
        )
        bundle_id = payload.get("bundle_id")
        if (
            isinstance(bundle_id, bool)
            or not isinstance(bundle_id, int)
            or bundle_id < 1
        ):
            raise ValueError("source verification requires a consumed bundle identity")
        project = (
            Project(path)
            if project_opener is None
            else open_claimed_project(payload, path, project_opener)
        )
        try:
            last_check = float("-inf")
            stopped = False

            def cancelled() -> bool:
                nonlocal last_check, stopped
                now = time.monotonic()
                if context.job_id is not None and now - last_check >= 0.25:
                    job = queue.get(context.job_id)
                    stopped = job is None or job.status != "running"
                    last_check = now
                return stopped

            state = verify_bundle(project, bundle_id, cancelled=cancelled)
            return {"project_id": project_id, "bundle_id": bundle_id, "state": state}
        finally:
            project.close()

    return registry.add(
        SOURCE_VERIFY_KIND, handle, origin="frisket.production.source_verification"
    )
