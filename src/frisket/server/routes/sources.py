"""Source route registration."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from frisket.contracts.http.project_sources import (
    ProjectSourceDetail,
    ProjectSourceHealth,
    ProjectSourceList,
)
from frisket.server.paging import PageLimit100, PageOffset
from frisket.server.route_errors import http_error_responses
from frisket.server.services.sources import SourceNotFound, SourceService


def register_source_routes(
    app: FastAPI,
    *,
    service: SourceService,
) -> None:
    @app.get(
        "/api/projects/{pid}/sources",
        response_model=ProjectSourceList,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_sources(pid: str) -> ProjectSourceList:
        """All live sources for a project (empty list when none configured)."""
        return service.list_sources(pid)

    @app.get(
        "/api/projects/{pid}/sources/{source_id}",
        response_model=ProjectSourceDetail,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def get_source_ep(
        pid: str,
        source_id: int,
        runs_offset: PageOffset = 0,
        runs_limit: PageLimit100 = 50,
    ) -> ProjectSourceDetail:
        try:
            return service.get_source(
                pid,
                source_id,
                runs_offset=runs_offset,
                runs_limit=runs_limit,
            )
        except SourceNotFound as exc:
            raise HTTPException(404, "source not found") from exc

    @app.get(
        "/api/projects/{pid}/sources/{source_id}/health",
        response_model=ProjectSourceHealth,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def get_source_health_ep(
        pid: str,
        source_id: int,
        runs_offset: PageOffset = 0,
        runs_limit: PageLimit100 = 20,
    ) -> ProjectSourceHealth:
        try:
            return service.get_source_health(
                pid,
                source_id,
                runs_offset=runs_offset,
                runs_limit=runs_limit,
            )
        except SourceNotFound as exc:
            raise HTTPException(404, "source not found") from exc
