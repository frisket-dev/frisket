"""Notification services for local server routes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from frisket.engine.jobs import (
    enqueue_notification_delivery,
    enqueue_notification_emit_result,
)
from frisket.server.notifications.service import (
    LOCAL_ACTOR_ID,
    NOTIFICATION_CHANNELS_SCHEMA_VERSION,
    NOTIFICATION_DELIVERY_REQUESTS_SCHEMA_VERSION,
    NOTIFICATION_PAGE_SCHEMA_VERSION,
    NOTIFICATION_ROUTES_SCHEMA_VERSION,
    NOTIFICATION_STATES,
    NOTIFICATION_SUMMARY_SCHEMA_VERSION,
    DELIVERY_STATUSES,
    create_notification_test_request,
    emit_notification_candidate,
    normalize_channel_input,
    normalize_filter_payload,
    normalize_route_input,
    parse_source_ref,
    public_actor_state,
    public_notification_item,
)
from frisket.server.paging import offset_page_meta
from frisket.server.workspace import Workspace


class NotificationNotFound(ValueError):
    pass


class NotificationChannelNotFound(ValueError):
    pass


class NotificationRouteNotFound(ValueError):
    pass


class NotificationRequestError(ValueError):
    def __init__(self, detail: str, *, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class NotificationService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

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
    ) -> dict[str, Any]:
        if state not in NOTIFICATION_STATES:
            raise NotificationRequestError("unsupported notification state")
        project = self._workspace.get(project_id)
        context = _notification_actor_context(request)
        visibility = _notification_delivery_visibility(project, context)
        try:
            parsed_source_ref = parse_source_ref(source_ref)
        except ValueError as exc:
            raise NotificationRequestError(str(exc)) from exc
        rows = project.list_notification_items(
            actor_id=context.actor_id,
            state=state,
            source_kind=source_kind,
            source_ref=parsed_source_ref,
            severity=severity,
            visible_delivery_route_ids=visibility.route_ids,
            visible_delivery_channel_ids=visibility.channel_ids,
            offset=offset,
            limit=limit,
        )
        total = project.notification_items_total(
            actor_id=context.actor_id,
            state=state,
            source_kind=source_kind,
            source_ref=parsed_source_ref,
            severity=severity,
            visible_delivery_route_ids=visibility.route_ids,
            visible_delivery_channel_ids=visibility.channel_ids,
        )
        return {
            **offset_page_meta(
                schema_version=NOTIFICATION_PAGE_SCHEMA_VERSION,
                order="desc",
                offset=offset,
                limit=limit,
                total=total,
                item_count=len(rows),
            ),
            "notifications": [public_notification_item(row) for row in rows],
        }

    def summary(self, project_id: str, request: Any) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        context = _notification_actor_context(request)
        visibility = _notification_delivery_visibility(project, context)
        summary = project.notification_summary(
            actor_id=context.actor_id,
            visible_delivery_route_ids=visibility.route_ids,
            visible_delivery_channel_ids=visibility.channel_ids,
        )
        return {
            "schema_version": NOTIFICATION_SUMMARY_SCHEMA_VERSION,
            **summary,
        }

    def mark_seen(self, project_id: str, request: Any, body: dict[str, Any]) -> dict:
        project = self._workspace.get(project_id)
        filters = self._normalize_filter_body(body)
        count = project.mark_notifications_seen(
            actor_id=_notification_actor_id(request),
            **filters,
        )
        return {"seen_count": count}

    def bulk_ack(self, project_id: str, request: Any, body: dict[str, Any]) -> dict:
        project = self._workspace.get(project_id)
        filters = self._normalize_filter_body(body)
        count = project.acknowledge_notifications(
            actor_id=_notification_actor_id(request),
            **filters,
        )
        return {"acknowledged_count": count}

    def emit(
        self, project_id: str, request: Any, body: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        try:
            result = emit_notification_candidate(
                project,
                body,
                actor_id=_notification_actor_id(request),
            )
            enqueue_notification_emit_result(
                self._workspace.queue,
                workspace_root=self._workspace.root,
                project_id=project_id,
                storage_org_id=self._workspace.queue_storage_org_id,
                result=result,
                project_opener=self._workspace.project_opener,
            )
            return result
        except ValueError as exc:
            raise NotificationRequestError(str(exc)) from exc

    def mark_read(
        self, project_id: str, request: Any, notification_id: int
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        self._ensure_notification(project, notification_id)
        state = project.mark_notification_read(
            notification_id, _notification_actor_id(request)
        )
        return public_actor_state(state)

    def ack(
        self, project_id: str, request: Any, notification_id: int
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        self._ensure_notification(project, notification_id)
        state = project.acknowledge_notification(
            notification_id, _notification_actor_id(request)
        )
        return public_actor_state(state)

    def unack(
        self, project_id: str, request: Any, notification_id: int
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        self._ensure_notification(project, notification_id)
        state = project.unacknowledge_notification(
            notification_id, _notification_actor_id(request)
        )
        return public_actor_state(state)

    def list_channels(
        self, project_id: str, request: Any | None = None
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        context = _notification_actor_context(request)
        project.ensure_default_in_app_notification_channel()
        return {
            "schema_version": NOTIFICATION_CHANNELS_SCHEMA_VERSION,
            "channels": [
                project.public_notification_channel(int(row["id"]))
                for row in project.notification_channels()
                if _channel_visible_to_actor(row, context)
            ],
        }

    def create_channel(
        self, project_id: str, request: Any | None, body: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        context = _notification_actor_context(request)
        try:
            data = normalize_channel_input(body)
            data["config"] = _channel_config_for_actor(
                data["config"],
                body,
                context,
            )
            return project.create_notification_channel(**data)
        except NotificationRequestError:
            raise
        except ValueError as exc:
            raise NotificationRequestError(str(exc)) from exc

    def patch_channel(
        self,
        project_id: str,
        request: Any | None,
        channel_id: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.notification_channel(channel_id)
        if row is None:
            raise NotificationChannelNotFound("notification channel not found")
        context = _notification_actor_context(request)
        owner_kind, owner_ref = _channel_owner(row)
        requested_owner_kind = _requested_owner_kind(body)
        if requested_owner_kind is not None and requested_owner_kind != owner_kind:
            raise NotificationRequestError(
                "notification channel owner cannot be changed",
                status_code=403,
            )
        if owner_kind == "user":
            _require_owned_session_actor(
                context,
                owner_ref,
                "user notification channel",
            )
        try:
            normalized = normalize_channel_input(body, existing_kind=str(row["kind"]))
            set_secret = (
                "secret_ref" in body
                or "webhook_secret_ref" in body
                or "webhook_url_secret_ref" in body
                or "url_secret_ref" in body
            )
            config_fields = {
                "audience",
                "channel_label",
                "max_redirects",
                "max_request_bytes",
                "max_response_bytes",
                "provider",
                "signature_header",
                "signature_secret_ref",
                "signing_secret_ref",
                "to",
                "webhook_host",
                "webhook_signing_secret_ref",
                "webhook_url",
            }
            next_config = normalized["config"]
            if owner_kind == "user":
                next_config = _with_channel_owner(next_config, owner_ref)
            return project.update_notification_channel(
                channel_id,
                name=body.get("name"),
                enabled=body.get("enabled") if "enabled" in body else None,
                config=(
                    next_config if any(key in body for key in config_fields) else None
                ),
                secret_ref=normalized["secret_ref"],
                set_secret_ref=set_secret,
            )
        except ValueError as exc:
            raise NotificationRequestError(str(exc)) from exc

    def list_routes(
        self, project_id: str, request: Any | None = None
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        context = _notification_actor_context(request)
        return {
            "schema_version": NOTIFICATION_ROUTES_SCHEMA_VERSION,
            "routes": [
                project.public_notification_route(int(row["id"]))
                for row in project.notification_routes()
                if _route_visible_to_actor(row, context)
            ],
        }

    def create_route(
        self, project_id: str, request: Any | None, body: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        context = _notification_actor_context(request)
        try:
            data = normalize_route_input(body, project_id=project_id)
            self._apply_route_owner_policy(
                project,
                data,
                body,
                context=context,
            )
            return project.create_notification_route(**data)
        except NotificationRequestError:
            raise
        except ValueError as exc:
            raise NotificationRequestError(str(exc)) from exc

    def patch_route(
        self,
        project_id: str,
        request: Any | None,
        route_id: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.notification_route(route_id)
        if row is None:
            raise NotificationRouteNotFound("notification route not found")
        context = _notification_actor_context(request)
        if str(row["owner_kind"]) == "user":
            _require_owned_session_actor(
                context,
                row["owner_ref"],
                "user notification route",
            )
        try:
            data = normalize_route_input(body, project_id=project_id, existing=row)
            if str(row["owner_kind"]) != data["owner_kind"]:
                raise ValueError("notification route owner cannot be changed")
            self._apply_route_owner_policy(
                project,
                data,
                body,
                context=context,
                existing=row,
            )
            return project.update_notification_route(
                route_id,
                name=data["name"],
                enabled=data["enabled"],
                owner_kind=data["owner_kind"],
                owner_ref=data["owner_ref"],
                recipient_actor_id=data["recipient_actor_id"],
                set_recipient_actor_id=(
                    "recipient_actor_id" in body
                    or row["recipient_actor_id"] is not None
                ),
                channel_id=data["channel_id"],
                source_kind=data["source_kind"],
                set_source_kind=(
                    "source_kind" in body or row["source_kind"] is not None
                ),
                source_ref_match=data["source_ref_match"],
                event_kinds=data["event_kinds"],
                severity_min=data["severity_min"],
                delivery_mode=data["delivery_mode"],
                digest_cadence=data["digest_cadence"],
                set_digest_cadence=(
                    "digest_cadence" in body or row["digest_cadence"] is not None
                ),
                digest_timezone=data["digest_timezone"],
                digest_anchor_time=data["digest_anchor_time"],
                set_digest_anchor_time=(
                    "digest_anchor_time" in body
                    or row["digest_anchor_time"] is not None
                ),
                template_key=data["template_key"],
                set_template_key=(
                    "template_key" in body or row["template_key"] is not None
                ),
            )
        except NotificationRequestError:
            raise
        except ValueError as exc:
            raise NotificationRequestError(str(exc)) from exc

    def test_route(
        self, project_id: str, request: Any | None, route_id: int
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.notification_route(route_id)
        if row is None:
            raise NotificationRouteNotFound("notification route not found")
        context = _notification_actor_context(request)
        if str(row["owner_kind"]) == "user":
            _require_owned_session_actor(
                context,
                row["owner_ref"],
                "user notification route",
            )
        try:
            created = create_notification_test_request(project, route_id)
            enqueue_notification_delivery(
                self._workspace.queue,
                workspace_root=self._workspace.root,
                project_id=project_id,
                storage_org_id=self._workspace.queue_storage_org_id,
                request_id=int(created["id"]),
                project_opener=self._workspace.project_opener,
            )
            return project.public_notification_delivery_request(int(created["id"]))
        except ValueError as exc:
            message = str(exc)
            if "not found" in message:
                raise NotificationRouteNotFound("notification route not found") from exc
            raise NotificationRequestError(message) from exc

    def list_delivery_requests(
        self,
        project_id: str,
        request: Any | None,
        *,
        status: str | None,
        route_id: int | None,
        channel_id: int | None,
        notification_id: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        if status is not None and status not in DELIVERY_STATUSES:
            raise NotificationRequestError("unsupported notification delivery status")
        project = self._workspace.get(project_id)
        visibility = _notification_delivery_visibility(
            project,
            _notification_actor_context(request),
        )
        rows = project.list_notification_delivery_requests(
            status=status,
            route_id=route_id,
            channel_id=channel_id,
            notification_id=notification_id,
            visible_route_ids=visibility.route_ids,
            visible_channel_ids=visibility.channel_ids,
            offset=offset,
            limit=limit,
        )
        total = project.notification_delivery_requests_total(
            status=status,
            route_id=route_id,
            channel_id=channel_id,
            notification_id=notification_id,
            visible_route_ids=visibility.route_ids,
            visible_channel_ids=visibility.channel_ids,
        )
        return {
            **offset_page_meta(
                schema_version=NOTIFICATION_DELIVERY_REQUESTS_SCHEMA_VERSION,
                order="desc",
                offset=offset,
                limit=limit,
                total=total,
                item_count=len(rows),
            ),
            "delivery_requests": [
                project.public_notification_delivery_request(int(row["id"]))
                for row in rows
            ],
        }

    def deliver(
        self, project_id: str, notification_id: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        self._ensure_notification(project, notification_id)
        raise NotificationRequestError(
            "direct notification delivery is disabled; use notification routes"
        )

    def _normalize_filter_body(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return normalize_filter_payload(body)
        except (TypeError, ValueError) as exc:
            raise NotificationRequestError(str(exc)) from exc

    def _ensure_notification(self, project: Any, notification_id: int) -> None:
        if project.notification_item(notification_id) is None:
            raise NotificationNotFound("notification not found")

    def _apply_route_owner_policy(
        self,
        project: Any,
        data: dict[str, Any],
        body: dict[str, Any],
        *,
        context: "_NotificationActorContext",
        existing: Any | None = None,
    ) -> None:
        if data["owner_kind"] == "user":
            _require_owned_session_actor(
                context,
                str(existing["owner_ref"])
                if existing is not None
                else context.actor_id,
                "user notification route",
            )
            if "owner_ref" in body and str(body["owner_ref"]) != context.actor_id:
                raise NotificationRequestError(
                    "user notification route owner_ref must match actor",
                    status_code=403,
                )
            if (
                "recipient_actor_id" in body
                and str(body["recipient_actor_id"]) != context.actor_id
            ):
                raise NotificationRequestError(
                    "user notification route recipient_actor_id must match actor",
                    status_code=403,
                )
            data["owner_ref"] = context.actor_id
            data["recipient_actor_id"] = context.actor_id
        self._ensure_external_channel_for_route(
            project,
            data["channel_id"],
            actor_context=context,
            route_owner_kind=data["owner_kind"],
        )

    def _ensure_external_channel_for_route(
        self,
        project: Any,
        channel_id: int,
        *,
        actor_context: "_NotificationActorContext",
        route_owner_kind: str,
    ) -> None:
        channel = project.notification_channel(channel_id)
        if channel is None:
            raise ValueError("notification channel not found")
        if str(channel["kind"]) == "in_app":
            raise ValueError("in-app notification route is implicit")
        channel_owner_kind, channel_owner_ref = _channel_owner(channel)
        if route_owner_kind == "user":
            if (
                channel_owner_kind != "user"
                or channel_owner_ref != actor_context.actor_id
            ):
                raise ValueError(
                    "user notification routes require an owned user channel"
                )
        elif channel_owner_kind == "user":
            raise ValueError(
                "project notification routes cannot use user-owned channels"
            )


@dataclass(frozen=True)
class _NotificationActorContext:
    actor_id: str
    auth: str


@dataclass(frozen=True)
class _NotificationVisibility:
    route_ids: set[int] | None
    channel_ids: set[int] | None


def _notification_actor_id(request: Any) -> str:
    return _notification_actor_context(request).actor_id


def _notification_actor_context(request: Any | None) -> _NotificationActorContext:
    if request is None:
        return _NotificationActorContext(actor_id=LOCAL_ACTOR_ID, auth="local")
    state = getattr(request, "state", None)
    user = getattr(state, "user", None)
    if isinstance(user, dict) and user.get("id") is not None:
        auth = "pat" if user.get("auth") == "pat" else "session"
        return _NotificationActorContext(actor_id=f"user:{int(user['id'])}", auth=auth)
    return _NotificationActorContext(actor_id=LOCAL_ACTOR_ID, auth="local")


def _requested_owner_kind(body: dict[str, Any]) -> str | None:
    if "owner_kind" not in body:
        return None
    return str(body["owner_kind"]).strip().lower()


def _channel_config_for_actor(
    config: dict[str, Any],
    body: dict[str, Any],
    context: _NotificationActorContext,
) -> dict[str, Any]:
    owner_kind = _requested_owner_kind(body)
    if owner_kind is None or owner_kind == "project":
        return _without_channel_owner(config)
    if owner_kind != "user":
        raise ValueError("unsupported notification channel owner_kind")
    _require_owned_session_actor(context, context.actor_id, "user notification channel")
    return _with_channel_owner(config, context.actor_id)


def _with_channel_owner(
    config: dict[str, Any], owner_ref: str | None
) -> dict[str, Any]:
    next_config = dict(config)
    if owner_ref:
        next_config["owner_kind"] = "user"
        next_config["owner_ref"] = owner_ref
    return next_config


def _without_channel_owner(config: dict[str, Any]) -> dict[str, Any]:
    next_config = dict(config)
    next_config.pop("owner_kind", None)
    next_config.pop("owner_ref", None)
    return next_config


def _channel_owner(channel: Any) -> tuple[str, str | None]:
    config = _channel_config(channel)
    owner_kind = str(config.get("owner_kind") or "project")
    owner_ref = config.get("owner_ref")
    if owner_ref is not None:
        owner_ref = str(owner_ref)
    return owner_kind, owner_ref


def _channel_config(channel: Any) -> dict[str, Any]:
    try:
        config = json.loads(channel["config_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _channel_visible_to_actor(
    channel: Any,
    context: _NotificationActorContext,
) -> bool:
    owner_kind, owner_ref = _channel_owner(channel)
    if owner_kind != "user":
        return True
    if context.actor_id == LOCAL_ACTOR_ID:
        return True
    return owner_ref == context.actor_id


def _route_visible_to_actor(
    route: Any,
    context: _NotificationActorContext,
) -> bool:
    if str(route["owner_kind"]) != "user":
        return True
    if context.actor_id == LOCAL_ACTOR_ID:
        return True
    return str(route["owner_ref"]) == context.actor_id


def _notification_delivery_visibility(
    project: Any,
    context: _NotificationActorContext,
) -> _NotificationVisibility:
    if context.actor_id == LOCAL_ACTOR_ID:
        return _NotificationVisibility(route_ids=None, channel_ids=None)
    return _NotificationVisibility(
        route_ids={
            int(route["id"])
            for route in project.notification_routes()
            if _route_visible_to_actor(route, context)
        },
        channel_ids={
            int(channel["id"])
            for channel in project.notification_channels()
            if _channel_visible_to_actor(channel, context)
        },
    )


def _require_owned_session_actor(
    context: _NotificationActorContext,
    owner_ref: Any,
    resource_name: str,
) -> None:
    if context.auth == "pat" or not context.actor_id.startswith("user:"):
        raise NotificationRequestError(
            f"{resource_name} requires a hosted browser session",
            status_code=403,
        )
    if str(owner_ref) != context.actor_id:
        raise NotificationRequestError(
            f"{resource_name} belongs to another user",
            status_code=403,
        )
