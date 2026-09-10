"""Generic notification core service.

Watchlists, runs, sources, and plugins emit candidates. This module validates,
dedupes, records generic notification items, and plans external delivery
requests. In-app delivery is the durable item/feed projection; external
providers run only from delivery requests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, TypedDict

from frisket.server.notifications.candidates import (
    NotificationCandidate,
    validate_notification_candidate,
)
from frisket.server.notifications.producers import (
    NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND,
)
from frisket.redaction import DEFAULT_LOG_STRING_MAX_CHARS, redact_text, redact_value
from frisket.engine.store import Project
from frisket.features.watchlists.specs import canonical_json

LOCAL_ACTOR_ID = "local:project"
NOTIFICATION_PAGE_SCHEMA_VERSION = "frisket.notifications_page.v1"
NOTIFICATION_SUMMARY_SCHEMA_VERSION = "frisket.notifications_summary.v1"
NOTIFICATION_CHANNELS_SCHEMA_VERSION = "frisket.notification_channels.v1"
NOTIFICATION_ROUTES_SCHEMA_VERSION = "frisket.notification_routes.v1"
NOTIFICATION_DELIVERY_REQUESTS_SCHEMA_VERSION = (
    "frisket.notification_delivery_requests.v1"
)
NOTIFICATION_EMIT_RESULT_SCHEMA_VERSION = "frisket.notification_emit_result.v2"

NOTIFICATION_STATES = {"unseen", "seen", "read", "acknowledged", "all"}
NOTIFICATION_CHANNEL_KINDS = {"in_app", "email", "slack", "webhook"}
NOTIFICATION_ROUTE_OWNER_KINDS = {"project", "system", "user"}
NOTIFICATION_ROUTE_DELIVERY_MODES = {"immediate", "digest"}
NOTIFICATION_DIGEST_CADENCES = {"hourly", "daily", "manual"}
NOTIFICATION_SEVERITIES = ("info", "warning", "critical")
DELIVERY_STATUSES = {
    "queued",
    "processing",
    "sent",
    "failed",
    "skipped",
    "cancelled",
    "reconciliation_required",
}

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
    "webhook_url",
)


class NotificationEmitDeliveryRequest(TypedDict):
    id: int
    route_id: int
    channel_id: int
    delivery_kind: str
    status: str


class NotificationEmitResult(TypedDict):
    schema_version: str
    notification_id: int
    deduped: bool
    planned_delivery_requests: list[NotificationEmitDeliveryRequest]


def emit_notification_candidate(
    project: Project,
    candidate: dict[str, Any] | NotificationCandidate,
    *,
    actor_id: str = LOCAL_ACTOR_ID,
) -> NotificationEmitResult:
    normalized = validate_notification_candidate(dict(candidate))
    safe_source_ref = redact_value(
        normalized["source_ref"],
        max_string_chars=DEFAULT_LOG_STRING_MAX_CHARS,
    )
    safe_deep_link = redact_value(
        normalized.get("deep_link", {}),
        max_string_chars=DEFAULT_LOG_STRING_MAX_CHARS,
    )
    safe_payload = redact_value(
        normalized.get("payload", {}),
        max_string_chars=DEFAULT_LOG_STRING_MAX_CHARS,
    )
    normalized = {
        **normalized,
        "source_kind": redact_text(normalized["source_kind"], max_chars=4096),
        "source_ref": safe_source_ref if isinstance(safe_source_ref, dict) else {},
        "dedupe_key": redact_text(normalized["dedupe_key"], max_chars=4096),
        "title": redact_text(normalized["title"], max_chars=4096),
        "summary": redact_text(normalized["summary"], max_chars=4096, one_line=False),
        "deep_link": safe_deep_link if isinstance(safe_deep_link, dict) else {},
        "payload": safe_payload if isinstance(safe_payload, dict) else {},
    }
    source_ref = _canonical_object(normalized["source_ref"])
    event_ids = sorted({int(value) for value in normalized.get("source_event_ids", [])})
    event_kinds = _event_kinds_for_candidate(project, normalized, event_ids)
    dedupe_key = _dedupe_key(normalized, source_ref, event_ids)
    payload = _redact_payload(normalized.get("payload", {}))
    deep_link = _canonical_object(normalized.get("deep_link", {}))

    existing = project.notification_item_by_dedupe_key(dedupe_key)
    deduped = existing is not None
    if existing is None:
        try:
            notification_id = project.insert_notification_item(
                source_kind=normalized["source_kind"],
                source_ref=_json(source_ref),
                dedupe_key=dedupe_key,
                source_event_ids=_json(event_ids),
                event_count=max(1, len(event_ids)),
                event_kinds=_json(event_kinds),
                title=normalized["title"],
                summary=normalized["summary"],
                severity=normalized["severity"],
                deep_link=_json(deep_link),
                payload=_json(payload),
            )
        except sqlite3.IntegrityError:
            existing = project.notification_item_by_dedupe_key(dedupe_key)
            if existing is None:
                raise
            deduped = True

    if existing is not None:
        notification_id = int(existing["id"])
        merged_event_ids = sorted(
            {
                *_loads_list(existing["source_event_ids"]),
                *event_ids,
            }
        )
        merged_event_kinds = sorted(
            {
                *[str(value) for value in _loads_list(existing["event_kinds"])],
                *event_kinds,
            }
        )
        material_update = {
            "source_event_ids": _json(merged_event_ids),
            "event_count": max(
                int(existing["event_count"] or 1), len(merged_event_ids), 1
            ),
            "event_kinds": _json(merged_event_kinds),
            "title": normalized["title"],
            "summary": normalized["summary"],
            "severity": normalized["severity"],
            "deep_link": _json(deep_link),
            "payload": _json(payload),
        }
        if any(existing[field] != value for field, value in material_update.items()):
            project.update_notification_item(notification_id, **material_update)

    project.ensure_notification_actor_state(notification_id, actor_id)
    planned_requests = plan_notification_routes(project, notification_id)
    return {
        "schema_version": NOTIFICATION_EMIT_RESULT_SCHEMA_VERSION,
        "notification_id": notification_id,
        "deduped": deduped,
        "planned_delivery_requests": [
            _public_request_for_emit(request) for request in planned_requests
        ],
    }


def plan_notification_routes(
    project: Project,
    notification_id: int,
) -> list[dict[str, Any]]:
    item = project.notification_item(notification_id)
    if item is None:
        raise ValueError("notification not found")
    if str(item["source_kind"]) == NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND:
        return []
    requests: list[dict[str, Any]] = []
    for route in project.notification_routes(enabled_only=True):
        if not _route_matches_item(route, item):
            continue
        channel = project.notification_channel(int(route["channel_id"]))
        if channel is None or not bool(channel["enabled"]):
            continue
        if str(channel["kind"]) == "in_app":
            continue
        dedupe_key = (
            f"notification:item:{int(notification_id)}:"
            f"route:{int(route['id'])}:channel:{int(route['channel_id'])}"
        )
        requests.append(
            project.create_notification_delivery_request(
                route_id=int(route["id"]),
                channel_id=int(route["channel_id"]),
                notification_id=int(notification_id),
                delivery_kind="item",
                dedupe_key=dedupe_key,
            )
        )
    return requests


def create_notification_test_request(
    project: Project,
    route_id: int,
) -> dict[str, Any]:
    route = project.notification_route(route_id)
    if route is None:
        raise ValueError("notification route not found")
    channel = project.notification_channel(int(route["channel_id"]))
    if channel is None:
        raise ValueError("notification channel not found")
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


def public_notification_item(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "source_kind": row["source_kind"],
        "source_ref": _loads_object(row["source_ref"]),
        "source_event_ids": _loads_list(row["source_event_ids"]),
        "event_count": int(row["event_count"]),
        "event_kinds": _loads_list(row["event_kinds"]),
        "title": row["title"],
        "summary": row["summary"],
        "severity": row["severity"],
        "deep_link": _loads_object(row["deep_link"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "state": row["actor_state"],
        "seen_at": row["seen_at"],
        "read_at": row["read_at"],
        "acknowledged_at": row["acknowledged_at"],
        "acknowledged_by": row["acknowledged_by"],
    }


def public_actor_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "notification_id": int(state["notification_id"]),
        "actor_id": state["actor_id"],
        "state": state["state"],
        "seen_at": state["seen_at"],
        "read_at": state["read_at"],
        "acknowledged_at": state["acknowledged_at"],
        "acknowledged_by": state["acknowledged_by"],
        "updated_at": state["updated_at"],
    }


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


def _event_kinds_for_candidate(
    project: Project, candidate: NotificationCandidate, event_ids: list[int]
) -> list[str]:
    if candidate["source_kind"] != "watch":
        return []
    source_ref = candidate["source_ref"]
    try:
        watch_id = int(source_ref["watch_id"])
        run_id = int(source_ref["run_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("watch notification source_ref requires watch_id and run_id")
    if not event_ids:
        raise ValueError("watch notification source events are required")
    rows = project.watch_events_for_notification(
        watch_id=watch_id, run_id=run_id, event_ids=event_ids
    )
    found = {int(row["id"]) for row in rows}
    if found != set(event_ids):
        raise ValueError("watch notification source events must exist in this project")
    return sorted({str(row["event_kind"]) for row in rows})


def _route_matches_item(route: Any, item: Any) -> bool:
    if str(route["delivery_mode"]) != "immediate":
        return False
    route_source_kind = route["source_kind"]
    if route_source_kind and route_source_kind != item["source_kind"]:
        return False
    source_ref_match = _loads_object(route["source_ref_match_json"])
    if source_ref_match:
        item_source_ref = _loads_object(item["source_ref"])
        for key, value in source_ref_match.items():
            if item_source_ref.get(key) != value:
                return False
    route_event_kinds = {str(value) for value in _loads_list(route["event_kinds_json"])}
    if route_event_kinds:
        item_event_kinds = {str(value) for value in _loads_list(item["event_kinds"])}
        if not item_event_kinds.intersection(route_event_kinds):
            return False
    if _severity_rank(str(item["severity"])) < _severity_rank(
        str(route["severity_min"])
    ):
        return False
    return True


def _severity_rank(value: str) -> int:
    try:
        return NOTIFICATION_SEVERITIES.index(value)
    except ValueError:
        return -1


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


def _dedupe_key(
    candidate: NotificationCandidate, source_ref: dict[str, Any], event_ids: list[int]
) -> str:
    if candidate["source_kind"] == "watch" and event_ids:
        return watch_events_dedupe_key(
            watch_id=int(source_ref["watch_id"]),
            run_id=int(source_ref["run_id"]),
            event_ids=event_ids,
        )
    return str(candidate["dedupe_key"]).strip()


def watch_events_dedupe_key(*, watch_id: int, run_id: int, event_ids: list[int]) -> str:
    digest = hashlib.sha256(
        canonical_json(sorted({int(value) for value in event_ids})).encode("utf-8")
    ).hexdigest()
    return f"watch:{int(watch_id)}:run:{int(run_id)}:events:sha256:{digest}"


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


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                continue
            out[key_text] = _redact_payload(child)
        return out
    if isinstance(value, list):
        return [_redact_payload(child) for child in value]
    return value


def _canonical_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return json.loads(_json(value))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads_object(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _loads_list(value: Any) -> list[Any]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _public_attempt_for_emit(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "channel_id": attempt["channel_id"],
        "kind": attempt["kind"],
        "status": attempt["status"],
    }


def _public_request_for_emit(
    request: dict[str, Any],
) -> NotificationEmitDeliveryRequest:
    return {
        "id": request["id"],
        "route_id": request["route_id"],
        "channel_id": request["channel_id"],
        "delivery_kind": request["delivery_kind"],
        "status": request["status"],
    }
