"""Project-scoped selector-choice route registration."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from frisket.contracts.http.selector_choices import (
    ProjectModelRouteRequest,
    ProjectModelRouteResponse,
    SelectorChoicesQuery,
    SelectorChoicesResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.selector_choices import (
    SelectorCapabilitiesFor,
    SelectorChoiceService,
    SelectorModelsGatewayStatusFor,
)
from frisket.server.services.project_model_routes import (
    ProjectModelRouteError,
    ProjectModelRoutesService,
)
from frisket.server.workspace import Workspace


def register_selector_choices_routes(
    app: FastAPI,
    *,
    workspace: Workspace,
    capabilities_for: SelectorCapabilitiesFor,
    edition: str = "solo",
    models_gateway_status_for: SelectorModelsGatewayStatusFor | None = None,
) -> None:
    service = SelectorChoiceService(
        workspace, edition=edition, models_gateway_status_for=models_gateway_status_for
    )
    model_routes = ProjectModelRoutesService(workspace)

    @app.post(
        "/api/projects/{pid}/selector-choices",
        response_model=SelectorChoicesResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def selector_choices(
        request: Request,
        pid: str,
        body: SelectorChoicesQuery,
    ) -> SelectorChoicesResponse:
        return service.choices(
            pid,
            body,
            request_context=request,
            capabilities=capabilities_for(request, pid),
        )

    @app.patch(
        "/api/projects/{pid}/model-routes",
        response_model=ProjectModelRouteResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def set_project_model_route(
        request: Request,
        pid: str,
        body: ProjectModelRouteRequest,
    ) -> ProjectModelRouteResponse:
        if not capabilities_for(request, pid).may_author_actions:
            raise HTTPException(
                status_code=403,
                detail="Project editor access is required to change model routes.",
            )
        try:
            selected = model_routes.set_route(pid, body.model_id, body.route)
        except ProjectModelRouteError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return ProjectModelRouteResponse(
            model_id=selected.model_id,
            route=selected.route,
        )


__all__ = ["register_selector_choices_routes"]
