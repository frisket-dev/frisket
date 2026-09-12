"""Workspace models-gateway status and setup routes."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from frisket.contracts.http.models_gateway import (
    ModelsGatewayCandidateRequest,
    ModelsGatewaySaveRequest,
    ModelsGatewayStatus,
    ModelsGatewayValidationResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.models_gateway import ModelsGatewayService


class ModelsGatewayAPIRoute(APIRoute):
    """Keep candidate tokens out of FastAPI's request-validation errors."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_validation(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(
                    422,
                    "models gateway request fields are invalid",
                ) from exc

        return safe_validation


def register_models_gateway_routes(
    app: FastAPI, *, service: ModelsGatewayService
) -> None:
    router = APIRouter(route_class=ModelsGatewayAPIRoute)

    @router.get(
        "/api/models-gateway",
        response_model=ModelsGatewayStatus,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 500),
    )
    def get_models_gateway() -> dict:
        return service.status(can_mutate=True)

    @router.post(
        "/api/models-gateway/validate",
        response_model=ModelsGatewayValidationResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 422, 500),
    )
    def validate_models_gateway(body: ModelsGatewayCandidateRequest) -> dict:
        return service.validate_candidate(origin=body.origin, token=body.token)

    @router.put(
        "/api/models-gateway",
        response_model=ModelsGatewayStatus,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 422, 500),
    )
    def set_models_gateway(body: ModelsGatewaySaveRequest) -> dict:
        try:
            return service.save(
                origin=body.origin,
                token=body.token,
                validation_token=body.validation_token,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    app.include_router(router)


__all__ = ["register_models_gateway_routes"]
