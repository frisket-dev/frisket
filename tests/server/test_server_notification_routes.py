from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.notifications import register_notification_routes
from frisket.server.services.notifications import (
    NotificationChannelNotFound,
    NotificationNotFound,
    NotificationRequestError,
    NotificationRouteNotFound,
)


def test_notification_routes_bind_query_body_request_and_map_service_errors() -> None:
    service = _FakeNotificationService()
    app = FastAPI()
    register_notification_routes(app, service=service)
    client = TestClient(app)

    listed = client.get(
        "/api/projects/p/notifications",
        headers={"x-test-actor": "route"},
        params={
            "state": "read",
            "source_kind": "watch",
            "source_ref": '{"watch_id":1}',
            "severity": "warning",
            "offset": 2,
            "limit": 3,
        },
    )
    assert listed.status_code == 200
    assert service.calls[-1] == (
        "list_notifications",
        "p",
        "route",
        "read",
        "watch",
        '{"watch_id":1}',
        "warning",
        2,
        3,
    )

    bad_state = client.get("/api/projects/p/notifications", params={"state": "bad"})
    assert bad_state.status_code == 400
    assert bad_state.json()["detail"] == "unsupported notification state"

    summary = client.get(
        "/api/projects/p/notifications/summary", headers={"x-test-actor": "summary"}
    )
    assert summary.status_code == 200
    assert service.calls[-1] == ("summary", "p", "summary")

    seen = client.post(
        "/api/projects/p/notifications/seen",
        json={"notification_ids": [7]},
        headers={"x-test-actor": "seen"},
    )
    assert seen.status_code == 200
    assert service.calls[-1] == (
        "mark_seen",
        "p",
        "seen",
        {"notification_ids": [7]},
    )

    ack = client.post(
        "/api/projects/p/notifications/ack",
        json={"source_kind": "watch"},
        headers={"x-test-actor": "ack"},
    )
    assert ack.status_code == 200
    assert service.calls[-1] == (
        "bulk_ack",
        "p",
        "ack",
        {"source_kind": "watch"},
    )

    emitted = client.post(
        "/api/projects/p/notifications/emit",
        json={"source_kind": "plugin.acme"},
        headers={"x-test-actor": "emit"},
    )
    assert emitted.status_code == 200
    assert service.calls[-1] == ("emit", "p", "emit", {"source_kind": "plugin.acme"})

    read = client.post(
        "/api/projects/p/notifications/9/read",
        headers={"x-test-actor": "read"},
    )
    assert read.status_code == 200
    assert service.calls[-1] == ("mark_read", "p", "read", 9)

    missing_read = client.post("/api/projects/p/notifications/404/read")
    assert missing_read.status_code == 404
    assert missing_read.json()["detail"] == "notification not found"

    item_ack = client.post(
        "/api/projects/p/notifications/9/ack",
        headers={"x-test-actor": "item-ack"},
    )
    assert item_ack.status_code == 200
    assert service.calls[-1] == ("ack", "p", "item-ack", 9)

    unack = client.post(
        "/api/projects/p/notifications/9/unack",
        headers={"x-test-actor": "unack"},
    )
    assert unack.status_code == 200
    assert service.calls[-1] == ("unack", "p", "unack", 9)

    channels = client.get("/api/projects/p/notification-channels")
    assert channels.status_code == 200
    assert service.calls[-1] == ("list_channels", "p", None)

    created = client.post(
        "/api/projects/p/notification-channels",
        json={
            "kind": "email",
            "name": "Ops",
            "enabled": "false",
            "raw_enabled": False,
            "from": "ops@example.com",
            "from_": "not-a-wire-key",
        },
        headers={"x-test-actor": "create-channel"},
    )
    assert created.status_code == 200
    assert service.calls[-1] == (
        "create_channel",
        "p",
        "create-channel",
        {
            "kind": "email",
            "name": "Ops",
            "enabled": "false",
            "from": "ops@example.com",
        },
    )

    patched = client.patch(
        "/api/projects/p/notification-channels/8",
        json={"enabled": False},
        headers={"x-test-actor": "patch-channel"},
    )
    assert patched.status_code == 200
    assert service.calls[-1] == (
        "patch_channel",
        "p",
        "patch-channel",
        8,
        {"enabled": False},
    )

    missing_channel = client.patch(
        "/api/projects/p/notification-channels/404",
        json={"enabled": False},
    )
    assert missing_channel.status_code == 404
    assert missing_channel.json()["detail"] == "notification channel not found"

    routes = client.get("/api/projects/p/notification-routes")
    assert routes.status_code == 200
    assert service.calls[-1] == ("list_routes", "p", None)

    created_route = client.post(
        "/api/projects/p/notification-routes",
        json={"name": "Watch Slack", "channel_id": 8},
        headers={"x-test-actor": "create-route"},
    )
    assert created_route.status_code == 200
    assert service.calls[-1] == (
        "create_route",
        "p",
        "create-route",
        {"name": "Watch Slack", "channel_id": 8},
    )

    patched_route = client.patch(
        "/api/projects/p/notification-routes/4",
        json={"enabled": False},
        headers={"x-test-actor": "patch-route"},
    )
    assert patched_route.status_code == 200
    assert service.calls[-1] == (
        "patch_route",
        "p",
        "patch-route",
        4,
        {"enabled": False},
    )

    missing_route = client.patch(
        "/api/projects/p/notification-routes/404",
        json={"enabled": False},
    )
    assert missing_route.status_code == 404
    assert missing_route.json()["detail"] == "notification route not found"

    tested_route = client.post(
        "/api/projects/p/notification-routes/4/test",
        headers={"x-test-actor": "test-route"},
    )
    assert tested_route.status_code == 200
    assert service.calls[-1] == ("test_route", "p", "test-route", 4)

    request_page = client.get(
        "/api/projects/p/notification-delivery-requests",
        params={
            "status": "queued",
            "route_id": 4,
            "channel_id": 8,
            "notification_id": 9,
            "offset": 1,
            "limit": 2,
        },
    )
    assert request_page.status_code == 200
    assert service.calls[-1] == (
        "list_delivery_requests",
        "p",
        None,
        "queued",
        4,
        8,
        9,
        1,
        2,
    )

    delivered = client.post(
        "/api/projects/p/notifications/9/deliver",
        json={"channel_id": 8},
    )
    assert delivered.status_code == 200
    assert service.calls[-1] == ("deliver", "p", 9, {"channel_id": 8})

    bad_delivery = client.post("/api/projects/p/notifications/9/deliver", json={})
    assert bad_delivery.status_code == 400
    assert bad_delivery.json()["detail"] == "channel_id is required"


