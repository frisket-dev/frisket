"""Optional import probing on the existing durable project queue."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from frisket.engine.store.media_blobs import MediaBlobStore, update_blob_metadata
from frisket.engine.store import Project
from frisket.project_identity import ProjectStorageKey

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import (
    BLOB_METADATA_KIND,
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    JobQueue,
    claimed_project_location,
)
from .worker import HandlerRegistration, HandlerRegistry

PROBE_BATCH_SIZE = 64
LOG = logging.getLogger(__name__)


def register_blob_metadata_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, context: JobHandlerContext) -> dict[str, Any]:
        def stopped() -> bool:
            if payload.get("job_id") is None:
                return False  # Direct, non-queued invocation.
            job = queue.get(int(payload["job_id"]))
            return job is None or job.status != "running"

        project_id, project_root, path = claimed_project_location(
            payload,
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            require_storage_identity=project_opener is not None,
        )
        if project_opener is None:
            project = Project(path)
        else:
            project = open_claimed_project(payload, path, project_opener)
        updated = 0
        failed = 0
        try:
            store = MediaBlobStore(project)
            cursor = str(payload.get("after_hash") or "")
            digests = store.hashes_needing_metadata(
                limit=PROBE_BATCH_SIZE, after_hash=cursor
            )
            for digest in digests:
                if stopped():
                    break
                # A row may have been removed since this bounded scan.
                try:
                    if store.blob_exists(digest):
                        update_blob_metadata(project, digest)
                        updated += 1
                except Exception:
                    # Leave the probe absent for later repair/retry, but do not
                    # strand the rest of the corpus behind one missing object.
                    failed += 1
                    LOG.warning("blob_metadata_probe_failed", exc_info=True)
            if len(digests) == PROBE_BATCH_SIZE and not stopped():
                # Yield the worker between batches. A failed enqueue retries
                # this job; completed probe namespaces already mark progress.
                # Carry only serializable queue identity, not injected claims.
                continuation = {
                    "project_id": project_id,
                    "workspace_root": str(project_root),
                    "after_hash": digests[-1],
                    "dedupe_key": f"blob-metadata:after:{digests[-1]}",
                }
                claimed = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
                if isinstance(claimed, ProjectStorageKey):
                    continuation["storage_org_id"] = claimed.storage_org_id
                if isinstance(context.trusted_job_org_id, int):
                    continuation["org_id"] = context.trusted_job_org_id
                queue.enqueue(BLOB_METADATA_KIND, continuation)
            return {"project_id": project_id, "updated": updated, "failed": failed}
        finally:
            project.close()

    return registry.add(
        BLOB_METADATA_KIND,
        handle,
        origin="frisket.production.blob_metadata",
    )
