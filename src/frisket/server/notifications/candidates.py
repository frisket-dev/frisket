"""Validation helpers for future notification producers.

Stage 1 intentionally defines candidates only. Durable notification items,
routes, channels, and delivery attempts belong to later lanes.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, NotRequired, TypedDict

Severity = Literal["info", "warning", "critical"]


class NotificationCandidate(TypedDict):
    source_kind: str
    source_ref: dict[str, Any]
    dedupe_key: str
    title: str
    summary: str
    source_event_ids: NotRequired[list[int]]
    severity: NotRequired[Severity]
    deep_link: NotRequired[dict[str, Any]]
    payload: NotRequired[dict[str, Any]]


def validate_notification_candidate(data: dict[str, Any]) -> NotificationCandidate:
    if not isinstance(data, dict):
        raise ValueError("notification candidate must be an object")
    source_kind = _required_string(data, "source_kind")
    source_ref = _required_object(data, "source_ref")
    dedupe_key = _required_string(data, "dedupe_key")
    title = _required_string(data, "title")
    summary = _required_string(data, "summary")
    event_ids = data.get("source_event_ids", [])
    if not isinstance(event_ids, list):
        raise ValueError("source_event_ids must be an array")
    normalized_event_ids: list[int] = []
    for value in event_ids:
        try:
            event_id = int(value)
        except (TypeError, ValueError):
            raise ValueError("source_event_ids must contain integers") from None
        if event_id <= 0:
            raise ValueError("source_event_ids must contain positive integers")
        normalized_event_ids.append(event_id)
    severity = str(data.get("severity") or "info").strip().lower()
    if severity not in {"info", "warning", "critical"}:
        raise ValueError("severity must be info, warning, or critical")
    deep_link = data.get("deep_link", {})
    if not isinstance(deep_link, dict):
        raise ValueError("deep_link must be an object")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return {
        "source_kind": source_kind,
        "source_ref": deepcopy(source_ref),
        "source_event_ids": normalized_event_ids,
        "dedupe_key": dedupe_key,
        "title": title,
        "summary": summary,
        "severity": severity,  # type: ignore[typeddict-item]
        "deep_link": deepcopy(deep_link),
        "payload": deepcopy(payload),
    }


def _required_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _required_object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field} is required")
    return value
