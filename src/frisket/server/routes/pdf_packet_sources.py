"""Authenticated access to original PDFs retained by a packet review session."""

from __future__ import annotations

from contextlib import ExitStack

from fastapi import FastAPI

from frisket.server.route_errors import http_error_responses
from frisket.server.routes.project_blobs import _BlobResponse
from frisket.server.services.pdf_packet_splits import (
    PdfPacketSplitRouteError,
    PdfPacketSplitService,
)
from frisket.server.services.project_blobs import BlobDownload
from frisket.server.thread_worker import await_thread_worker


def _lease_source(
    service: PdfPacketSplitService, project_id: str, split_id: str, source_key: str
) -> BlobDownload:
    session = service._session(project_id, split_id)
    resources = ExitStack()
    try:
        resources.enter_context(service._admit_session(session))
        source = next(
            (item for item in session.sources if item.key == source_key), None
        )
        if source is None:
            raise PdfPacketSplitRouteError(404, "packet source was not found")
        path = resources.enter_context(source.open_source())
        return BlobDownload(
            path=path, media_type="application/pdf", materialization=resources
        )
    except BaseException:
        resources.close()
        raise


def register_pdf_packet_source_routes(
    app: FastAPI, *, service: PdfPacketSplitService
) -> None:
    @app.get(
        "/api/projects/{pid}/import/pdf-packet-splits/{split_id}/source-file",
        responses=http_error_responses(400, 401, 403, 404, 409, 500, 503),
    )
    async def get_pdf_packet_source_file(
        pid: str, split_id: str, source_key: str
    ) -> _BlobResponse:
        source = await await_thread_worker(
            _lease_source, service, pid, split_id, source_key
        )
        return _BlobResponse(source, headers={"Cache-Control": "private, no-store"})


__all__ = ["register_pdf_packet_source_routes"]