def _actor_state_payload(notification_id: int, *, state: str) -> dict[str, Any]:
    """The frozen notification actor-state wire shape."""

    return {
        "notification_id": notification_id,
        "actor_id": "local:project",
        "state": state,
        "seen_at": "2026-08-11 00:00:00",
        "read_at": "2026-08-11 00:00:00" if state != "unseen" else None,
        "acknowledged_at": ("2026-08-11 00:00:00" if state == "acknowledged" else None),
        "acknowledged_by": "local:project" if state == "acknowledged" else None,
        "updated_at": "2026-08-11 00:00:00",
    }


def _channel_payload(channel_id: int) -> dict[str, Any]:
    return {
        "id": channel_id,
        "kind": "email",
        "name": "Ops",
        "enabled": True,
        "owner_kind": "project",
        "config": {},
        "has_secret": False,
        "created_at": "2026-08-11 00:00:00",
        "updated_at": "2026-08-11 00:00:00",
    }


def _route_payload(route_id: int) -> dict[str, Any]:
    return {
        "id": route_id,
        "name": "Watch Slack",
        "enabled": True,
        "owner_kind": "project",
        "owner_ref": "p",
        "recipient_actor_id": None,
        "channel_id": 8,
        "source_kind": None,
        "source_ref_match": {},
        "event_kinds": [],
        "severity_min": "info",
        "delivery_mode": "immediate",
        "digest_cadence": None,
        "digest_timezone": "UTC",
        "digest_anchor_time": None,
        "template_key": None,
        "created_at": "2026-08-11 00:00:00",
        "updated_at": "2026-08-11 00:00:00",
    }


def _delivery_request_payload(request_id: int, *, route_id: int) -> dict[str, Any]:
    return {
        "id": request_id,
        "route_id": route_id,
        "channel_id": 8,
        "notification_id": None,
        "digest_run_id": None,
        "delivery_kind": "test",
        "dedupe_key": f"notification:test:route:{route_id}:channel:8:fixture",
        "status": "queued",
        "job_id": None,
        "available_at": "2026-08-11 00:00:00",
        "last_error": None,
        "provider_ref": None,
        "created_at": "2026-08-11 00:00:00",
        "updated_at": "2026-08-11 00:00:00",
        "sent_at": None,
    }


