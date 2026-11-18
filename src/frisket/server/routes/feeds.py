"""Feed route registration (paths stay under ``/sources``)."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.project_sources import (
    ProjectSourceDetail,
    ProjectSourceHealth,
    ProjectSourceList,
)
from frisket.server.paging import PageLimit100, PageOffset
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.feeds import FeedNotFound, FeedService


def register_feed_routes(
    app: FastAPI,
    *,
    service: FeedService,
) -> None:
    register_typed_error(app, FeedNotFound, 404, "feed not found")

    @app.get(
        "/api/projects/{pid}/sources",
        response_model=ProjectSourceList,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_sources(pid: str) -> ProjectSourceList:
        """All live sources for a project (empty list when none configured)."""
        return service.list_feeds(pid)

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
        return service.get_feed(
            pid,
            source_id,
            runs_offset=runs_offset,
            runs_limit=runs_limit,
        )

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
        return service.get_feed_health(
            pid,
            source_id,
            runs_offset=runs_offset,
            runs_limit=runs_limit,
        )
