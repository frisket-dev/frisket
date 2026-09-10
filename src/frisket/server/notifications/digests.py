"""Notification digest window policy and composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from frisket.server.notifications.delivery import NotificationRenderedMessage
from frisket.server.notifications.producers import (
    NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND,
)
from frisket.server.notifications.service import (
    LOCAL_ACTOR_ID,
    _loads_list,
    _loads_object,
    _severity_rank,
)
from frisket.engine.store import Project


@dataclass(frozen=True)
class NotificationDigestWindow:
    cadence: str
    window_key: str
    start_at: datetime
    end_at: datetime

    @property
    def start_sql(self) -> str:
        return _sqlite_dt(self.start_at)

    @property
    def end_sql(self) -> str:
        return _sqlite_dt(self.end_at)


def due_digest_window_for_route(
    route: Any,
    *,
    now: datetime | None = None,
) -> NotificationDigestWindow | None:
    cadence = str(route["digest_cadence"] or "daily")
    timezone = str(route["digest_timezone"] or "UTC")
    if timezone != "UTC":
        raise ValueError("notification digest timezone must be UTC")
    if cadence == "manual":
        return None
    current = _coerce_utc(now or datetime.now(UTC))
    if cadence == "hourly":
        end = current.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=1)
    elif cadence == "daily":
        anchor = _parse_anchor_time(route["digest_anchor_time"] or "09:00")
        anchor_today = datetime.combine(current.date(), anchor, tzinfo=UTC)
        end = (
            anchor_today
            if current >= anchor_today
            else anchor_today - timedelta(days=1)
        )
        start = end - timedelta(days=1)
    else:
        raise ValueError("unsupported notification digest cadence")
    return _window(cadence=cadence, start_at=start, end_at=end)


def manual_digest_window(
    *,
    start_at: datetime,
    end_at: datetime,
) -> NotificationDigestWindow:
    start = _coerce_utc(start_at)
    end = _coerce_utc(end_at)
    if end <= start:
        raise ValueError("notification digest window end must be after start")
    return _window(cadence="manual", start_at=start, end_at=end)


def compose_due_notification_digests(
    project: Project,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for route in project.notification_routes(enabled_only=True):
        if str(route["delivery_mode"]) != "digest":
            continue
        if str(route["digest_cadence"] or "daily") == "manual":
            continue
        runs.append(compose_notification_digest(project, int(route["id"]), now=now))
    return runs


def compose_notification_digest(
    project: Project,
    route_id: int,
    *,
    now: datetime | None = None,
    window: NotificationDigestWindow | None = None,
) -> dict[str, Any]:
    route = project.notification_route(route_id)
    if route is None:
        raise ValueError("notification route not found")
    if not bool(route["enabled"]):
        raise ValueError("notification digest route is disabled")
    if str(route["delivery_mode"]) != "digest":
        raise ValueError("notification route is not a digest route")
    channel = project.notification_channel(int(route["channel_id"]))
    if channel is None:
        raise ValueError("notification digest channel not found")
    if str(channel["kind"]) == "in_app":
        raise ValueError("in-app notification digest route is implicit")
    digest_window = window or due_digest_window_for_route(route, now=now)
    if digest_window is None:
        raise ValueError("manual notification digest requires an explicit window")
    if str(route["digest_timezone"] or "UTC") != "UTC":
        raise ValueError("notification digest timezone must be UTC")
    existing = project.notification_digest_run_by_route_window(
        route_id=int(route["id"]),
        window_key=digest_window.window_key,
    )
    if existing is not None:
        if str(existing["status"]) != "composing":
            return project.public_notification_digest_run(int(existing["id"]))
        run = project.public_notification_digest_run(int(existing["id"]))
    else:
        run = project.create_notification_digest_run(
            route_id=int(route["id"]),
            channel_id=int(route["channel_id"]),
            cadence=digest_window.cadence,
            window_key=digest_window.window_key,
            window_start_at=digest_window.start_sql,
            window_end_at=digest_window.end_sql,
            status="composing",
        )
    items = _eligible_digest_items(project, route, digest_window)
    item_ids = [int(item["id"]) for item in items]
    if not item_ids:
        return project.set_notification_digest_run_result(
            int(run["id"]),
            status="empty",
            item_count=0,
        )

    project.add_notification_digest_items(int(run["id"]), item_ids)
    request = project.create_notification_delivery_request(
        route_id=int(route["id"]),
        channel_id=int(route["channel_id"]),
        digest_run_id=int(run["id"]),
        delivery_kind="digest",
        dedupe_key=(
            f"notification:digest:route:{int(route['id'])}:"
            f"window:{digest_window.window_key}"
        ),
    )
    return project.set_notification_digest_run_result(
        int(run["id"]),
        status="composed",
        item_count=len(item_ids),
        delivery_request_id=int(request["id"]),
    )


def render_notification_digest_message(
    project: Project,
    *,
    project_id: str,
    request: Any,
) -> NotificationRenderedMessage:
    digest_run_id = request["digest_run_id"]
    if digest_run_id is None:
        raise ValueError("notification digest request missing digest_run_id")
    run = project.notification_digest_run(int(digest_run_id))
    if run is None:
        raise ValueError("notification digest run not found")
    route = project.notification_route(int(run["route_id"]))
    route_name = str(route["name"]) if route is not None else "Notification"
    items = project.notification_digest_items(int(run["id"]))
    item_count = len(items)
    title = f"{route_name} digest"
    summary = (
        f"{item_count} notification{'s' if item_count != 1 else ''} from "
        f"{run['window_start_at']} UTC to {run['window_end_at']} UTC."
    )
    if items:
        lines = [
            f"- {str(item['title'])}: {str(item['summary'])}" for item in items[:10]
        ]
        if len(items) > 10:
            lines.append(f"- {len(items) - 10} more notifications")
        summary = f"{summary}\n" + "\n".join(lines)
    return NotificationRenderedMessage(
        project_id=project_id,
        delivery_request_id=int(request["id"]),
        route_id=int(request["route_id"]) if request["route_id"] is not None else None,
        channel_id=int(request["channel_id"]),
        delivery_kind="digest",
        title=title,
        summary=summary,
        source_kind="digest",
        severity=_max_severity(items),
        notification_id=None,
        digest_run_id=int(run["id"]),
        deep_link={"kind": "notification_digest", "digest_run_id": int(run["id"])},
    )


def _eligible_digest_items(
    project: Project,
    route: Any,
    window: NotificationDigestWindow,
) -> list[Any]:
    actor_id = str(route["recipient_actor_id"] or LOCAL_ACTOR_ID)
    rows = project.db.execute(
        "SELECT i.*, COALESCE(s.state, 'unseen') AS actor_state "
        "FROM notification_items i "
        "LEFT JOIN notification_actor_state s "
        "ON s.notification_id=i.id AND s.actor_id=? "
        "WHERE i.created_at >= ? AND i.created_at < ? "
        "AND COALESCE(s.state, 'unseen') != 'acknowledged' "
        "ORDER BY i.created_at ASC, i.id ASC",
        (actor_id, window.start_sql, window.end_sql),
    ).fetchall()
    return [row for row in rows if _route_matches_digest_item(route, row)]


def _route_matches_digest_item(route: Any, item: Any) -> bool:
    if str(item["source_kind"]) == NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND:
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
    return _severity_rank(str(item["severity"])) >= _severity_rank(
        str(route["severity_min"])
    )


def _window(
    *,
    cadence: str,
    start_at: datetime,
    end_at: datetime,
) -> NotificationDigestWindow:
    start = _coerce_utc(start_at)
    end = _coerce_utc(end_at)
    return NotificationDigestWindow(
        cadence=cadence,
        window_key=f"{cadence}:{_iso_z(start)}:{_iso_z(end)}",
        start_at=start,
        end_at=end,
    )


def _parse_anchor_time(value: str) -> time:
    parts = str(value).split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("notification digest anchor time must be HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise ValueError("notification digest anchor time must be HH:MM") from exc


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def _sqlite_dt(value: datetime) -> str:
    return _coerce_utc(value).strftime("%Y-%m-%d %H:%M:%S")


def _iso_z(value: datetime) -> str:
    return _coerce_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_severity(items: list[Any]) -> str | None:
    if not items:
        return None
    return max((str(item["severity"]) for item in items), key=_severity_rank)
