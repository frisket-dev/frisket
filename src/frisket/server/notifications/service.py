"""Notification channel, route, and feed shaping for the local server.

Candidate emission and route planning live in ``frisket.notifications.emit``;
this module normalizes channel/route input and shapes public feed payloads.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from frisket.contracts.http.notifications import (
    NotificationActorState,
    NotificationItem,
)
from frisket.notifications.emit import (
    NOTIFICATION_SEVERITIES,
    _canonical_object,
    _loads_list,
    _loads_object,
)
from frisket.engine.store import Project
from frisket.engine.store.notifications import (
    require_notification_channel,
    require_notification_route,
)

NOTIFICATION_CHANNELS_SCHEMA_VERSION: Final = "frisket.notification_channels.v1"
NOTIFICATION_ROUTES_SCHEMA_VERSION: Final = "frisket.notification_routes.v1"
NOTIFICATION_DELIVERY_REQUESTS_SCHEMA_VERSION: Final = (
    "frisket.notification_delivery_requests.v1"
)

NOTIFICATION_STATES = {"unseen", "seen", "read", "acknowledged", "all"}
NOTIFICATION_CHANNEL_KINDS = {"in_app", "email", "slack", "webhook"}
NOTIFICATION_ROUTE_OWNER_KINDS = {"project", "system", "user"}
NOTIFICATION_ROUTE_DELIVERY_MODES = {"immediate", "digest"}
NOTIFICATION_DIGEST_CADENCES = {"hourly", "daily", "manual"}
DELIVERY_STATUSES = {
    "queued",
    "processing",
    "sent",
    "failed",
    "skipped",
    "cancelled",
    "reconciliation_required",
}


def create_notification_test_request(
    project: Project,
    route_id: int,
) -> dict[str, Any]:
    route = require_notification_route(project, route_id)
    channel = require_notification_channel(project, int(route["channel_id"]))
    if str(channel["kind"]) == "in_app":
        raise ValueError("in-app notification route test is implicit")
    return project.create_notification_delivery_request(
        route_id=int(route["id"]),
        channel_id=int(route["channel_id"]),
        delivery_kind="test",
        dedupe_key=(
            f"notification:test:route:{int(route['id'])}:"
            f"channel:{int(route['channel_id'])}:{uuid.uuid4().hex}"
        ),
    )


def public_notification_item(row: Any) -> NotificationItem:
    return NotificationItem(
        id=int(row["id"]),
        source_kind=row["source_kind"],
        source_ref=_loads_object(row["source_ref"]),
        source_event_ids=_loads_list(row["source_event_ids"]),
        event_count=int(row["event_count"]),
        event_kinds=_loads_list(row["event_kinds"]),
        title=row["title"],
        summary=row["summary"],
        severity=row["severity"],
        deep_link=_loads_object(row["deep_link"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        state=row["actor_state"],
        seen_at=row["seen_at"],
        read_at=row["read_at"],
        acknowledged_at=row["acknowledged_at"],
        acknowledged_by=row["acknowledged_by"],
    )


def public_actor_state(state: dict[str, Any]) -> NotificationActorState:
    return NotificationActorState(
        notification_id=int(state["notification_id"]),
        actor_id=state["actor_id"],
        state=state["state"],
        seen_at=state["seen_at"],
        read_at=state["read_at"],
        acknowledged_at=state["acknowledged_at"],
        acknowledged_by=state["acknowledged_by"],
        updated_at=state["updated_at"],
    )


def normalize_channel_input(
    data: dict[str, Any], *, existing_kind: str | None = None
) -> dict[str, Any]:
    kind = str(existing_kind or data.get("kind") or "").strip().lower()
    if kind not in NOTIFICATION_CHANNEL_KINDS:
        raise ValueError("unsupported notification channel kind")
    name = str(data.get("name") or kind.replace("_", " ").title()).strip()
    if not name:
        raise ValueError("notification channel name is required")
    enabled = bool(data.get("enabled", True))
    secret_ref = _channel_secret_ref(kind, data)
    config = _channel_config(kind, data)
    return {
        "kind": kind,
        "name": name,
        "enabled": enabled,
        "config": config,
        "secret_ref": secret_ref,
    }


def normalize_route_input(
    data: dict[str, Any],
    *,
    project_id: str,
    existing: Any | None = None,
) -> dict[str, Any]:
    existing_values = _existing_route_values(existing)
    name = str(data.get("name", existing_values.get("name") or "")).strip()
    if not name:
        raise ValueError("notification route name is required")
    channel_id = data.get("channel_id", existing_values.get("channel_id"))
    if channel_id is None:
        raise ValueError("channel_id is required")
    try:
        normalized_channel_id = int(channel_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("channel_id is required") from exc
    enabled = bool(data.get("enabled", existing_values.get("enabled", True)))
    owner_kind = str(
        data.get("owner_kind", existing_values.get("owner_kind") or "project")
    )
    if owner_kind not in NOTIFICATION_ROUTE_OWNER_KINDS:
        raise ValueError("unsupported notification route owner_kind")
    owner_ref = str(
        data.get("owner_ref", existing_values.get("owner_ref") or project_id)
    ).strip()
    if not owner_ref:
        raise ValueError("notification route owner_ref is required")
    recipient_actor_id = data.get(
        "recipient_actor_id", existing_values.get("recipient_actor_id")
    )
    if recipient_actor_id is not None:
        recipient_actor_id = str(recipient_actor_id).strip() or None
    source_kind = data.get("source_kind", existing_values.get("source_kind"))
    if source_kind is not None:
        source_kind = str(source_kind).strip() or None
    source_ref_match = data.get(
        "source_ref_match",
        data.get(
            "source_ref_match_json", existing_values.get("source_ref_match") or {}
        ),
    )
    if source_ref_match is None:
        source_ref_match = {}
    if not isinstance(source_ref_match, dict):
        raise ValueError("source_ref_match must be an object")
    event_kinds = data.get("event_kinds", existing_values.get("event_kinds") or [])
    if event_kinds is None:
        event_kinds = []
    if not isinstance(event_kinds, list):
        raise ValueError("event_kinds must be an array")
    normalized_event_kinds = sorted(
        {str(value).strip() for value in event_kinds if str(value).strip()}
    )
    severity_min = str(
        data.get("severity_min", existing_values.get("severity_min") or "info")
    )
    if severity_min not in NOTIFICATION_SEVERITIES:
        raise ValueError("unsupported notification route severity_min")
    delivery_mode = str(
        data.get("delivery_mode", existing_values.get("delivery_mode") or "immediate")
    )
    if delivery_mode not in NOTIFICATION_ROUTE_DELIVERY_MODES:
        raise ValueError("unsupported notification route delivery_mode")
    digest_cadence = data.get("digest_cadence", existing_values.get("digest_cadence"))
    if digest_cadence is not None:
        digest_cadence = str(digest_cadence)
        if digest_cadence not in NOTIFICATION_DIGEST_CADENCES:
            raise ValueError("unsupported notification digest cadence")
    digest_timezone = str(
        data.get("digest_timezone", existing_values.get("digest_timezone") or "UTC")
    )
    if digest_timezone != "UTC":
        raise ValueError("notification digest timezone must be UTC")
    digest_anchor_time = data.get(
        "digest_anchor_time", existing_values.get("digest_anchor_time")
    )
    if digest_anchor_time is not None:
        digest_anchor_time = str(digest_anchor_time).strip() or None
    template_key = data.get("template_key", existing_values.get("template_key"))
    if template_key is not None:
        template_key = str(template_key).strip() or None
    return {
        "name": name,
        "enabled": enabled,
        "owner_kind": owner_kind,
        "owner_ref": owner_ref,
        "recipient_actor_id": recipient_actor_id,
        "channel_id": normalized_channel_id,
        "source_kind": source_kind,
        "source_ref_match": _canonical_object(source_ref_match),
        "event_kinds": normalized_event_kinds,
        "severity_min": severity_min,
        "delivery_mode": delivery_mode,
        "digest_cadence": digest_cadence,
        "digest_timezone": digest_timezone,
        "digest_anchor_time": digest_anchor_time,
        "template_key": template_key,
    }


def parse_source_ref(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("source_ref must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("source_ref must be a JSON object")
    return value


def normalize_filter_payload(data: dict[str, Any]) -> dict[str, Any]:
    ids = data.get("notification_ids")
    notification_ids = None
    if ids is not None:
        if not isinstance(ids, list):
            raise ValueError("notification_ids must be an array")
        notification_ids = [int(value) for value in ids]
    source_ref = data.get("source_ref")
    if source_ref is not None and not isinstance(source_ref, dict):
        raise ValueError("source_ref must be an object")
    return {
        "notification_ids": notification_ids,
        "source_kind": data.get("source_kind"),
        "source_ref": source_ref,
        "before_created_at": data.get("before_created_at"),
    }


def _existing_route_values(row: Any | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "owner_kind": row["owner_kind"],
        "owner_ref": row["owner_ref"],
        "recipient_actor_id": row["recipient_actor_id"],
        "channel_id": int(row["channel_id"]),
        "source_kind": row["source_kind"],
        "source_ref_match": _loads_object(row["source_ref_match_json"]),
        "event_kinds": _loads_list(row["event_kinds_json"]),
        "severity_min": row["severity_min"],
        "delivery_mode": row["delivery_mode"],
        "digest_cadence": row["digest_cadence"],
        "digest_timezone": row["digest_timezone"],
        "digest_anchor_time": row["digest_anchor_time"],
        "template_key": row["template_key"],
    }


def _channel_secret_ref(kind: str, data: dict[str, Any]) -> str | None:
    if kind == "slack":
        value = data.get("webhook_secret_ref") or data.get("secret_ref")
    elif kind == "webhook":
        value = (
            data.get("webhook_url_secret_ref")
            or data.get("url_secret_ref")
            or data.get("webhook_secret_ref")
            or data.get("secret_ref")
        )
    else:
        value = data.get("secret_ref")
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _channel_config(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind == "in_app":
        return {"audience": str(data.get("audience") or "project")}
    if kind == "email":
        config: dict[str, Any] = {}
        if data.get("to"):
            to = str(data["to"]).strip()
            config["to"] = to
            if "@" in to:
                config["to_domain"] = to.rsplit("@", 1)[1]
        from_address = data.get("from") or data.get("from_address")
        if from_address:
            config["from"] = str(from_address).strip()
        if data.get("provider"):
            config["provider"] = str(data["provider"]).strip()
        return config
    if kind == "slack":
        config = {}
        if data.get("channel_label"):
            config["channel_label"] = str(data["channel_label"]).strip()
        webhook_url = str(data.get("webhook_url") or "").strip()
        if webhook_url.startswith("https://"):
            host = webhook_url.removeprefix("https://").split("/", 1)[0]
            if host:
                config["webhook_host"] = host
        elif data.get("webhook_host"):
            config["webhook_host"] = str(data["webhook_host"]).strip()
        return config
    if kind == "webhook":
        config = {}
        webhook_url = str(data.get("webhook_url") or "").strip()
        if webhook_url.startswith("https://"):
            host = webhook_url.removeprefix("https://").split("/", 1)[0]
            if host:
                config["webhook_host"] = host
        elif data.get("webhook_host"):
            config["webhook_host"] = str(data["webhook_host"]).strip()
        signing_ref = (
            data.get("signing_secret_ref")
            or data.get("webhook_signing_secret_ref")
            or data.get("signature_secret_ref")
        )
        if signing_ref:
            config["signing_secret_ref"] = str(signing_ref).strip()
        signature_header = data.get("signature_header")
        if signature_header:
            config["signature_header"] = str(signature_header).strip()
        max_response_bytes = data.get("max_response_bytes")
        if max_response_bytes is not None:
            config["max_response_bytes"] = _bounded_positive_int(
                max_response_bytes,
                field="max_response_bytes",
                default=64 * 1024,
                ceiling=64 * 1024,
            )
        max_request_bytes = data.get("max_request_bytes")
        if max_request_bytes is not None:
            config["max_request_bytes"] = _bounded_positive_int(
                max_request_bytes,
                field="max_request_bytes",
                default=256 * 1024,
                ceiling=256 * 1024,
            )
        max_redirects = data.get("max_redirects")
        if max_redirects is not None:
            config["max_redirects"] = _bounded_positive_int(
                max_redirects,
                field="max_redirects",
                default=2,
                ceiling=2,
                allow_zero=True,
            )
        config["signing_algorithm"] = "hmac-sha256"
        return config
    return {}


def _bounded_positive_int(
    value: Any,
    *,
    field: str,
    default: int,
    ceiling: int,
    allow_zero: bool = False,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    floor = 0 if allow_zero else 1
    if parsed < floor:
        raise ValueError(f"{field} must be at least {floor}")
    return min(parsed, ceiling)
