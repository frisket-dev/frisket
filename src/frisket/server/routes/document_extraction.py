"""Visual document extraction editor routes; running uses actions/v1/run."""

import asyncio
from contextlib import suppress
from threading import Event

from fastapi import FastAPI, HTTPException, Request

from frisket.actions.types import TableError
from frisket.contracts.http.document_extraction import (
    ExtractionDocumentResponse,
    ExtractionPreviewRequest,
    ExtractionPreviewResponse,
    ExtractionTemplateSave,
    ExtractionSavedTemplate,
    ExtractionTemplatesResponse,
)
from frisket.server.services.document_extraction import DocumentExtractionService
from frisket.server.thread_worker import await_thread_worker
from frisket.server.route_errors import RouteError, http_error_responses


def register_document_extraction_routes(
    app: FastAPI, *, service: DocumentExtractionService
):
    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RouteError:
            raise
        except (TableError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    async def read(request, function, *args, **kwargs):
        cancelled = Event()

        async def disconnect():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    cancelled.set()
                    return

        watcher = asyncio.create_task(disconnect())
        try:
            return await await_thread_worker(
                call,
                function,
                *args,
                cancelled=cancelled.is_set,
                on_cancel=cancelled.set,
                **kwargs,
            )
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    @app.get(
        "/api/projects/{pid}/document-extraction/documents/{row_id}",
        response_model=ExtractionDocumentResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    async def extraction_document_get(
        request: Request, pid: str, row_id: int, sheet_id: int, column_id: int
    ):
        return await read(
            request,
            service.document,
            pid,
            sheet_id=sheet_id,
            column_id=column_id,
            row_id=row_id,
        )

    @app.post(
        "/api/projects/{pid}/document-extraction/preview",
        response_model=ExtractionPreviewResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    async def extraction_preview(
        request: Request, pid: str, body: ExtractionPreviewRequest
    ):
        return await read(request, service.preview, pid, body)

    @app.get(
        "/api/projects/{pid}/document-extraction/templates",
        response_model=ExtractionTemplatesResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def extraction_templates_list(pid: str, sheet_id: int):
        return call(service.templates, pid, sheet_id)

    @app.post(
        "/api/projects/{pid}/document-extraction/templates",
        response_model=ExtractionSavedTemplate,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    async def extraction_template_save(
        request: Request, pid: str, body: ExtractionTemplateSave
    ):
        return await read(request, service.save, pid, body)
