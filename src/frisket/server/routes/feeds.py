"""Feed route registration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.project_feeds import (
    ProjectFeedDetail,
    ProjectFeedHealth,
    ProjectFeedList,
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
        "/api/projects/{pid}/feeds",
        response_model=ProjectFeedList,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_feeds(pid: str) -> ProjectFeedList:
        """All live feeds for a project (empty list when none configured)."""
        return service.list_feeds(pid)

    @app.get(
        "/api/projects/{pid}/feeds/{feed_id}",
        response_model=ProjectFeedDetail,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def get_feed(
        pid: str,
        feed_id: int,
        runs_offset: PageOffset = 0,
        runs_limit: PageLimit100 = 50,
    ) -> ProjectFeedDetail:
        return service.get_feed(
            pid,
            feed_id,
            runs_offset=runs_offset,
            runs_limit=runs_limit,
        )

    @app.get(
        "/api/projects/{pid}/feeds/{feed_id}/health",
        response_model=ProjectFeedHealth,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def get_feed_health(
        pid: str,
        feed_id: int,
        runs_offset: PageOffset = 0,
        runs_limit: PageLimit100 = 20,
    ) -> ProjectFeedHealth:
        return service.get_feed_health(
            pid,
            feed_id,
            runs_offset=runs_offset,
            runs_limit=runs_limit,
        )
