"""Walkthrough catalog route registration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.walkthroughs import WalkthroughCatalogResponse
from frisket.server.route_errors import http_error_responses
from frisket.server.walkthroughs import walkthrough_catalog


def register_walkthrough_routes(app: FastAPI) -> None:
    @app.get(
        "/api/walkthroughs",
        response_model=WalkthroughCatalogResponse,
        responses=http_error_responses(401, 500),
    )
    def list_walkthroughs() -> dict[str, list[dict[str, object]]]:
        return walkthrough_catalog()
