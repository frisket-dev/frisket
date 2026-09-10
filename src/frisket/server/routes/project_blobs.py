"""Project blob route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from frisket.server import schemas
from frisket.server.services.project_blobs import (
    ProjectBlobService,
)


def register_project_blob_routes(
    app: FastAPI,
    *,
    service: ProjectBlobService,
) -> None:
    @app.get("/api/projects/{pid}/blobs/{digest}")
    def get_blob(pid: str, digest: str) -> FileResponse:
        blob = service.blob_download(pid, digest)
        return FileResponse(
            blob.path,
            media_type=blob.media_type,
            background=BackgroundTask(blob.close),
        )

    @app.post("/api/projects/{pid}/blobs/metadata/backfill")
    def backfill_metadata(
        pid: str,
        body: schemas.BlobMetadataBody | None = None,
    ) -> dict[str, Any]:
        """Recompute missing blob metadata for a project (operator repair).

        This remains the operator recovery surface for blobs stored before
        metadata
        extraction existed or after extractor upgrades (``force=true``):
        import/backfill must always be able to recover display metadata from
        the blob table alone (store/media_blobs.py backfill_blob_metadata);
        exercised by tests/test_media_metadata_ingest_probe.py and granted
        session_or_pat/editor in the endpoint catalog. Removal
        condition: remove only when a store-format migration stamps metadata
        for all existing blobs at project open/upgrade time, and retire the
        tests/test_media_metadata_ingest_probe.py backfill case plus the
        ``backfill_metadata`` endpoint-catalog entry in the same change.
        """
        opts = body or schemas.BlobMetadataBody()
        return service.backfill_metadata(
            pid,
            force=opts.force,
            limit=opts.limit,
        )
