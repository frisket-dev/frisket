"""Optional import probing on the existing durable project queue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.engine.store.media_blobs import MediaBlobStore, update_blob_metadata
from frisket.engine.store import Project

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_payload_project
from .queue import claimed_project_location
from .worker import HandlerRegistration, HandlerRegistry

BLOB_METADATA_KIND = "blob.metadata.backfill"
PROBE_BATCH_SIZE = 64


def register_blob_metadata_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, _context: JobHandlerContext) -> dict[str, Any]:
        if project_opener is None:
            project_id, _, path = claimed_project_location(
                payload,
                workspace_root=workspace_root,
                workspace_root_storage_org_id=workspace_root_storage_org_id,
                require_storage_identity=False,
            )
            project = Project(path)
        else:
            project_id, project = open_payload_project(
                payload,
                workspace_root=workspace_root,
                workspace_root_storage_org_id=workspace_root_storage_org_id,
                project_opener=project_opener,
            )
        updated = 0
        try:
            store = MediaBlobStore(project)
            cursor = ""
            while digests := store.hashes_needing_metadata(
                limit=PROBE_BATCH_SIZE, after_hash=cursor
            ):
                for digest in digests:
                    # A row may have been removed since this bounded scan.
                    if store.blob_exists(digest):
                        update_blob_metadata(project, digest)
                        updated += 1
                cursor = digests[-1]
            return {"project_id": project_id, "updated": updated}
        finally:
            project.close()

    return registry.add(
        BLOB_METADATA_KIND,
        handle,
        origin="frisket.production.blob_metadata",
    )
