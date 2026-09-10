"""Action run support route registrations."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query, Request

from frisket.contracts.http.models import (
    ActionRunCancel,
    ActionRunRows,
    ActionRunRowsQuery,
    ActionRunStatus,
    EmptyQuery,
)
from frisket.contracts.http.project_attempts import ProjectAttemptsPage
from frisket.contracts.http.run_provenance import RunTraceRowEvidence
from frisket.server.paging import PageLimit100, PageLimit500, PageOffset
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
)
from frisket.server.services.action_run_cancel import ActionRunCancelService
from frisket.server.services.action_run_rows import ActionRunRowsService
from frisket.server.services.action_run_status import ActionRunStatusService
from frisket.server.services.action_run_trace import ActionRunTraceService
from frisket.server.services.project_attempts import ProjectAttemptsService
from frisket.server.services.run_attempts import RunAttemptsService


def register_action_run_cancel_routes(
    app: FastAPI,
    *,
    service: ActionRunCancelService,
) -> None:
    @app.post(
        "/api/projects/{pid}/actions/runs/{run_id}/cancel",
        response_model=ActionRunCancel,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def cancel_run(request: Request, pid: str, run_id: int) -> ActionRunCancel:
        reject_unknown_query_parameters(request, EmptyQuery)
        return ActionRunCancel.model_validate(service.cancel_run(pid, run_id))


def register_action_run_status_routes(
    app: FastAPI,
    *,
    service: ActionRunStatusService,
) -> None:
    @app.get(
        "/api/projects/{pid}/actions/runs/{run_id}/status",
        response_model=ActionRunStatus,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def action_run_status(request: Request, pid: str, run_id: int) -> ActionRunStatus:
        reject_unknown_query_parameters(request, EmptyQuery)
        return ActionRunStatus.model_validate(service.run_status(pid, run_id))


def register_action_run_rows_routes(
    app: FastAPI,
    *,
    service: ActionRunRowsService,
) -> None:
    @app.get(
        "/api/projects/{pid}/actions/runs/{run_id}/rows",
        response_model=ActionRunRows,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def run_rows(
        request: Request,
        pid: str,
        run_id: int,
        offset: PageOffset = 0,
        limit: PageLimit500 = 50,
        status: str = None,  # type: ignore[assignment]
    ) -> ActionRunRows:
        reject_unknown_query_parameters(request, ActionRunRowsQuery)
        return ActionRunRows.model_validate(
            service.run_rows(
                pid,
                run_id,
                offset=offset,
                limit=limit,
                status=status,
            )
        )


def register_action_run_trace_routes(
    app: FastAPI,
    *,
    service: ActionRunTraceService,
) -> None:
    @app.get("/api/projects/{pid}/actions/runs/{run_id}/trace")
    def action_run_trace(pid: str, run_id: int) -> dict[str, Any]:
        return service.run_trace(pid, run_id)

    @app.get(
        "/api/projects/{pid}/actions/runs/{run_id}/trace/rows/{row_id}",
        response_model=RunTraceRowEvidence,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def action_run_trace_row(
        pid: str,
        run_id: int,
        row_id: int,
        column_id: int | None = Query(default=None, ge=1),
    ) -> RunTraceRowEvidence:
        return RunTraceRowEvidence.model_validate(
            service.run_trace_row(
                pid,
                run_id,
                row_id,
                column_id=column_id,
            )
        )


def register_run_attempt_routes(
    app: FastAPI,
    *,
    service: RunAttemptsService,
) -> None:
    @app.get("/api/projects/{pid}/actions/runs/{run_id}/attempts")
    def run_attempts(pid: str, run_id: int) -> dict[str, Any]:
        """The attempt receipt is an acceptance criterion, not
        an extra: without it ``execution_attempts`` is an internal forensic
        table and three of the four product sentences ("where did my data
        go", "what did it cost", "did I say yes to that") go unanswered.

        Each entry carries the six facts: which rows the attempt covered,
        which target/provider the data went to, which consent authorized it
        and whether that consent was direct or derived
        (``consent.grant_basis``), which cost basis and terms version were
        authorized, and its terminal outcome. The same view joins settlement
        on ``attempt_id``.
        """
        return service.receipts(pid, run_id)


def register_project_attempts_routes(
    app: FastAPI,
    *,
    service: ProjectAttemptsService,
) -> None:
    @app.get(
        "/api/projects/{pid}/actions/attempts",
        response_model=ProjectAttemptsPage,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def project_attempts(
        pid: str,
        run_id: int | None = None,
        offset: PageOffset = 0,
        limit: PageLimit100 = 25,
    ) -> dict[str, Any]:
        """Attempt receipts for one project, newest first (paged).

        The reader that can reach a COMPACTION-ORPHANED attempt. Its sibling
        ``run_attempts`` is ``WHERE run_id=?``, and ruling 7 deliberately
        NULLs ``run_id`` when ``compact()`` reclaims the run, so the record
        the ruling preserved — the consent and the charge for exactly the
        runs a user is most likely to ask about — had no opener. Here the
        orphan is an ordinary row with a null ``run_id``.

        ``?run_id=`` narrows to one run's attempts (what the run detail
        surface asks) off the same query, so the run view and the project
        list cannot disagree about a receipt.
        """
        return service.page(pid, run_id=run_id, offset=offset, limit=limit)
