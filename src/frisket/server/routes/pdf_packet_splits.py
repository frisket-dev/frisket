"""HTTP boundary for the guided PDF-packet split import."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from frisket.contracts.http.pdf_packet_splits import (
    PdfPacketCandidatesRequest,
    PdfPacketCandidatesResponse,
    PdfPacketCommitRequest,
    PdfPacketJobStartResponse,
    PdfPacketOcrEstimateRequest,
    PdfPacketOcrEstimateResponse,
    PdfPacketOcrJobRequest,
    PdfPacketPageResponse,
    PdfPacketSplitStatus,
    PdfPacketTextSourceRequest,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.import_bulk_types import BulkImportLimits
from frisket.server.services.import_uploads import admit_upload
from frisket.server.services.pdf_packet_splits import PdfPacketSplitService
from frisket.server.thread_worker import await_thread_worker


def register_pdf_packet_split_routes(
    app: FastAPI,
    *,
    service: PdfPacketSplitService,
    limits: BulkImportLimits = BulkImportLimits(),
) -> None:
    base = "/api/projects/{pid}/import/pdf-packet-splits"
    responses = http_error_responses(400, 401, 403, 404, 409, 413, 422, 500, 503)

    @app.post(
        base,
        status_code=202,
        response_model=PdfPacketSplitStatus,
        responses=responses,
    )
    async def create_pdf_packet_split(
        pid: str,
        file: Annotated[UploadFile, File()],
        request_id: Annotated[str | None, Form()] = None,
    ) -> PdfPacketSplitStatus:
        upload = await admit_upload(file, max_bytes=limits.max_upload_bytes)
        return await await_thread_worker(
            service.create, pid, upload, request_id=request_id
        )

    @app.get(
        f"{base}/{{split_id}}",
        response_model=PdfPacketSplitStatus,
        responses=responses,
    )
    async def get_pdf_packet_split(pid: str, split_id: str) -> PdfPacketSplitStatus:
        return await await_thread_worker(service.status, pid, split_id)

    @app.get(
        f"{base}/{{split_id}}/pages/{{page}}",
        response_model=PdfPacketPageResponse,
        responses=responses,
    )
    async def get_pdf_packet_split_page(
        pid: str, split_id: str, page: int
    ) -> PdfPacketPageResponse:
        return await await_thread_worker(service.page, pid, split_id, page)

    @app.get(f"{base}/{{split_id}}/pages/{{page}}/thumbnail", responses=responses)
    async def get_pdf_packet_split_thumbnail(
        pid: str, split_id: str, page: int
    ) -> FileResponse:
        thumbnail = await await_thread_worker(service.thumbnail, pid, split_id, page)
        return FileResponse(
            thumbnail.path,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=3600"},
            background=BackgroundTask(thumbnail.close),
        )

    @app.post(
        f"{base}/{{split_id}}/ocr/estimate",
        response_model=PdfPacketOcrEstimateResponse,
        responses=responses,
    )
    async def estimate_pdf_packet_split_ocr(
        request: Request,
        pid: str,
        split_id: str,
        body: PdfPacketOcrEstimateRequest,
    ) -> PdfPacketOcrEstimateResponse:
        return await await_thread_worker(
            service.estimate_ocr,
            pid,
            split_id,
            body,
            request_context=request,
        )

    @app.post(
        f"{base}/{{split_id}}/ocr/jobs",
        status_code=202,
        response_model=PdfPacketJobStartResponse,
        responses=responses,
    )
    async def start_pdf_packet_split_ocr(
        request: Request,
        pid: str,
        split_id: str,
        body: PdfPacketOcrJobRequest,
    ) -> PdfPacketJobStartResponse:
        return await await_thread_worker(
            service.start_ocr,
            pid,
            split_id,
            body,
            request_context=request,
        )

    @app.delete(
        f"{base}/{{split_id}}/jobs/{{job_id}}",
        response_model=PdfPacketSplitStatus,
        responses=responses,
    )
    async def cancel_pdf_packet_split_job(
        pid: str, split_id: str, job_id: str
    ) -> PdfPacketSplitStatus:
        return await await_thread_worker(service.cancel_job, pid, split_id, job_id)

    @app.put(
        f"{base}/{{split_id}}/text-source",
        response_model=PdfPacketSplitStatus,
        responses=responses,
    )
    async def select_pdf_packet_split_text_source(
        pid: str, split_id: str, body: PdfPacketTextSourceRequest
    ) -> PdfPacketSplitStatus:
        return await await_thread_worker(
            service.select_text_source, pid, split_id, body
        )

    @app.post(
        f"{base}/{{split_id}}/candidates",
        response_model=PdfPacketCandidatesResponse,
        responses=responses,
    )
    async def match_pdf_packet_split_candidates(
        pid: str, split_id: str, body: PdfPacketCandidatesRequest
    ) -> PdfPacketCandidatesResponse:
        return await await_thread_worker(service.candidates, pid, split_id, body)

    @app.post(
        f"{base}/{{split_id}}/commit",
        status_code=202,
        response_model=PdfPacketJobStartResponse,
        responses=responses,
    )
    async def commit_pdf_packet_split(
        request: Request,
        pid: str,
        split_id: str,
        body: PdfPacketCommitRequest,
    ) -> PdfPacketJobStartResponse:
        return await await_thread_worker(
            service.commit,
            pid,
            split_id,
            body,
            request_context=request,
        )

    @app.delete(base + "/{split_id}", status_code=204, responses=responses)
    async def delete_pdf_packet_split(pid: str, split_id: str) -> Response:
        await await_thread_worker(service.close, pid, split_id)
        return Response(status_code=204)


__all__ = ["register_pdf_packet_split_routes"]
