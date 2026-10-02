"""Project blob route registration."""

from __future__ import annotations

from typing import Any
import asyncio
from contextlib import suppress
from threading import Event

from fastapi import FastAPI, Request, Path as PathParam
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from frisket.server import schemas
from frisket.server.services.project_blobs import (
    ProjectBlobService,
)


class _PageImageResponse(FileResponse):
    def __init__(self, blob):
        super().__init__(
            blob.path,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=3600"},
        )
        self._blob = blob

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._blob.close()


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

    @app.get("/api/projects/{pid}/blobs/{digest}/pages/{page}/image")
    async def get_pdf_page_image(
        pid: str, digest: str, request: Request, page: int = PathParam(ge=1)
    ) -> FileResponse:
        cancelled = Event()

        async def watch_disconnect():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    cancelled.set()
                    return

        watcher = asyncio.create_task(watch_disconnect())
        blob = None
        try:
            try:
                blob = await service.pdf_page_image(
                    pid, digest, page, should_cancel=cancelled.is_set
                )
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
            return _PageImageResponse(blob)
        except BaseException:
            if blob is not None:
                blob.close()
            raise

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
