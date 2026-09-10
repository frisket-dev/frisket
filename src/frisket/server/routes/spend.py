"""Spend dashboard route registration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.spend import SpendReport
from frisket.server.route_errors import http_error_responses
from frisket.server.services.spend import SpendService


def register_spend_routes(
    app: FastAPI,
    *,
    service: SpendService,
) -> None:
    @app.get(
        "/api/spend",
        response_model=SpendReport,
        responses=http_error_responses(401, 403, 500),
    )
    def spend() -> SpendReport:
        return SpendReport.model_validate(service.spend_dashboard())
