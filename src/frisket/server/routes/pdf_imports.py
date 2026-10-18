"""HTTP routes for durable precommit multi-PDF intake."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from frisket.contracts.http.pdf_imports import (
    PDF_IMPORT_SCHEMA_VERSION,
    PdfImportBatchOcrEstimateRequest,
    PdfImportBatchOcrState,
    PdfImportChoicesRequest,
    PdfImportCommitRequest,
    PdfImportCommitResponse,
    PdfImportCreateRequest,
    PdfImportListResponse,
    PdfImportManifestBatchRequest,
    PdfImportOcrPreviewEstimate,
    PdfImportOcrPreviewRequest,
    PdfImportOcrPreviewStart,
    PdfImportPageResponse,
    PdfImportReselectRequest,
    PdfImportReselectResponse,
    PdfImportSamplesResponse,
    PdfImportStatus,
    PdfTextCategory,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.import_bulk_types import BulkImportLimits
from frisket.server.services.import_uploads import admit_uploads
from frisket.server.services.pdf_imports import FrozenPdfImport, PdfImportService
from frisket.server.services.selector_choices_capabilities import (
    SelectorCapabilitiesFor,
)
from frisket.server.thread_worker import await_thread_worker


CommitHandler = Callable[[FrozenPdfImport, Request], Mapping[str, Any] | Any]


def register_pdf_import_routes(
    app: FastAPI,
    *,
    service: PdfImportService,
    capabilities_for: SelectorCapabilitiesFor,
    limits: BulkImportLimits = BulkImportLimits(),
    commit_handler: CommitHandler | None = None,
) -> None:
    base = "/api/projects/{pid}/import/pdf-imports"
    responses = http_error_responses(400, 401, 402, 403, 404, 409, 413, 422, 500, 503)

    def require_action_run(request: Request, pid: str) -> None:
        if not capabilities_for(request, pid).may_run_actions:
            raise HTTPException(
                status_code=403,
                detail="Project action execution access is required.",
            )

    @app.post(
        base,
        operation_id="createPdfImport",
        status_code=201,
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def create_pdf_import(
        pid: str, body: PdfImportCreateRequest
    ) -> PdfImportStatus:
        return await await_thread_worker(
            service.create,
            pid,
            expected_files=body.expected_files,
            expected_bytes=body.expected_bytes,
            source_label=body.source_label,
        )

    @app.get(
        base,
        operation_id="listPdfImports",
        response_model=PdfImportListResponse,
        responses=responses,
    )
    async def list_pdf_imports(pid: str) -> PdfImportListResponse:
        items = await await_thread_worker(service.list, pid)
        return PdfImportListResponse(items=items)

    @app.get(
        f"{base}/{{draft_id}}",
        operation_id="getPdfImport",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def get_pdf_import(pid: str, draft_id: str) -> PdfImportStatus:
        return await await_thread_worker(service.status, pid, draft_id)

    @app.post(
        f"{base}/{{draft_id}}/manifest",
        operation_id="appendPdfImportManifest",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def append_pdf_import_manifest(
        pid: str, draft_id: str, body: PdfImportManifestBatchRequest
    ) -> PdfImportStatus:
        return await await_thread_worker(
            service.append_manifest,
            pid,
            draft_id,
            batch_id=body.batch_id,
            files=[item.model_dump(mode="json") for item in body.files],
        )

    @app.post(
        f"{base}/{{draft_id}}/manifest/seal",
        operation_id="sealPdfImportManifest",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def seal_pdf_import_manifest(pid: str, draft_id: str) -> PdfImportStatus:
        return await await_thread_worker(service.seal_manifest, pid, draft_id)

    @app.post(
        f"{base}/{{draft_id}}/uploads",
        operation_id="uploadPdfImportFiles",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def upload_pdf_import_files(
        pid: str,
        draft_id: str,
        files: Annotated[list[UploadFile], File()],
        file_ids: Annotated[list[str], Form()],
        logical_paths: Annotated[list[str], Form()],
        batch_id: Annotated[str, Form()],
    ) -> PdfImportStatus:
        admitted = await admit_uploads(
            files,
            max_files=min(limits.max_upload_files or 64, 64),
            max_bytes=limits.max_request_bytes or limits.max_upload_bytes,
        )
        return await await_thread_worker(
            service.upload,
            pid,
            draft_id,
            admitted,
            file_ids=file_ids,
            logical_paths=logical_paths,
            batch_id=batch_id,
        )

    @app.post(
        f"{base}/{{draft_id}}/pause",
        operation_id="pausePdfImport",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def pause_pdf_import(pid: str, draft_id: str) -> PdfImportStatus:
        return await await_thread_worker(service.set_paused, pid, draft_id, True)

    @app.post(
        f"{base}/{{draft_id}}/resume",
        operation_id="resumePdfImport",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def resume_pdf_import(pid: str, draft_id: str) -> PdfImportStatus:
        return await await_thread_worker(service.set_paused, pid, draft_id, False)

    @app.patch(
        f"{base}/{{draft_id}}/choices",
        operation_id="updatePdfImportChoices",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def update_pdf_import_choices(
        request: Request, pid: str, draft_id: str, body: PdfImportChoicesRequest
    ) -> PdfImportStatus:
        if (
            body.semantic_setup_confirmation is not None
            and not capabilities_for(request, pid).manage_model_downloads
        ):
            raise HTTPException(
                status_code=403,
                detail="Model download management access is required.",
            )
        return await await_thread_worker(
            service.update_choices,
            pid,
            draft_id,
            **body.model_dump(mode="json"),
        )

    @app.post(
        f"{base}/{{draft_id}}/match",
        operation_id="comparePdfImportDestination",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def compare_pdf_import_destination(
        pid: str, draft_id: str
    ) -> PdfImportStatus:
        return await await_thread_worker(service.match_destination, pid, draft_id)

    @app.post(
        f"{base}/{{draft_id}}/semantic-setup/refresh",
        operation_id="refreshPdfImportSemanticSetup",
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def refresh_pdf_import_semantic_setup(
        request: Request, pid: str, draft_id: str
    ) -> PdfImportStatus:
        return await await_thread_worker(
            service.refresh_semantic_setup,
            pid,
            draft_id,
            request_context=request,
        )

    @app.get(
        f"{base}/{{draft_id}}/samples",
        operation_id="listPdfImportSamples",
        response_model=PdfImportSamplesResponse,
        responses=responses,
    )
    async def list_pdf_import_samples(
        pid: str,
        draft_id: str,
        category: PdfTextCategory,
        cursor: str = "",
        limit: Annotated[int, Query(ge=1, le=6)] = 6,
    ) -> PdfImportSamplesResponse:
        return await await_thread_worker(
            service.samples,
            pid,
            draft_id,
            category=category,
            cursor=cursor,
            limit=limit,
        )

    @app.post(
        f"{base}/{{draft_id}}/reselect",
        operation_id="reconcilePdfImportReselection",
        response_model=PdfImportReselectResponse,
        responses=responses,
    )
    async def reconcile_pdf_import_reselection(
        pid: str, draft_id: str, body: PdfImportReselectRequest
    ) -> PdfImportReselectResponse:
        return await await_thread_worker(
            service.reconcile_reselection,
            pid,
            draft_id,
            files=[item.model_dump(mode="json") for item in body.files],
        )

    page_base = f"{base}/{{draft_id}}/files/{{occurrence_id}}/pages/{{page}}"

    @app.get(
        page_base,
        operation_id="getPdfImportPage",
        response_model=PdfImportPageResponse,
        responses=responses,
    )
    async def get_pdf_import_page(
        pid: str, draft_id: str, occurrence_id: str, page: int
    ) -> PdfImportPageResponse:
        return await await_thread_worker(
            service.page, pid, draft_id, occurrence_id, page
        )

    @app.get(
        f"{page_base}/thumbnail",
        operation_id="getPdfImportThumbnail",
        responses=responses,
    )
    async def get_pdf_import_thumbnail(
        pid: str, draft_id: str, occurrence_id: str, page: int
    ) -> FileResponse:
        lease = await await_thread_worker(
            service.thumbnail, pid, draft_id, occurrence_id, page
        )
        return FileResponse(
            lease.path,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=3600"},
            background=BackgroundTask(lease.close),
        )

    @app.post(
        f"{base}/{{draft_id}}/ocr-estimate",
        operation_id="estimatePdfImportBatchOcr",
        response_model=PdfImportBatchOcrState,
        responses=responses,
    )
    async def estimate_pdf_import_batch_ocr(
        request: Request,
        pid: str,
        draft_id: str,
        body: PdfImportBatchOcrEstimateRequest,
    ) -> PdfImportBatchOcrState:
        require_action_run(request, pid)
        return await await_thread_worker(
            service.estimate_batch_ocr,
            pid,
            draft_id,
            expected_revision=body.expected_revision,
            engine=body.engine,
            ocr_policy=body.ocr_policy,
            request_context=request,
        )

    @app.post(
        f"{page_base}/ocr-preview/estimate",
        operation_id="estimatePdfImportOcrPreview",
        response_model=PdfImportOcrPreviewEstimate,
        responses=responses,
    )
    async def estimate_pdf_import_ocr_preview(
        request: Request,
        pid: str,
        draft_id: str,
        occurrence_id: str,
        page: int,
        body: PdfImportOcrPreviewRequest,
    ) -> PdfImportOcrPreviewEstimate:
        require_action_run(request, pid)
        return await await_thread_worker(
            service.estimate_ocr_preview,
            pid,
            draft_id,
            occurrence_id,
            page,
            engine=body.engine,
            request_context=request,
        )

    @app.post(
        f"{page_base}/ocr-preview",
        operation_id="startPdfImportOcrPreview",
        status_code=202,
        response_model=PdfImportOcrPreviewStart,
        responses=responses,
    )
    async def start_pdf_import_ocr_preview(
        request: Request,
        pid: str,
        draft_id: str,
        occurrence_id: str,
        page: int,
        body: PdfImportOcrPreviewRequest,
    ) -> PdfImportOcrPreviewStart:
        require_action_run(request, pid)
        return await await_thread_worker(
            service.start_ocr_preview,
            pid,
            draft_id,
            occurrence_id,
            page,
            engine=body.engine,
            confirmation=body.confirmation,
            request_context=request,
        )

    @app.post(
        f"{base}/{{draft_id}}/commit",
        operation_id="commitPdfImport",
        status_code=202,
        response_model=PdfImportCommitResponse,
        responses=responses,
    )
    async def commit_pdf_import(
        request: Request, pid: str, draft_id: str, body: PdfImportCommitRequest
    ) -> PdfImportCommitResponse:
        require_action_run(request, pid)
        frozen = await await_thread_worker(
            service.freeze_for_commit,
            pid,
            draft_id,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
            request_context=request,
        )
        job = None
        if commit_handler is not None:
            handled = commit_handler(frozen, request)
            job = await handled if inspect.isawaitable(handled) else handled
        return PdfImportCommitResponse(
            schema_version=PDF_IMPORT_SCHEMA_VERSION,
            draft_id=draft_id,
            commit_id=frozen.commit_id,
            status="accepted",
            manifest_hash=frozen.manifest_hash,
            selected_total=frozen.committed_total,
            available_through=frozen.available_through,
            uploads_complete=frozen.uploads_complete,
            job=dict(job) if job is not None else None,
        )

    @app.delete(
        f"{base}/{{draft_id}}",
        operation_id="discardPdfImport",
        status_code=202,
        response_model=PdfImportStatus,
        responses=responses,
    )
    async def discard_pdf_import(pid: str, draft_id: str) -> PdfImportStatus:
        return await await_thread_worker(service.discard, pid, draft_id)


__all__ = ["register_pdf_import_routes"]