class _FakeNotificationService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_notifications(
        self,
        project_id: str,
        request: Any,
        *,
        state: str,
        source_kind: str | None,
        source_ref: str | None,
        severity: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        if state == "bad":
            raise NotificationRequestError("unsupported notification state")
        self.calls.append(
            (
                "list_notifications",
                project_id,
                request.headers.get("x-test-actor"),
                state,
                source_kind,
                source_ref,
                severity,
                offset,
                limit,
            )
        )
        return {
            "schema_version": "frisket.notifications_page.v1",
            "order": "desc",
            "offset": offset,
            "limit": limit,
            "total": 0,
            "has_more": False,
            "next_offset": None,
            "notifications": [],
        }

    def summary(self, project_id: str, request: Any) -> dict:
        self.calls.append(("summary", project_id, request.headers.get("x-test-actor")))
        return {
            "schema_version": "frisket.notifications_summary.v1",
            "total": 0,
            "unseen": 0,
            "seen": 0,
            "read": 0,
            "acknowledged": 0,
            "by_severity": {},
            "by_source_kind": {},
            "by_source_ref": [],
        }

    def mark_seen(self, project_id: str, request: Any, body: dict[str, Any]) -> dict:
        self.calls.append(
            ("mark_seen", project_id, request.headers.get("x-test-actor"), body)
        )
        return {"seen_count": 1}

    def bulk_ack(self, project_id: str, request: Any, body: dict[str, Any]) -> dict:
        self.calls.append(
            ("bulk_ack", project_id, request.headers.get("x-test-actor"), body)
        )
        return {"acknowledged_count": 1}

    def emit(self, project_id: str, request: Any, body: dict[str, Any]) -> dict:
        self.calls.append(
            ("emit", project_id, request.headers.get("x-test-actor"), body)
        )
        return {"notification_id": 1}

    def mark_read(self, project_id: str, request: Any, notification_id: int) -> dict:
        if notification_id == 404:
            raise NotificationNotFound("notification not found")
        self.calls.append(
            (
                "mark_read",
                project_id,
                request.headers.get("x-test-actor"),
                notification_id,
            )
        )
        return _actor_state_payload(notification_id, state="read")

    def ack(self, project_id: str, request: Any, notification_id: int) -> dict:
        self.calls.append(
            (
                "ack",
                project_id,
                request.headers.get("x-test-actor"),
                notification_id,
            )
        )
        return _actor_state_payload(notification_id, state="acknowledged")

    def unack(self, project_id: str, request: Any, notification_id: int) -> dict:
        self.calls.append(
            (
                "unack",
                project_id,
                request.headers.get("x-test-actor"),
                notification_id,
            )
        )
        return _actor_state_payload(notification_id, state="read")

    def list_channels(self, project_id: str, request: Any) -> dict:
        self.calls.append(
            ("list_channels", project_id, request.headers.get("x-test-actor"))
        )
        return {"schema_version": "frisket.notification_channels.v1", "channels": []}

    def create_channel(
        self, project_id: str, request: Any, body: dict[str, Any]
    ) -> dict:
        self.calls.append(
            ("create_channel", project_id, request.headers.get("x-test-actor"), body)
        )
        return _channel_payload(1)

    def patch_channel(
        self, project_id: str, request: Any, channel_id: int, body: dict[str, Any]
    ) -> dict:
        if channel_id == 404:
            raise NotificationChannelNotFound("notification channel not found")
        self.calls.append(
            (
                "patch_channel",
                project_id,
                request.headers.get("x-test-actor"),
                channel_id,
                body,
            )
        )
        return _channel_payload(channel_id)

    def list_routes(self, project_id: str, request: Any) -> dict:
        self.calls.append(
            ("list_routes", project_id, request.headers.get("x-test-actor"))
        )
        return {"schema_version": "frisket.notification_routes.v1", "routes": []}

    def create_route(self, project_id: str, request: Any, body: dict[str, Any]) -> dict:
        self.calls.append(
            ("create_route", project_id, request.headers.get("x-test-actor"), body)
        )
        return _route_payload(1)

    def patch_route(
        self, project_id: str, request: Any, route_id: int, body: dict[str, Any]
    ) -> dict:
        if route_id == 404:
            raise NotificationRouteNotFound("notification route not found")
        self.calls.append(
            (
                "patch_route",
                project_id,
                request.headers.get("x-test-actor"),
                route_id,
                body,
            )
        )
        return _route_payload(route_id)

    def test_route(self, project_id: str, request: Any, route_id: int) -> dict:
        self.calls.append(
            ("test_route", project_id, request.headers.get("x-test-actor"), route_id)
        )
        return _delivery_request_payload(11, route_id=route_id)

    def list_delivery_requests(
        self,
        project_id: str,
        request: Any,
        *,
        status: str | None,
        route_id: int | None,
        channel_id: int | None,
        notification_id: int | None,
        offset: int,
        limit: int,
    ) -> dict:
        self.calls.append(
            (
                "list_delivery_requests",
                project_id,
                request.headers.get("x-test-actor"),
                status,
                route_id,
                channel_id,
                notification_id,
                offset,
                limit,
            )
        )
        return {
            "schema_version": "frisket.notification_delivery_requests.v1",
            "order": "desc",
            "offset": offset,
            "limit": limit,
            "total": 0,
            "has_more": False,
            "next_offset": None,
            "delivery_requests": [],
        }

    def deliver(
        self, project_id: str, notification_id: int, body: dict[str, Any]
    ) -> dict:
        if "channel_id" not in body:
            raise NotificationRequestError("channel_id is required")
        self.calls.append(("deliver", project_id, notification_id, body))
        return {"notification_id": notification_id, "channel_id": body["channel_id"]}
