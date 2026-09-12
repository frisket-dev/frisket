"""Project-scoped selector-choice route registration."""

from __future__ import annotations

from fastapi import FastAPI, Request

from frisket.contracts.http.selector_choices import (
    SelectorChoicesQuery,
    SelectorChoicesResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.selector_choices import (
    SelectorCapabilitiesFor,
    SelectorChoiceService,
    SelectorModelsGatewayStatusFor,
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


__all__ = ["register_selector_choices_routes"]
