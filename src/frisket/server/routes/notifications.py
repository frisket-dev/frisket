"""Notification route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from frisket.contracts.http.notifications import (
    AckResult,
    NotificationActorState,
    NotificationChannel,
    NotificationChannelRequest,
    NotificationChannelsPage,
    NotificationDeliveryRequest,
    NotificationDeliveryRequestsPage,
    NotificationPage,
    NotificationRoute,
    NotificationRouteRequest,
    NotificationRoutesPage,
    NotificationStateFilterRequest,
    NotificationSummary,
    SeenResult,
)
from frisket.server.paging import PageLimit100, PageOffset
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.notifications import (
    NotificationChannelNotFound,
    NotificationNotFound,
    NotificationRequestError,
    NotificationRouteNotFound,
    NotificationService,
)


def register_notification_routes(
    app: FastAPI,
    *,
    service: NotificationService,
) -> None:
    register_typed_error(app, NotificationNotFound, 404, "notification not found")
    register_typed_error(
        app, NotificationChannelNotFound, 404, "notification channel not found"
    )
    register_typed_error(
        app, NotificationRouteNotFound, 404, "notification route not found"
    )
    register_typed_error(app, NotificationRequestError, detail=None)

    @app.get(
        "/api/projects/{pid}/notifications",
        response_model=NotificationPage,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def list_notifications(
        request: Request,
        pid: str,
        state: str = "all",
        source_kind: str | None = None,
        source_ref: str | None = None,
        severity: str | None = None,
        offset: PageOffset = 0,
        limit: PageLimit100 = 50,
    ) -> NotificationPage:
        return service.list_notifications(
            pid,
            request,
            state=state,
            source_kind=source_kind,
            source_ref=source_ref,
            severity=severity,
            offset=offset,
            limit=limit,
        )

    @app.get(
        "/api/projects/{pid}/notifications/summary",
        response_model=NotificationSummary,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def notifications_summary(request: Request, pid: str) -> NotificationSummary:
        return service.summary(pid, request)

    @app.post(
        "/api/projects/{pid}/notifications/seen",
        response_model=SeenResult,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def mark_notifications_seen(
        request: Request, pid: str, body: NotificationStateFilterRequest
    ) -> SeenResult:
        return service.mark_seen(pid, request, body.filter_payload())

    @app.post(
        "/api/projects/{pid}/notifications/ack",
        response_model=AckResult,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def bulk_ack_notifications(
        request: Request, pid: str, body: NotificationStateFilterRequest
    ) -> AckResult:
        return service.bulk_ack(pid, request, body.filter_payload())

    @app.post("/api/projects/{pid}/notifications/emit")
    def emit_notification(request: Request, pid: str, body: dict[str, Any]) -> dict:
        return service.emit(pid, request, body)

    # The three per-item actor-state POSTs stay BODYLESS: a private consumer
    # POSTs with no request body at all and reads actor_id from the response.
    @app.post(
        "/api/projects/{pid}/notifications/{notification_id}/read",
        response_model=NotificationActorState,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def mark_notification_read(
        request: Request, pid: str, notification_id: int
    ) -> NotificationActorState:
        return service.mark_read(pid, request, notification_id)

    @app.post(
        "/api/projects/{pid}/notifications/{notification_id}/ack",
        response_model=NotificationActorState,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def ack_notification(
        request: Request, pid: str, notification_id: int
    ) -> NotificationActorState:
        return service.ack(pid, request, notification_id)

    @app.post(
        "/api/projects/{pid}/notifications/{notification_id}/unack",
        response_model=NotificationActorState,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def unack_notification(
        request: Request, pid: str, notification_id: int
    ) -> NotificationActorState:
        return service.unack(pid, request, notification_id)

    @app.get(
        "/api/projects/{pid}/notification-channels",
        response_model=NotificationChannelsPage,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_notification_channels(
        request: Request, pid: str
    ) -> NotificationChannelsPage:
        return service.list_channels(pid, request)

    @app.post(
        "/api/projects/{pid}/notification-channels",
        response_model=NotificationChannel,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def create_notification_channel(
        request: Request, pid: str, body: NotificationChannelRequest
    ) -> NotificationChannel:
        return service.create_channel(pid, request, body.provided_payload())

    @app.patch(
        "/api/projects/{pid}/notification-channels/{channel_id}",
        response_model=NotificationChannel,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def patch_notification_channel(
        request: Request, pid: str, channel_id: int, body: NotificationChannelRequest
    ) -> NotificationChannel:
        return service.patch_channel(pid, request, channel_id, body.provided_payload())

    @app.get(
        "/api/projects/{pid}/notification-routes",
        response_model=NotificationRoutesPage,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_notification_routes(request: Request, pid: str) -> NotificationRoutesPage:
        return service.list_routes(pid, request)

    @app.post(
        "/api/projects/{pid}/notification-routes",
        response_model=NotificationRoute,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def create_notification_route(
        request: Request, pid: str, body: NotificationRouteRequest
    ) -> NotificationRoute:
        return service.create_route(pid, request, body.provided_payload())

    @app.patch(
        "/api/projects/{pid}/notification-routes/{route_id}",
        response_model=NotificationRoute,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def patch_notification_route(
        request: Request, pid: str, route_id: int, body: NotificationRouteRequest
    ) -> NotificationRoute:
        return service.patch_route(pid, request, route_id, body.provided_payload())

    @app.post(
        "/api/projects/{pid}/notification-routes/{route_id}/test",
        response_model=NotificationDeliveryRequest,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def test_notification_route(
        request: Request, pid: str, route_id: int
    ) -> NotificationDeliveryRequest:
        return service.test_route(pid, request, route_id)

    @app.get(
        "/api/projects/{pid}/notification-delivery-requests",
        response_model=NotificationDeliveryRequestsPage,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def list_notification_delivery_requests(
        request: Request,
        pid: str,
        status: str | None = None,
        route_id: int | None = None,
        channel_id: int | None = None,
        notification_id: int | None = None,
        offset: PageOffset = 0,
        limit: PageLimit100 = 50,
    ) -> NotificationDeliveryRequestsPage:
        return service.list_delivery_requests(
            pid,
            request,
            status=status,
            route_id=route_id,
            channel_id=channel_id,
            notification_id=notification_id,
            offset=offset,
            limit=limit,
        )

    @app.post("/api/projects/{pid}/notifications/{notification_id}/deliver")
    def deliver_notification(
        pid: str, notification_id: int, body: dict[str, Any]
    ) -> dict:
        return service.deliver(pid, notification_id, body)
