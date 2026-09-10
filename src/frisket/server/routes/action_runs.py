"""V1 action execution route registration."""

from __future__ import annotations

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from frisket.contracts.action import ActionResult, Receipt
from frisket.contracts.http.action_estimate_validation import (
    ACTION_RUN_ERROR_RESPONSES,
    ActionRunRequest,
)
from frisket.contracts.http.models import (
    ActionJob,
    ActionJobsPage,
    ActionJobsQuery,
)
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
)
from frisket.server.services.action_runs import (
    ActionRunService,
)
from frisket.server.import_admission import (
    ImportAdmission,
    import_admission_refusal,
    is_import_action,
)


def register_action_run_routes(
    app: FastAPI,
    *,
    service: ActionRunService,
    import_admission: ImportAdmission | None = None,
) -> None:
    @app.post(
        "/api/projects/{pid}/actions/v1/run",
        response_model=ActionResult,
        response_model_exclude_unset=True,
        responses={
            **ACTION_RUN_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422),
        },
    )
    def v1_action_run(
        request: Request,
        pid: str,
        body: ActionRunRequest,
    ) -> ActionResult | JSONResponse:
        is_import = is_import_action(body.root.get("action_id"))
        permit = (
            import_admission.try_acquire()
            if import_admission is not None and is_import
            else None
        )
        if import_admission is not None and is_import and permit is None:
            return import_admission_refusal()
        try:
            response = service.run_action(
                pid,
                body.root,
                request_context=request,
            )
        finally:
            if permit is not None:
                permit.release()
        if response.status_code != 200:
            return JSONResponse(
                status_code=response.status_code,
                content=response.payload,
            )
        return ActionResult.model_validate(response.payload)

    @app.get(
        "/api/projects/{pid}/actions/v1/receipts/{receipt_id}",
        response_model=Receipt,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def v1_receipt_lookup(pid: str, receipt_id: str) -> Receipt:
        return Receipt.model_validate(service.receipt_lookup(pid, receipt_id))

    @app.get(
        "/api/projects/{pid}/actions/jobs",
        response_model=ActionJobsPage,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def action_jobs(
        request: Request,
        pid: str,
        # A supplied query value is always a string; ``None`` represents the
        # parameter being absent. Keeping the FastAPI field non-nullable makes
        # its schema describe the on-wire value rather than the Python default.
        status: str = None,  # type: ignore[assignment]
        limit: int = Query(default=100, ge=1, le=500),
    ) -> ActionJobsPage:
        reject_unknown_query_parameters(request, ActionJobsQuery)
        return ActionJobsPage.model_validate(
            service.list_jobs(pid, status=status, limit=limit)
        )

    @app.get(
        "/api/projects/{pid}/actions/jobs/{job_id}",
        response_model=ActionJob,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def action_job_detail(pid: str, job_id: int) -> ActionJob:
        return ActionJob.model_validate(service.job_detail(pid, job_id))

    @app.post("/api/projects/{pid}/actions/jobs/{job_id}/cancel")
    def action_job_cancel(pid: str, job_id: int) -> dict:
        return service.cancel_job(pid, job_id)
