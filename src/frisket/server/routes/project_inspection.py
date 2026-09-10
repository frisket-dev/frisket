"""Project inspection route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from frisket.contracts.http.history_review import HistoryPage
from frisket.contracts.http.onboarding_imports import SampleProjectSeedResponse
from frisket.server.route_errors import http_error_responses
from frisket.server.services.project_debug import ProjectDebugService
from frisket.server.services.project_history import ProjectHistoryService
from frisket.server.services.project_timing import ProjectTimingService
from frisket.server.services.sample_project import (
    SampleProjectSeedError,
    SampleProjectSeedService,
)


def register_project_debug_routes(
    app: FastAPI,
    *,
    service: ProjectDebugService,
) -> None:
    @app.get("/api/projects/{pid}/debug")
    def project_debug(pid: str) -> dict[str, Any]:
        return service.debug(pid)


def register_project_timing_routes(
    app: FastAPI,
    *,
    service: ProjectTimingService,
) -> None:
    @app.get("/api/projects/{pid}/timing")
    def project_timing(pid: str) -> dict[str, Any]:
        return service.project_timing(pid)


def register_project_history_routes(
    app: FastAPI,
    *,
    service: ProjectHistoryService,
) -> None:
    @app.get(
        "/api/projects/{pid}/history",
        response_model=HistoryPage,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def history(
        pid: str,
        offset: int | None = Query(default=None, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> HistoryPage:
        return HistoryPage.model_validate(
            service.history(pid, offset=offset, limit=limit)
        )


def register_sample_project_routes(
    app: FastAPI,
    *,
    service: SampleProjectSeedService,
) -> None:
    @app.post(
        "/api/projects/{pid}/seed-sample",
        response_model=SampleProjectSeedResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def seed_sample_project(pid: str) -> dict:
        try:
            return service.seed(pid)
        except SampleProjectSeedError as exc:
            raise HTTPException(500, str(exc)) from exc
