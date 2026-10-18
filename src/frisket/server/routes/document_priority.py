"""Route for prioritizing explicitly opened documents."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request

from frisket.contracts.http.document_priority import (
    DocumentPriorityRequest,
    DocumentPriorityResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.document_priority import DocumentPriorityService
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities
from frisket.server.thread_worker import await_thread_worker


def register_document_priority_route(
    app: FastAPI,
    *,
    service: DocumentPriorityService,
    capabilities_for: Callable[[Request, str], SelectorCapabilities],
) -> None:
    @app.post(
        "/api/projects/{pid}/search-preparation/document-priority",
        operation_id="promoteDocumentPriority",
        response_model=DocumentPriorityResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    async def promote_document_priority(
        request: Request, pid: str, body: DocumentPriorityRequest
    ) -> DocumentPriorityResponse:
        return await await_thread_worker(
            service.promote,
            pid,
            sheet_id=body.sheet_id,
            row_id=body.row_id,
            column_id=body.column_id,
            may_run_actions=capabilities_for(request, pid).may_run_actions,
        )


__all__ = ["register_document_priority_route"]
