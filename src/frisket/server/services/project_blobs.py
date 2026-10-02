"""Project blob services for local server routes."""

from __future__ import annotations

import mimetypes
import asyncio
import tempfile
from contextlib import AbstractContextManager, ExitStack
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.engine.store.media_blobs import backfill_blob_metadata
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError
from frisket.engine.store.blob_backend import BlobNotFoundError, validate_blob_digest
from frisket.engine.pdf_render import (
    PdfPageOutOfRange,
    PdfRenderError,
    PdfRenderCancelled,
    render_pdf_pages,
)
from frisket.server.thread_worker import await_thread_worker


@dataclass
class BlobDownload:
    path: Path
    media_type: str
    materialization: AbstractContextManager[Path]

    def close(self) -> None:
        self.materialization.__exit__(None, None, None)


class ProjectBlobRouteError(RouteError):
    pass


class ProjectBlobService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def blob_download(self, project_id: str, digest: str) -> BlobDownload:
        try:
            validate_blob_digest(digest)
        except ValueError as exc:
            raise ProjectBlobRouteError(404, "no such blob") from exc
        project = self._workspace.get(project_id)
        row = project.db.execute(
            "SELECT mime, filename FROM blobs WHERE hash=?",
            (digest,),
        ).fetchone()
        if row is None:
            raise ProjectBlobRouteError(404, "no such blob")
        media_type = (
            (row["mime"] if row else None)
            or mimetypes.guess_type(row["filename"] if row else "")[0]
            or "application/octet-stream"
        )
        materialization = project.materialize_blob(digest)
        try:
            path = Path(materialization.__enter__())
        except BlobNotFoundError as exc:
            raise ProjectBlobRouteError(404, "no such blob") from exc
        return BlobDownload(
            path=path,
            media_type=media_type,
            materialization=materialization,
        )

    def backfill_metadata(
        self,
        project_id: str,
        *,
        force: bool,
        limit: int | None,
    ) -> dict[str, Any]:
        """Recover probe facts only; acquisition title/duration are not recoverable."""

        return backfill_blob_metadata(
            self._workspace.get(project_id),
            force=force,
            limit=limit,
        )

    async def pdf_page_image(
        self,
        project_id: str,
        digest: str,
        page: int,
        *,
        should_cancel: Callable[[], bool],
    ) -> BlobDownload:
        """Render one disposable page with OCR's MediaBox/rotation geometry."""
        resources = ExitStack()
        try:

            def acquire_source():
                from frisket.ops.ocr_engines import OcrEngines

                source = self.blob_download(project_id, digest)
                resources.callback(source.close)
                if not OcrEngines._is_pdf(source.path, None):
                    raise ProjectBlobRouteError(422, "A valid PDF page is required")
                return source

            source = await await_thread_worker(acquire_source)
            if page < 1:
                raise ProjectBlobRouteError(422, "A valid PDF page is required")
            scratch = Path(
                resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="frisket-page-")
                )
            )
            try:
                rendered = await render_pdf_pages(
                    source.path,
                    scratch,
                    dpi=144,
                    pages=[page],
                    max_edge=2000,
                    should_cancel=should_cancel,
                )
            except PdfRenderCancelled:
                raise asyncio.CancelledError from None
            except PdfPageOutOfRange as exc:
                raise ProjectBlobRouteError(422, "PDF page is out of range") from exc
            except PdfRenderError as exc:
                raise ProjectBlobRouteError(
                    503, "PDF page could not be rendered"
                ) from exc
            return BlobDownload(rendered.pages[0][1], "image/png", resources)
        except BaseException:
            resources.close()
            raise
