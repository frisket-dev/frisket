"""HTTP routes for durable local search preparation."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Query, Request

from frisket.contracts.http.search_preparation import (
    SearchPreparationBatch,
    SearchPreparationBatchList,
    SearchPreparationCreateRequest,
    SearchPreparationDecisionRequest,
    SearchPreparationReplanRequest,
    SearchPreparationTextSamples,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.search_preparation import SearchPreparationService
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities
from frisket.server.thread_worker import await_thread_worker


def register_search_preparation_routes(
    app: FastAPI,
    *,
    service: SearchPreparationService,
    capabilities_for: Callable[[Request, str], SelectorCapabilities],
) -> None:
    base = "/api/projects/{pid}/search-preparation"
    responses = http_error_responses(400, 401, 403, 404, 409, 422, 500, 503)

    @app.post(
        base,
        operation_id="createSearchPreparation",
        response_model=SearchPreparationBatch,
        responses=responses,
    )
    async def create_search_preparation(
        pid: str, body: SearchPreparationCreateRequest
    ) -> SearchPreparationBatch:
        return await await_thread_worker(
            service.create,
            pid,
            sheet_id=body.sheet_id,
            row_ids=body.row_ids,
            force_ocr=body.force_ocr,
        )

    @app.get(
        base,
        operation_id="listSearchPreparations",
        response_model=SearchPreparationBatchList,
        responses=responses,
    )
    async def list_search_preparations(
        request: Request, pid: str
    ) -> SearchPreparationBatchList:
        return await await_thread_worker(
            service.list,
            pid,
            may_setup_models=capabilities_for(request, pid).manage_model_downloads,
        )

    @app.get(
        f"{base}/{{batch_id}}",
        operation_id="getSearchPreparation",
        response_model=SearchPreparationBatch,
        responses=responses,
    )
    async def get_search_preparation(
        request: Request, pid: str, batch_id: str
    ) -> SearchPreparationBatch:
        return await await_thread_worker(
            service.get,
            pid,
            batch_id,
            may_setup_models=capabilities_for(request, pid).manage_model_downloads,
        )

    @app.post(
        f"{base}/{{batch_id}}/replan",
        operation_id="replanSearchPreparation",
        response_model=SearchPreparationBatch,
        responses=responses,
    )
    async def replan_search_preparation(
        pid: str, batch_id: str, body: SearchPreparationReplanRequest
    ) -> SearchPreparationBatch:
        return await await_thread_worker(
            service.replan,
            pid,
            batch_id,
            plan_hash=body.plan_hash,
            force_ocr=body.force_ocr,
        )

    @app.post(
        f"{base}/{{batch_id}}/decision",
        operation_id="decideSearchPreparation",
        response_model=SearchPreparationBatch,
        responses=responses,
    )
    async def decide_search_preparation(
        request: Request,
        pid: str,
        batch_id: str,
        body: SearchPreparationDecisionRequest,
    ) -> SearchPreparationBatch:
        capabilities = capabilities_for(request, pid)
        actor = getattr(request.state, "user", None)
        initiated_by = (
            str(actor.get("id"))
            if isinstance(actor, dict) and actor.get("id")
            else None
        )
        return await await_thread_worker(
            service.decide,
            pid,
            batch_id,
            plan_hash=body.plan_hash,
            decision=body.decision,
            selected_setup_refs=body.selected_setup_refs,
            may_setup_models=capabilities.manage_model_downloads,
            initiated_by=initiated_by,
        )

    @app.post(
        f"{base}/{{batch_id}}/stop",
        operation_id="stopSearchPreparation",
        response_model=SearchPreparationBatch,
        responses=responses,
    )
    async def stop_search_preparation(
        pid: str, batch_id: str
    ) -> SearchPreparationBatch:
        return await await_thread_worker(service.stop, pid, batch_id)

    @app.get(
        f"{base}/{{batch_id}}/text-samples",
        operation_id="getSearchPreparationTextSamples",
        response_model=SearchPreparationTextSamples,
        responses=responses,
    )
    async def get_search_preparation_text_samples(
        pid: str,
        batch_id: str,
        row_id: int | None = Query(default=None, gt=0),
        page_number: int | None = Query(default=None, gt=0),
    ) -> SearchPreparationTextSamples:
        return await await_thread_worker(
            service.text_samples,
            pid,
            batch_id,
            row_id=row_id,
            page_number=page_number,
        )


__all__ = ["register_search_preparation_routes"]
