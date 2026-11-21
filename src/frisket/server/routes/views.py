"""View route registration."""

from __future__ import annotations

from fastapi import FastAPI
from frisket.contracts.http.views import (
    View,
    ViewCreateRequest,
    ViewDefinitionReplaceRequest,
    ViewDelete,
    ViewList,
    ViewRenameRequest,
    ViewResolved,
)
from frisket.server.paging import PageLimit500, PageOffset
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.views import ViewService, ViewNotFound, ViewRequestError

_VIEW_ERRORS = (400, 401, 403, 404, 409, 422, 500)


def register_view_routes(app: FastAPI, *, service: ViewService) -> None:
    register_typed_error(app, ViewNotFound, 404, "view not found")
    register_typed_error(app, ViewRequestError, 400, lambda exc: exc.detail())

    @app.get(
        "/api/projects/{pid}/views",
        response_model=ViewList,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def list_views(pid: str, sheet_id: int | None = None) -> ViewList:
        return service.list_views(pid, sheet_id=sheet_id)

    @app.post(
        "/api/projects/{pid}/views",
        response_model=View,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def create_view(pid: str, body: ViewCreateRequest) -> View:
        return service.create_view(
            pid,
            name=body.name,
            selection=body.selection.model_dump(exclude_none=True),
            presentation=body.presentation,
        )

    @app.get(
        "/api/projects/{pid}/views/{view_id}",
        response_model=View,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def get_view_ep(pid: str, view_id: int) -> View:
        return service.get_view(pid, view_id)

    @app.patch(
        "/api/projects/{pid}/views/{view_id}",
        response_model=View,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def patch_view(pid: str, view_id: int, body: ViewRenameRequest) -> View:
        return service.rename_view(pid, view_id, name=body.name)

    @app.put(
        "/api/projects/{pid}/views/{view_id}/definition",
        response_model=View,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def replace_view_definition(
        pid: str, view_id: int, body: ViewDefinitionReplaceRequest
    ) -> View:
        return service.replace_view_definition(
            pid,
            view_id,
            selection=body.selection.model_dump(exclude_none=True),
            presentation=body.presentation,
        )

    @app.delete(
        "/api/projects/{pid}/views/{view_id}",
        response_model=ViewDelete,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def delete_view_ep(pid: str, view_id: int) -> ViewDelete:
        return service.delete_view(pid, view_id)

    @app.get(
        "/api/projects/{pid}/views/{view_id}/resolve",
        response_model=ViewResolved,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def resolve_view(
        pid: str, view_id: int, limit: PageLimit500 = 50, offset: PageOffset = 0
    ) -> ViewResolved:
        return service.resolve_view(pid, view_id, limit=limit, offset=offset)
