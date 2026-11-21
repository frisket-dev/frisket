"""Alert route registration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.alerts import (
    Alert,
    AlertCheckEventsPage,
    AlertCheckResult,
    AlertChecksPage,
    AlertCreateRequest,
    AlertDelete,
    AlertList,
    AlertPatchRequest,
)
from frisket.server.paging import PageLimit100, PageLimit500, PageOffset
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.alerts import (
    AlertCheckNotFound,
    AlertNotFound,
    AlertRequestError,
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
        "/api/projects/{pid}/alerts",
        response_model=AlertList,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_alerts(pid: str) -> AlertList:
        return service.list_alerts(pid)

    @app.post(
        "/api/projects/{pid}/alerts",
        response_model=Alert,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def create_alert(pid: str, body: AlertCreateRequest) -> Alert:
        return service.create_alert(
            pid,
            name=body.name,
            view_id=body.view_id,
            detection_policy=body.detection_policy,
            enabled=body.enabled,
            schedule=body.schedule,
        )

    @app.patch(
        "/api/projects/{pid}/alerts/{alert_id}",
        response_model=Alert,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def patch_alert(pid: str, alert_id: int, body: AlertPatchRequest) -> Alert:
        fields = {
            field: getattr(body, field)
            for field in body.model_fields_set
            if field in {"name", "enabled", "detection_policy", "schedule"}
        }
        return service.patch_alert(pid, alert_id, fields=fields)

    @app.delete(
        "/api/projects/{pid}/alerts/{alert_id}",
        response_model=AlertDelete,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def delete_alert(pid: str, alert_id: int) -> AlertDelete:
        return service.delete_alert(pid, alert_id)

    # check_alert stays BODYLESS: an event-triggered evaluation with no request
    # payload.
    @app.post(
        "/api/projects/{pid}/alerts/{alert_id}/check",
        response_model=AlertCheckResult,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def check_alert(pid: str, alert_id: int) -> AlertCheckResult:
        return service.check_alert(pid, alert_id)

    @app.get(
        "/api/projects/{pid}/alerts/{alert_id}/checks",
        response_model=AlertChecksPage,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def list_alert_checks(
        pid: str,
        alert_id: int,
        offset: PageOffset = 0,
        limit: PageLimit100 = 20,
        hits_limit: PageLimit500 = 20,
    ) -> AlertChecksPage:
        return service.list_alert_checks(
            pid,
            alert_id,
            offset=offset,
            limit=limit,
            hits_limit=hits_limit,
        )

    @app.get(
        "/api/projects/{pid}/alerts/{alert_id}/checks/{check_id}/events",
        response_model=AlertCheckEventsPage,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def list_alert_check_events(
        pid: str,
        alert_id: int,
        check_id: int,
        offset: PageOffset = 0,
        limit: PageLimit100 = 50,
        event_kind: str | None = None,
    ) -> AlertCheckEventsPage:
        return service.list_alert_check_events(
            pid,
            alert_id,
            check_id,
            offset=offset,
            limit=limit,
            event_kind=event_kind,
        )
