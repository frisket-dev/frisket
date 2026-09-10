"""Import route registration."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from frisket.contracts.action import ActionResult
from frisket.contracts.http.action_estimate_validation import ACTION_RUN_ERROR_RESPONSES

from frisket.contracts.http.bulk_imports import (
    BulkImportExecuteBody,
    BulkImportExecuteResponse,
    BulkImportPlanResponse,
)
from frisket.contracts.http.onboarding_imports import (
    IMPORT_CSV_ERROR_RESPONSES,
    IMPORT_FILES_ERROR_RESPONSES,
    IMPORT_PDF_ERROR_RESPONSES,
    IMPORT_URLS_ERROR_RESPONSES,
    IMPORT_XLSX_ERROR_RESPONSES,
    ImportCsvPreviewResponse,
    ImportCsvResponse,
    ImportFilesResponse,
    ImportPasteConfirmBody,
    ImportPasteConfirmResponse,
    ImportPasteDraftBody,
    ImportPasteDraftResponse,
    ImportUpdatePreviewBody,
    ImportUpdatePreviewResponse,
    ImportPdfResponse,
    ImportUrlsBody,
    ImportUrlsResponse,
    ImportXlsxResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.import_bulk import (
    BulkImportLimits,
    BulkUpload,
    ImportBulkRouteError,
    ImportBulkService,
)
from frisket.server.services.import_csv import ImportCsvUploadService
from frisket.server.services.import_followthemoney import FollowTheMoneyUploadService
from frisket.server.services.import_drafts import ImportDraftService
from frisket.server.services.import_files import (
    ImportFilesUploadService,
)
from frisket.server.services.import_pdf import ImportPdfUploadService
from frisket.server.services.import_uploads import (
    AdmittedUpload,
    admit_uploads,
    await_thread_worker,
)
from frisket.server.services.import_urls import ImportUrlsService
from frisket.server.services.import_xlsx import ImportXlsxUploadService
from frisket.server.transport import (
    V1_IMPORT_CSV_UPLOAD_BRIDGE_TASK,
    V1_IMPORT_FILES_UPLOAD_BRIDGE_TASK,
    V1_IMPORT_PDF_UPLOAD_BRIDGE_TASK,
    V1_IMPORT_URLS_BRIDGE_TASK,
    V1_IMPORT_XLSX_UPLOAD_BRIDGE_TASK,
    v1_action_transport,
)


def register_import_draft_routes(
    app: FastAPI,
    *,
    service: ImportDraftService,
) -> None:
    @app.post(
        "/api/projects/{pid}/import/drafts/paste",
        response_model=ImportPasteDraftResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    async def import_paste_draft(pid: str, body: ImportPasteDraftBody) -> dict:
        return service.paste_draft(pid, body.raw)

    @app.post(
        "/api/projects/{pid}/import/drafts/paste/confirm",
        response_model=ImportPasteConfirmResponse,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    async def import_paste_confirm(
        pid: str, body: ImportPasteConfirmBody, request: Request
    ) -> dict[str, Any] | JSONResponse:
        response = await await_thread_worker(
            service.confirm,
            pid,
            body,
            deps=service.executor_deps_for_request(pid, request),
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code,
                content=response.payload,
            )
        return response.payload

    @app.post(
        "/api/projects/{pid}/import/rows/update/preview",
        response_model=ImportUpdatePreviewResponse,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    async def import_update_preview(
        pid: str, body: ImportUpdatePreviewBody, request: Request
    ) -> dict:
        return await await_thread_worker(
            service.preview_update,
            pid,
            body,
            deps=service.executor_deps_for_request(pid, request),
        )


def register_import_bulk_routes(
    app: FastAPI,
    *,
    service: ImportBulkService,
) -> None:
    @app.post(
        "/api/projects/{pid}/import/bulk/plan",
        response_model=BulkImportPlanResponse,
        responses=http_error_responses(400, 401, 403, 404, 413, 422, 500),
    )
    async def import_bulk_plan(
        pid: str,
        files: Annotated[list[UploadFile], File()],
        logical_paths: Annotated[list[str], Form()],
        expand_archive: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        if len(files) != len(logical_paths):
            raise ImportBulkRouteError(
                400, "files and logical_paths must have the same length"
            )
        uploads = [
            BulkUpload(
                filename=file.filename or "upload",
                logical_path=logical_path,
                mime=file.content_type or "application/octet-stream",
                file=file,
            )
            for file, logical_path in zip(files, logical_paths, strict=True)
        ]
        return await service.plan(pid, uploads=uploads, expand_archive=expand_archive)

    @app.post(
        "/api/projects/{pid}/import/bulk/{plan_id}/execute",
        response_model=BulkImportExecuteResponse,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    async def import_bulk_execute(
        pid: str,
        plan_id: str,
        body: BulkImportExecuteBody,
        request: Request,
    ) -> dict[str, Any]:
        # FastAPI supplies the request as a dependency so this composition is
        # resolved exactly once for this execution, then shared by every
        # resulting output.
        return await await_thread_worker(
            service.execute,
            pid,
            plan_id,
            decisions=body.decisions,
            deps=service.executor_deps_for_request(pid, request),
        )


def register_import_csv_routes(
    app: FastAPI,
    *,
    service: ImportCsvUploadService,
    limits: BulkImportLimits = BulkImportLimits(),
) -> None:
    async def upload(
        pid: str,
        request: Request,
        file: UploadFile,
        *,
        sheet_name: str | None,
        encoding: str | None,
        destination_sheet_id: int | None = None,
        append_request_key: str | None = None,
        column_mapping: str | None = None,
        update_key_columns: str | None = None,
        keep_existing_on_blank: bool = False,
        confirmation: str | None = None,
    ) -> dict[str, Any] | Response:
        executor_deps = service.executor_deps_for_request(pid, request)
        response = await await_thread_worker(
            service.upload_csv,
            pid,
            upload=await _admit_one(file, limits=limits),
            sheet_name=sheet_name,
            encoding=encoding,
            destination_sheet_id=destination_sheet_id,
            append_request_key=append_request_key,
            column_mapping=column_mapping,
            update_key_columns=update_key_columns,
            keep_existing_on_blank=keep_existing_on_blank,
            confirmation=confirmation,
            deps=executor_deps,
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code, content=response.payload
            )
        return response.payload

    @app.post(
        "/api/projects/{pid}/import/csv/preview",
        response_model=ImportCsvPreviewResponse,
        responses=http_error_responses(400, 401, 403, 404, 413, 422, 500),
    )
    async def import_csv_preview(
        pid: str,
        file: UploadFile = File(...),
        encoding: str | None = None,
    ) -> dict[str, Any]:
        return await await_thread_worker(
            service.preview_csv,
            pid,
            upload=await _admit_one(file, limits=limits),
            encoding=encoding,
        )

    @app.post(
        "/api/projects/{pid}/import/csv/update/preview",
        response_model=ImportUpdatePreviewResponse,
        responses=http_error_responses(400, 401, 403, 404, 413, 422, 500),
    )
    async def import_csv_update_preview(
        pid: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        destination_sheet_id: Annotated[int, Form(ge=1)],
        column_mapping: Annotated[str, Form(min_length=2)],
        key_columns: Annotated[str, Form(min_length=2)],
        keep_existing_on_blank: Annotated[bool, Form()] = False,
        encoding: str | None = None,
    ) -> dict[str, Any]:
        return await await_thread_worker(
            service.preview_csv_update,
            pid,
            upload=await _admit_one(file, limits=limits),
            destination_sheet_id=destination_sheet_id,
            column_mapping=column_mapping,
            key_columns=key_columns,
            keep_existing_on_blank=keep_existing_on_blank,
            encoding=encoding,
            deps=service.executor_deps_for_request(pid, request),
        )

    @app.post(
        "/api/projects/{pid}/import/csv",
        response_model=ImportCsvResponse,
        responses={
            **IMPORT_CSV_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 413, 422),
        },
        **v1_action_transport(
            V1_IMPORT_CSV_UPLOAD_BRIDGE_TASK,
            "import.csv",
            "multipart_upload",
        ),
    )
    async def import_csv(
        pid: str,
        request: Request,
        file: UploadFile = File(...),
        sheet_name: str | None = None,
        encoding: str | None = None,
    ) -> dict[str, Any] | Response:
        return await upload(
            pid, request, file, sheet_name=sheet_name, encoding=encoding
        )

    @app.post(
        "/api/projects/{pid}/import/csv/append",
        response_model=ImportCsvResponse,
        responses={
            **IMPORT_CSV_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 413, 422),
        },
        **v1_action_transport(
            V1_IMPORT_CSV_UPLOAD_BRIDGE_TASK, "import.append_csv", "multipart_upload"
        ),
    )
    async def append_csv(
        pid: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        destination_sheet_id: Annotated[int, Form(ge=1)],
        append_request_key: Annotated[str, Form(min_length=1)],
        column_mapping: Annotated[str, Form(min_length=2)],
        encoding: str | None = None,
    ) -> dict[str, Any] | Response:
        return await upload(
            pid,
            request,
            file,
            sheet_name=None,
            encoding=encoding,
            destination_sheet_id=destination_sheet_id,
            append_request_key=append_request_key,
            column_mapping=column_mapping,
        )

    @app.post(
        "/api/projects/{pid}/import/csv/update",
        response_model=ImportCsvResponse,
        responses={
            **IMPORT_CSV_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 413, 422),
        },
        **v1_action_transport(
            V1_IMPORT_CSV_UPLOAD_BRIDGE_TASK, "import.update_csv", "multipart_upload"
        ),
    )
    async def update_csv(
        pid: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        destination_sheet_id: Annotated[int, Form(ge=1)],
        update_request_key: Annotated[str, Form(min_length=1)],
        column_mapping: Annotated[str, Form(min_length=2)],
        key_columns: Annotated[str, Form(min_length=2)],
        confirmation: Annotated[str, Form(min_length=1)],
        keep_existing_on_blank: Annotated[bool, Form()] = False,
        encoding: str | None = None,
    ) -> dict[str, Any] | Response:
        return await upload(
            pid,
            request,
            file,
            sheet_name=None,
            encoding=encoding,
            destination_sheet_id=destination_sheet_id,
            append_request_key=update_request_key,
            column_mapping=column_mapping,
            update_key_columns=key_columns,
            keep_existing_on_blank=keep_existing_on_blank,
            confirmation=confirmation,
        )


def register_import_xlsx_routes(
    app: FastAPI,
    *,
    service: ImportXlsxUploadService,
    limits: BulkImportLimits = BulkImportLimits(),
) -> None:
    @app.post(
        "/api/projects/{pid}/import/xlsx/preview",
        response_model=ImportCsvPreviewResponse,
        responses=http_error_responses(400, 401, 403, 404, 413, 422, 500),
    )
    async def import_xlsx_preview(
        pid: str, file: Annotated[UploadFile, File()]
    ) -> dict[str, Any]:
        return await await_thread_worker(
            service.preview_xlsx,
            pid,
            upload=await _admit_one(file, limits=limits),
        )

    @app.post(
        "/api/projects/{pid}/import/xlsx/update/preview",
        response_model=ImportUpdatePreviewResponse,
        responses=http_error_responses(400, 401, 403, 404, 413, 422, 500),
    )
    async def import_xlsx_update_preview(
        pid: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        destination_sheet_id: Annotated[int, Form(ge=1)],
        column_mapping: Annotated[str, Form(min_length=2)],
        key_columns: Annotated[str, Form(min_length=2)],
        keep_existing_on_blank: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        return await await_thread_worker(
            service.preview_xlsx_update,
            pid,
            upload=await _admit_one(file, limits=limits),
            destination_sheet_id=destination_sheet_id,
            column_mapping=column_mapping,
            key_columns=key_columns,
            keep_existing_on_blank=keep_existing_on_blank,
            deps=service.executor_deps_for_request(pid, request),
        )

    @app.post(
        "/api/projects/{pid}/import/xlsx",
        response_model=ImportXlsxResponse,
        responses={
            **IMPORT_XLSX_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422),
        },
        **v1_action_transport(
            V1_IMPORT_XLSX_UPLOAD_BRIDGE_TASK,
            "import.xlsx",
            "multipart_upload",
        ),
    )
    async def import_xlsx(
        pid: str,
        request: Request,
        file: UploadFile = File(...),
        sheet_name: str | None = None,
    ) -> dict[str, Any] | Response:
        response = await await_thread_worker(
            service.upload_xlsx,
            pid,
            upload=await _admit_one(file, limits=limits),
            sheet_name=sheet_name,
            deps=service.executor_deps_for_request(pid, request),
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code,
                content=response.payload,
            )
        return response.payload

    @app.post(
        "/api/projects/{pid}/import/xlsx/append",
        response_model=ImportXlsxResponse,
        responses={
            **IMPORT_XLSX_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 413, 422),
        },
        **v1_action_transport(
            V1_IMPORT_XLSX_UPLOAD_BRIDGE_TASK, "import.append_xlsx", "multipart_upload"
        ),
    )
    async def append_xlsx(
        pid: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        destination_sheet_id: Annotated[int, Form(ge=1)],
        append_request_key: Annotated[str, Form(min_length=1)],
        column_mapping: Annotated[str, Form(min_length=2)],
    ) -> dict[str, Any] | Response:
        response = await await_thread_worker(
            service.upload_xlsx,
            pid,
            upload=await _admit_one(file, limits=limits),
            sheet_name=None,
            deps=service.executor_deps_for_request(pid, request),
            destination_sheet_id=destination_sheet_id,
            append_request_key=append_request_key,
            column_mapping=column_mapping,
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code, content=response.payload
            )
        return response.payload

    @app.post(
        "/api/projects/{pid}/import/xlsx/update",
        response_model=ImportXlsxResponse,
        responses={
            **IMPORT_XLSX_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 413, 422),
        },
        **v1_action_transport(
            V1_IMPORT_XLSX_UPLOAD_BRIDGE_TASK, "import.update_xlsx", "multipart_upload"
        ),
    )
    async def update_xlsx(
        pid: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        destination_sheet_id: Annotated[int, Form(ge=1)],
        update_request_key: Annotated[str, Form(min_length=1)],
        column_mapping: Annotated[str, Form(min_length=2)],
        key_columns: Annotated[str, Form(min_length=2)],
        confirmation: Annotated[str, Form(min_length=1)],
        keep_existing_on_blank: Annotated[bool, Form()] = False,
    ) -> dict[str, Any] | Response:
        response = await await_thread_worker(
            service.upload_xlsx,
            pid,
            upload=await _admit_one(file, limits=limits),
            sheet_name=None,
            deps=service.executor_deps_for_request(pid, request),
            destination_sheet_id=destination_sheet_id,
            append_request_key=update_request_key,
            column_mapping=column_mapping,
            update_key_columns=key_columns,
            keep_existing_on_blank=keep_existing_on_blank,
            confirmation=confirmation,
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code, content=response.payload
            )
        return response.payload


def register_import_pdf_routes(
    app: FastAPI,
    *,
    service: ImportPdfUploadService,
    limits: BulkImportLimits = BulkImportLimits(),
) -> None:
    @app.post(
        "/api/projects/{pid}/import/pdf",
        response_model=ImportPdfResponse,
        responses={
            **IMPORT_PDF_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422),
        },
        **v1_action_transport(
            V1_IMPORT_PDF_UPLOAD_BRIDGE_TASK,
            "import.pdf",
            "multipart_upload",
        ),
    )
    async def import_pdf(
        pid: str,
        request: Request,
        file: UploadFile = File(...),
        sheet_name: str | None = None,
        dpi: int = 150,
    ) -> dict[str, Any] | Response:
        upload = await _admit_one(file, limits=limits)
        response = await service.upload_pdf(
            pid,
            upload=upload,
            sheet_name=sheet_name,
            dpi=dpi,
            deps=service.executor_deps_for_request(pid, request),
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code,
                content=response.payload,
            )
        return response.payload


def register_import_followthemoney_routes(
    app: FastAPI,
    *,
    service: FollowTheMoneyUploadService,
    limits: BulkImportLimits = BulkImportLimits(),
) -> None:
    @app.post(
        "/api/projects/{pid}/import/followthemoney",
        response_model=ActionResult,
        responses={
            **ACTION_RUN_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422),
        },
        **v1_action_transport(
            "typed-ftm-upload", "frisket.ftm.ftm_import", "multipart_upload"
        ),
    )
    async def import_followthemoney(
        pid: str,
        request: Request,
        file: UploadFile = File(...),
        dataset_name: str | None = Form(None),
    ) -> ActionResult | JSONResponse:
        upload = await _admit_one(file, limits=limits)
        response = await await_thread_worker(
            service.upload,
            pid,
            upload=upload,
            dataset_name=dataset_name,
            request_context=request,
        )
        if response.status_code != 200:
            return JSONResponse(
                status_code=response.status_code, content=response.payload
            )
        return ActionResult.model_validate(response.payload)


def register_import_files_routes(
    app: FastAPI,
    *,
    service: ImportFilesUploadService,
    limits: BulkImportLimits = BulkImportLimits(),
) -> None:
    @app.post(
        "/api/projects/{pid}/import/files",
        response_model=ImportFilesResponse,
        responses={
            **IMPORT_FILES_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422),
        },
        **v1_action_transport(
            V1_IMPORT_FILES_UPLOAD_BRIDGE_TASK,
            "import.files",
            "multipart_upload",
        ),
    )
    async def import_files(
        pid: str,
        request: Request,
        files: list[UploadFile] = File(...),
        sheet_name: str | None = None,
    ) -> dict[str, Any] | Response:
        uploads = await admit_uploads(
            files,
            max_files=limits.max_upload_files,
            max_bytes=limits.max_upload_bytes,
        )
        response = await await_thread_worker(
            service.upload_files,
            pid,
            files=uploads,
            sheet_name=sheet_name,
            deps=service.executor_deps_for_request(pid, request),
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code,
                content=response.payload,
            )
        return response.payload


async def _admit_one(file: UploadFile, *, limits: BulkImportLimits) -> AdmittedUpload:
    return (
        await admit_uploads(
            [file],
            max_files=limits.max_upload_files,
            max_bytes=limits.max_upload_bytes,
        )
    )[0]


def register_import_urls_routes(
    app: FastAPI,
    *,
    service: ImportUrlsService,
) -> None:
    @app.post(
        "/api/projects/{pid}/import/urls",
        response_model=ImportUrlsResponse,
        responses={
            **IMPORT_URLS_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422),
        },
        **v1_action_transport(
            V1_IMPORT_URLS_BRIDGE_TASK,
            "import.urls",
            "json_url_import",
        ),
    )
    async def import_urls(
        pid: str,
        body: ImportUrlsBody,
        request: Request,
    ) -> dict[str, Any] | Response:
        response = await service.import_urls(
            pid,
            request=request,
            urls=body.urls,
            sheet_name=body.sheet_name,
            column=body.column,
        )
        if response.force_json_response:
            return JSONResponse(
                status_code=response.status_code,
                content=response.payload,
            )
        return response.payload


__all__ = [
    "register_import_bulk_routes",
    "register_import_csv_routes",
    "register_import_draft_routes",
    "register_import_files_routes",
    "register_import_pdf_routes",
    "register_import_urls_routes",
    "register_import_xlsx_routes",
]
