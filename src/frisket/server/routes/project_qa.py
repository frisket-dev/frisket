"""Project-scoped Ask HTTP surface; editor admission is in endpoint policies."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Response

from frisket.contracts.http.models import EmptyQuery
from frisket.contracts.http.project_qa import (
    AskCitation,
    AskEventsPage,
    AskEventsQuery,
    AskReport,
    AskThread,
    AskThreadCreate,
    AskThreadDetail,
    AskThreadUpdate,
    AskTurn,
    AskTurnRequest,
)
from frisket.engine.store.project_qa import (
    ProjectQAConflictError,
    ProjectQANotFoundError,
)
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
)
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.project_qa_tools import ProjectQAScopeError
from frisket.server.services.project_qa_citations import resolve_citation


@contextmanager
def _errors():
    try:
        yield
    except ProjectQAScopeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ProjectQANotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProjectQAConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


def _actor(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    actor_id = user.get("id") if isinstance(user, dict) else None
    return str(actor_id) if actor_id is not None else None


def register_project_qa_routes(app: FastAPI, *, service: ProjectQAService) -> None:
    errors = http_error_responses(401, 403, 404, 409, 422, 500)
    path = "/api/projects/{pid}/qa/threads"

    @app.get(path, response_model=list[AskThread], responses=errors)
    async def qa_threads(request: Request, pid: str) -> list[AskThread]:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return [AskThread.model_validate(item) for item in service.list(pid)]

    @app.post(path, response_model=AskThread, responses=errors)
    async def qa_create_thread(
        request: Request, pid: str, body: AskThreadCreate
    ) -> AskThread:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskThread.model_validate(
                service.create(pid, body, actor=_actor(request))
            )

    @app.get(path + "/{thread_id}", response_model=AskThreadDetail, responses=errors)
    async def qa_thread(request: Request, pid: str, thread_id: str) -> AskThreadDetail:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskThreadDetail.model_validate(service.detail(pid, thread_id))

    @app.patch(path + "/{thread_id}", response_model=AskThread, responses=errors)
    async def qa_update_thread(
        request: Request, pid: str, thread_id: str, body: AskThreadUpdate
    ) -> AskThread:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskThread.model_validate(service.update(pid, thread_id, body))

    @app.delete(path + "/{thread_id}", status_code=204, responses=errors)
    async def qa_delete_thread(request: Request, pid: str, thread_id: str) -> Response:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            service.delete(pid, thread_id)
        return Response(status_code=204)

    @app.post(path + "/{thread_id}/turns", response_model=AskTurn, responses=errors)
    async def qa_submit_turn(
        request: Request, pid: str, thread_id: str, body: AskTurnRequest
    ) -> AskTurn:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskTurn.model_validate(
                service.submit(pid, thread_id, body, actor=_actor(request))
            )

    @app.get(
        path + "/{thread_id}/events", response_model=AskEventsPage, responses=errors
    )
    async def qa_events(
        request: Request,
        pid: str,
        thread_id: str,
        after: int = Query(default=0, ge=0),
        before: int | None = Query(default=None, gt=0),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> AskEventsPage:
        reject_unknown_query_parameters(request, AskEventsQuery)
        if after and before is not None:
            raise HTTPException(422, "Use either before or after, not both.")
        with _errors():
            return AskEventsPage.model_validate(
                service.events(pid, thread_id, after=after, before=before, limit=limit)
            )

    @app.post(
        path + "/{thread_id}/turns/{turn_id}/stop",
        response_model=AskTurn,
        responses=errors,
    )
    async def qa_stop_turn(
        request: Request, pid: str, thread_id: str, turn_id: str
    ) -> AskTurn:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskTurn.model_validate(service.stop(pid, thread_id, turn_id))

    @app.get(path + "/{thread_id}/report", response_model=AskReport, responses=errors)
    async def qa_report(request: Request, pid: str, thread_id: str) -> AskReport:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskReport.model_validate(service.report(pid, thread_id))

    @app.get(
        path + "/{thread_id}/citations/{citation_id}",
        response_model=AskCitation,
        responses=errors,
    )
    async def qa_citation(
        request: Request, pid: str, thread_id: str, citation_id: str
    ) -> AskCitation:
        reject_unknown_query_parameters(request, EmptyQuery)
        with _errors():
            return AskCitation.model_validate(
                resolve_citation(service.workspace.get(pid), thread_id, citation_id)
            )
