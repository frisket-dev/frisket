"""Generic notification core: candidate emission and route planning.

Alerts, runs, sources, and plugins emit candidates. This module validates,
dedupes, records generic notification items, and plans external delivery
requests. In-app delivery is the durable item/feed projection; external
providers run only from delivery requests.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, TypedDict

from frisket.notifications.candidates import (
    NotificationCandidate,
    validate_notification_candidate,
)
from frisket.notifications.producers import (
    NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND,
)
from frisket.redaction import DEFAULT_LOG_STRING_MAX_CHARS, redact_text, redact_value

if TYPE_CHECKING:
    from frisket.engine.store import Project

LOCAL_ACTOR_ID = "local:project"
NOTIFICATION_EMIT_RESULT_SCHEMA_VERSION = "frisket.notification_emit_result.v2"
NOTIFICATION_SEVERITIES = ("info", "warning", "critical")

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
                event_count=normalized["event_count"],
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
                int(existing["event_count"] or 1),
                len(merged_event_ids),
                normalized["event_count"],
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


def _event_kinds_for_candidate(
    project: Project, candidate: NotificationCandidate, event_ids: list[int]
) -> list[str]:
    if candidate["source_kind"] != "watch":
        return []
    source_ref = candidate["source_ref"]
    try:
        alert_id = int(source_ref["watch_id"])
        run_id = int(source_ref["run_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("watch notification source_ref requires watch_id and run_id")
    if not event_ids:
        raise ValueError("watch notification source events are required")
    rows = project.alert_events_for_notification(
        alert_id=alert_id, run_id=run_id, event_ids=event_ids
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


def _dedupe_key(
    candidate: NotificationCandidate, source_ref: dict[str, Any], event_ids: list[int]
) -> str:
    if candidate["source_kind"] == "watch" and event_ids:
        return alert_events_dedupe_key(
            alert_id=int(source_ref["watch_id"]),
            run_id=int(source_ref["run_id"]),
            event_ids=event_ids,
        )
    return str(candidate["dedupe_key"]).strip()


def alert_events_dedupe_key(*, alert_id: int, run_id: int, event_ids: list[int]) -> str:
    # The check owns one summary, irrespective of how many detail previews fit.
    return f"watch:{int(alert_id)}:run:{int(run_id)}"


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
