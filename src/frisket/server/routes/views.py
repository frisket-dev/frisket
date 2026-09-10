"""Saved view and saved lens route registration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.views_lenses import (
    SavedLens,
    SavedLensCreateRequest,
    SavedLensDelete,
    SavedLensList,
    SavedLensPatchRequest,
    SavedLensResolved,
    SavedView,
    SavedViewCreateRequest,
    SavedViewDefinitionReplaceRequest,
    SavedViewDelete,
    SavedViewList,
    SavedViewRenameRequest,
)
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.views import (
    LensNotFound,
    LensRequestError,
    ViewLensService,
    ViewNotFound,
)


_VIEW_ERRORS = (401, 403, 404, 409, 422, 500)
# The two lens routes that raise LensRequestError also answer 400.
_LENS_ERRORS = (400, *_VIEW_ERRORS)


def register_view_lens_routes(
    app: FastAPI,
    *,
    service: ViewLensService,
) -> None:
    register_typed_error(app, ViewNotFound, 404, "view not found")
    register_typed_error(app, LensNotFound, 404, "lens not found")
    register_typed_error(app, LensRequestError, 400, lambda exc: exc.detail())

    @app.get(
        "/api/projects/{pid}/views",
        response_model=SavedViewList,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def list_views(pid: str, sheet_id: int | None = None) -> SavedViewList:
        return service.list_views(pid, sheet_id=sheet_id)

    @app.post(
        "/api/projects/{pid}/views",
        response_model=SavedView,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def create_view(pid: str, body: SavedViewCreateRequest) -> SavedView:
        return service.create_view(
            pid,
            name=body.name,
            sheet_id=body.sheet_id,
            filter_=body.filter,
            sort=body.sort,
            columns=body.columns,
            column_groups=body.column_groups,
        )

    # Typed and fenced. Typing a route does not project it: browser
    # membership is the endpoint catalog's decision, and this one stays off.
    @app.get(
        "/api/projects/{pid}/views/{view_id}",
        response_model=SavedView,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def get_view_ep(pid: str, view_id: int) -> SavedView:
        return service.get_view(pid, view_id)

    @app.patch(
        "/api/projects/{pid}/views/{view_id}",
        response_model=SavedView,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def patch_view(
        pid: str,
        view_id: int,
        body: SavedViewRenameRequest,
    ) -> SavedView:
        return service.rename_view(pid, view_id, name=body.name)

    @app.put(
        "/api/projects/{pid}/views/{view_id}/definition",
        response_model=SavedView,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def replace_view_definition(
        pid: str,
        view_id: int,
        body: SavedViewDefinitionReplaceRequest,
    ) -> SavedView:
        return service.replace_view_definition(
            pid,
            view_id,
            filter_=body.filter,
            sort=body.sort,
            columns=body.columns,
            column_groups=body.column_groups,
        )

    @app.delete(
        "/api/projects/{pid}/views/{view_id}",
        response_model=SavedViewDelete,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def delete_view_ep(pid: str, view_id: int) -> SavedViewDelete:
        return service.delete_view(pid, view_id)

    @app.get(
        "/api/projects/{pid}/lenses",
        response_model=SavedLensList,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def list_lenses(pid: str, sheet_id: int | None = None) -> SavedLensList:
        return service.list_lenses(pid, sheet_id=sheet_id)

    @app.post(
        "/api/projects/{pid}/lenses",
        response_model=SavedLens,
        responses=http_error_responses(*_LENS_ERRORS),
    )
    def create_lens(pid: str, body: SavedLensCreateRequest) -> SavedLens:
        return service.create_lens(
            pid,
            name=body.name,
            query=body.query,
            presentation=body.presentation,
        )

    @app.get(
        "/api/projects/{pid}/lenses/{lens_id}",
        response_model=SavedLens,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def get_lens_ep(pid: str, lens_id: int) -> SavedLens:
        return service.get_lens(pid, lens_id)

    @app.patch(
        "/api/projects/{pid}/lenses/{lens_id}",
        response_model=SavedLens,
        responses=http_error_responses(*_LENS_ERRORS),
    )
    def patch_lens(
        pid: str,
        lens_id: int,
        body: SavedLensPatchRequest,
    ) -> SavedLens:
        return service.patch_lens(
            pid,
            lens_id,
            fields=body.model_dump(),
            provided_fields=body.model_fields_set,
        )

    @app.delete(
        "/api/projects/{pid}/lenses/{lens_id}",
        response_model=SavedLensDelete,
        responses=http_error_responses(*_VIEW_ERRORS),
    )
    def delete_lens_ep(pid: str, lens_id: int) -> SavedLensDelete:
        return service.delete_lens(pid, lens_id)

    @app.get(
        "/api/projects/{pid}/lenses/{lens_id}/resolve",
        response_model=SavedLensResolved,
        responses=http_error_responses(*_LENS_ERRORS),
    )
    def resolve_lens(
        pid: str,
        lens_id: int,
        # Deliberately unbounded: a live caller passes limit=5000 and the
        # preview spine clamps to MAX_QUERY_PREVIEW_LIMIT internally.
        limit: int = 50,
        offset: int = 0,
    ) -> SavedLensResolved:
        return service.resolve_lens(pid, lens_id, limit=limit, offset=offset)
