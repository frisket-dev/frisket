"""HTTP routes for one-source table import drafts."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, Request, Response, UploadFile

from frisket.contracts.http.table_imports import (
    TableImportColumnsRequest,
    TableImportCommitRequest,
    TableImportCommitResponse,
    TableImportDestinationRequest,
    TableImportDraft,
    TableImportRerunRequest,
    TableImportSettingsRequest,
    TableImportUnlockRequest,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.import_uploads import admit_upload
from frisket.server.services.table_imports import FrozenTableImport, TableImportService
from frisket.server.thread_worker import await_thread_worker


CommitHandler = Callable[[FrozenTableImport, Request], Mapping[str, Any] | Any]


def register_table_import_routes(
    app: FastAPI,
    *,
    service: TableImportService,
    commit_handler: CommitHandler | None = None,
) -> None:
    base = "/api/projects/{pid}/import/table-imports"
    responses = http_error_responses(400, 401, 403, 404, 409, 413, 422, 500, 503)

    async def create(
        pid: str,
        file: UploadFile,
        *,
        input_kind: str,
        large_paste_confirmed: bool = False,
    ) -> TableImportDraft:
        admitted = await admit_upload(file)
        return await await_thread_worker(
            service.create,
            pid,
            admitted,
            input_kind=input_kind,
            large_paste_confirmed=large_paste_confirmed,
        )

    @app.post(
        f"{base}/file",
        operation_id="createTableImportFromFile",
        status_code=201,
        response_model=TableImportDraft,
        responses=responses,
    )
    async def create_table_import_from_file(
        pid: str, file: Annotated[UploadFile, File()]
    ) -> TableImportDraft:
        return await create(pid, file, input_kind="file")

    @app.post(
        f"{base}/paste",
        operation_id="createTableImportFromPaste",
        status_code=201,
        response_model=TableImportDraft,
        responses=responses,
    )
    async def create_table_import_from_paste(
        pid: str,
        file: Annotated[UploadFile, File()],
        large_paste_confirmed: Annotated[bool, Form()] = False,
    ) -> TableImportDraft:
        return await create(
            pid,
            file,
            input_kind="paste",
            large_paste_confirmed=large_paste_confirmed,
        )

    @app.get(
        f"{base}/{{draft_id}}",
        operation_id="getTableImport",
        response_model=TableImportDraft,
        responses=responses,
    )
    async def get_table_import(pid: str, draft_id: str) -> TableImportDraft:
        return await await_thread_worker(service.status, pid, draft_id)

    @app.post(
        f"{base}/{{draft_id}}/unlock",
        operation_id="unlockTableImport",
        response_model=TableImportDraft,
        responses=responses,
    )
    async def unlock_table_import(
        pid: str, draft_id: str, body: TableImportUnlockRequest
    ) -> TableImportDraft:
        return await await_thread_worker(
            service.unlock, pid, draft_id, password=body.password
        )

    @app.patch(
        f"{base}/{{draft_id}}/settings",
        operation_id="updateTableImportSettings",
        response_model=TableImportDraft,
        responses=responses,
    )
    async def update_table_import_settings(
        pid: str, draft_id: str, body: TableImportSettingsRequest
    ) -> TableImportDraft:
        return await await_thread_worker(
            service.update_settings,
            pid,
            draft_id,
            expected_revision=body.expected_revision,
            settings=body.settings.model_dump(mode="json"),
        )

    @app.patch(
        f"{base}/{{draft_id}}/columns",
        operation_id="updateTableImportColumns",
        response_model=TableImportDraft,
        responses=responses,
    )
    async def update_table_import_columns(
        pid: str, draft_id: str, body: TableImportColumnsRequest
    ) -> TableImportDraft:
        return await await_thread_worker(
            service.update_columns,
            pid,
            draft_id,
            expected_revision=body.expected_revision,
            columns=[item.model_dump(mode="json") for item in body.columns],
        )

    @app.patch(
        f"{base}/{{draft_id}}/destination",
        operation_id="updateTableImportDestination",
        response_model=TableImportDraft,
        responses=responses,
    )
    async def update_table_import_destination(
        pid: str, draft_id: str, body: TableImportDestinationRequest
    ) -> TableImportDraft:
        return await await_thread_worker(
            service.update_destination,
            pid,
            draft_id,
            expected_revision=body.expected_revision,
            destination=body.destination.model_dump(mode="json"),
            mappings=[item.model_dump(mode="json") for item in body.mappings],
            dedup=body.dedup.model_dump(mode="json"),
        )

    @app.post(
        f"{base}/{{draft_id}}/commit",
        operation_id="commitTableImport",
        response_model=TableImportCommitResponse,
        responses=responses,
    )
    async def commit_table_import(
        request: Request,
        pid: str,
        draft_id: str,
        body: TableImportCommitRequest,
    ) -> TableImportCommitResponse:
        frozen = await await_thread_worker(
            service.freeze_for_commit,
            pid,
            draft_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
        if isinstance(frozen, TableImportCommitResponse):
            return frozen
        if commit_handler is None:
            raise RuntimeError("table import commit handler is unavailable")
        result = commit_handler(frozen, request)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "__dict__") and not isinstance(result, Mapping):
            result = vars(result)
        if not isinstance(result, Mapping):
            raise RuntimeError("table import commit handler returned no result")
        return await await_thread_worker(
            service.record_commit,
            pid,
            draft_id,
            idempotency_key=body.idempotency_key,
            result=result,
        )

    @app.delete(
        f"{base}/{{draft_id}}",
        operation_id="discardTableImport",
        status_code=204,
        responses=responses,
    )
    async def discard_table_import(pid: str, draft_id: str) -> Response:
        await await_thread_worker(service.discard, pid, draft_id)
        return Response(status_code=204)

    @app.post(
        f"{base}/{{draft_id}}/rerun",
        operation_id="rerunTableImportWorksheet",
        status_code=201,
        response_model=TableImportDraft,
        responses=responses,
    )
    async def rerun_table_import_worksheet(
        pid: str, draft_id: str, body: TableImportRerunRequest
    ) -> TableImportDraft:
        return await await_thread_worker(
            service.rerun, pid, draft_id, worksheet=body.worksheet
        )


__all__ = ["register_table_import_routes"]
