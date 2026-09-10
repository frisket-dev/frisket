"""Instance-level route registration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Response

from frisket.contracts.http.instance_runtime import HealthResponse
from frisket.contracts.http.product_telemetry import ProductTelemetryRequest
from frisket.engine.jobs import JobQueue
from frisket.engine.jobs.queue_health import queue_health_payload
from frisket.operability.telemetry import ProductTelemetryEvent, ProductTelemetryRuntime
from frisket.server.route_errors import http_error_responses
from frisket.server.services.admin_pricing import AdminPricingService
from frisket.server.workspace import Workspace


def register_admin_pricing_routes(
    app: FastAPI,
    *,
    service: AdminPricingService,
) -> None:
    @app.get("/api/admin/pricing/external")
    def external_pricing() -> dict[str, dict[str, Any]]:
        return service.external_pricing()


def register_health_routes(
    app: FastAPI,
    *,
    queue: JobQueue | None = None,
    liveness_window_seconds: float = 90.0,
    timeout_seconds: float | None = None,
    model_pull_enabled: bool = False,
) -> None:
    @app.get(
        "/api/health",
        response_model=HealthResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(500),
    )
    def health() -> HealthResponse:
        body: dict = {"ok": True}
        if queue is not None:
            body["queue"] = queue_health_payload(
                queue,
                now=datetime.now(UTC),
                liveness_window_seconds=liveness_window_seconds,
                timeout_seconds=timeout_seconds,
                model_pull_enabled=model_pull_enabled,
            )
        return HealthResponse.model_validate(body)


def register_product_telemetry_routes(
    app: FastAPI,
    *,
    workspace: Workspace,
    runtime: ProductTelemetryRuntime,
) -> None:
    def accept(body: ProductTelemetryRequest, expected_context: str) -> None:
        try:
            event = ProductTelemetryEvent(type=body.type, properties=body.properties)
        except ValueError as exc:
            raise HTTPException(422, "invalid product telemetry event") from exc
        if event.context != expected_context:
            raise HTTPException(422, "product telemetry event used the wrong route")
        runtime.emit(body.monthly_id, event)

    @app.post(
        "/api/telemetry/events",
        status_code=204,
        name="product_telemetry_installation",
        responses=http_error_responses(401, 422),
    )
    def installation_event(body: ProductTelemetryRequest) -> Response:
        accept(body, "installation")
        return Response(status_code=204)

    @app.post(
        "/api/projects/{pid}/telemetry/events",
        status_code=204,
        name="product_telemetry_project",
        responses=http_error_responses(401, 403, 404, 422),
    )
    def project_event(pid: str, body: ProductTelemetryRequest) -> Response:
        project = workspace.get(pid)
        if not project.project_metadata()["sensitive"]:
            accept(body, "project")
        return Response(status_code=204)


__all__ = [
    "register_admin_pricing_routes",
    "register_health_routes",
    "register_product_telemetry_routes",
]
