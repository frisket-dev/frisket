"""Alert route registration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.alerts import (
    Watch,
    WatchCreateRequest,
    WatchDelete,
    WatchList,
    WatchPatchRequest,
    WatchRunEventsPage,
    WatchRunResult,
    WatchRunsPage,
)
from frisket.server.paging import PageLimit100, PageLimit500, PageOffset
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.alerts import (
    AlertNotFound,
    AlertRequestError,
    AlertCheckNotFound,
    AlertService,
)


def register_alert_routes(
    app: FastAPI,
    *,
    service: AlertService,
) -> None:
    register_typed_error(app, AlertNotFound, 404, "alert not found")
    register_typed_error(app, AlertCheckNotFound, 404, "alert check not found")
    register_typed_error(app, AlertRequestError, detail=None)

    @app.get(
        "/api/projects/{pid}/watches",
        response_model=WatchList,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_watches(pid: str) -> WatchList:
        return service.list_alerts(pid)

    @app.post(
        "/api/projects/{pid}/watches",
        response_model=Watch,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def create_watch(pid: str, body: WatchCreateRequest) -> Watch:
        return service.create_alert(
            pid,
            name=body.name,
            view_id=body.view_id,
            detection_policy=body.detection_policy,
            enabled=body.enabled,
            schedule=body.schedule,
        )

    @app.patch(
        "/api/projects/{pid}/watches/{watch_id}",
        response_model=Watch,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def patch_watch(pid: str, watch_id: int, body: WatchPatchRequest) -> Watch:
        fields = {
            field: getattr(body, field)
            for field in body.model_fields_set
            if field in {"name", "enabled", "detection_policy", "schedule"}
        }
        return service.patch_alert(pid, watch_id, fields=fields)

    @app.delete(
        "/api/projects/{pid}/watches/{watch_id}",
        response_model=WatchDelete,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def delete_watch(pid: str, watch_id: int) -> WatchDelete:
        return service.delete_alert(pid, watch_id)

    # run_watch stays BODYLESS: an event-triggered evaluation with no request
    # payload today and none after the migration.
    @app.post(
        "/api/projects/{pid}/watches/{watch_id}/run",
        response_model=WatchRunResult,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def run_watch(pid: str, watch_id: int) -> WatchRunResult:
        return service.check_alert(pid, watch_id)

    @app.get(
        "/api/projects/{pid}/watches/{watch_id}/runs",
        response_model=WatchRunsPage,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def list_watch_runs(
        pid: str,
        watch_id: int,
        offset: PageOffset = 0,
        limit: PageLimit100 = 20,
        hits_limit: PageLimit500 = 20,
    ) -> WatchRunsPage:
        return service.list_alert_checks(
            pid,
            watch_id,
            offset=offset,
            limit=limit,
            hits_limit=hits_limit,
        )

    @app.get(
        "/api/projects/{pid}/watches/{watch_id}/runs/{run_id}/events",
        response_model=WatchRunEventsPage,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def list_watch_run_events(
        pid: str,
        watch_id: int,
        run_id: int,
        offset: PageOffset = 0,
        limit: PageLimit100 = 50,
        event_kind: str | None = None,
    ) -> WatchRunEventsPage:
        return service.list_alert_check_events(
            pid,
            watch_id,
            run_id,
            offset=offset,
            limit=limit,
            event_kind=event_kind,
        )
