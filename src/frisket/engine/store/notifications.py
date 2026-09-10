"""Notification store for a project bundle: items, per-actor state, channels,
routes, digest runs, and delivery requests/attempts, plus the visibility
clauses that scope delivery rows to a viewer. Free functions over the facade's
per-thread SQLite connection; ``project`` stays duck-typed (``Any``) so this
leaf never re-imports the facade module."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Collection


def _safe_json_key(key: str) -> bool:
    return bool(key) and all(c.isalnum() or c == "_" for c in key)


def _int_collection(values: Collection[int] | None) -> list[int] | None:
    if values is None:
        return None
    return sorted({int(value) for value in values})


def _placeholders(values: Collection[int]) -> str:
    return ",".join("?" for _ in values)


def _notification_delivery_visibility_clause(
    alias: str,
    *,
    visible_delivery_route_ids: Collection[int] | None,
    visible_delivery_channel_ids: Collection[int] | None,
) -> tuple[str, list[Any]]:
    route_ids = _int_collection(visible_delivery_route_ids)
    channel_ids = _int_collection(visible_delivery_channel_ids)
    if route_ids is None and channel_ids is None:
        return "", []
    clauses = [f"{alias}.source_kind!='notification_delivery'"]
    params: list[Any] = []
    delivery_clauses: list[str] = []
    if route_ids:
        delivery_clauses.append(
            "CAST(json_extract("
            f"{alias}.source_ref, '$.route_id') AS INTEGER) "
            f"IN ({_placeholders(route_ids)})"
        )
        params.extend(route_ids)
    if channel_ids:
        delivery_clauses.append(
            "(json_extract("
            f"{alias}.source_ref, '$.route_id') IS NULL "
            "AND CAST(json_extract("
            f"{alias}.source_ref, '$.channel_id') AS INTEGER) "
            f"IN ({_placeholders(channel_ids)}))"
        )
        params.extend(channel_ids)
    if delivery_clauses:
        clauses.append(
            f"({alias}.source_kind='notification_delivery' AND "
            f"({' OR '.join(delivery_clauses)})"
            ")"
        )
    return f"({' OR '.join(clauses)})", params


def _delivery_request_visibility_clause(
    *,
    visible_route_ids: Collection[int] | None,
    visible_channel_ids: Collection[int] | None,
) -> tuple[str, list[Any]]:
    route_ids = _int_collection(visible_route_ids)
    channel_ids = _int_collection(visible_channel_ids)
    if route_ids is None and channel_ids is None:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    if route_ids:
        clauses.append(f"route_id IN ({_placeholders(route_ids)})")
        params.extend(route_ids)
    if channel_ids:
        clauses.append(
            f"(route_id IS NULL AND channel_id IN ({_placeholders(channel_ids)}))"
        )
        params.extend(channel_ids)
    if not clauses:
        return "0=1", []
    return f"({' OR '.join(clauses)})", params


def _loads_json_object(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _loads_json_list(value: Any) -> list[Any]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _public_notification_channel_config(
    kind: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    public_config = {
        key: value
        for key, value in config.items()
        if key not in {"owner_kind", "owner_ref"}
    }
    if kind != "webhook":
        return public_config
    public: dict[str, Any] = {}
    if public_config.get("webhook_host"):
        public["webhook_host"] = str(public_config["webhook_host"])
    if public_config.get("signature_header"):
        public["signature_header"] = str(public_config["signature_header"])
    if public_config.get("signing_algorithm"):
        public["signing_algorithm"] = str(public_config["signing_algorithm"])
    if public_config.get("max_response_bytes") is not None:
        public["max_response_bytes"] = int(public_config["max_response_bytes"])
    if public_config.get("max_request_bytes") is not None:
        public["max_request_bytes"] = int(public_config["max_request_bytes"])
    if public_config.get("max_redirects") is not None:
        public["max_redirects"] = int(public_config["max_redirects"])
    public["has_signing_secret"] = bool(public_config.get("signing_secret_ref"))
    return public


def notification_item_by_dedupe_key(
    project: Any, dedupe_key: str
) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_items WHERE dedupe_key=?",
        (dedupe_key,),
    ).fetchone()


def notification_item(project: Any, notification_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_items WHERE id=?",
        (int(notification_id),),
    ).fetchone()


def insert_notification_item(
    project: Any,
    *,
    source_kind: str,
    source_ref: str,
    dedupe_key: str,
    source_event_ids: str,
    event_count: int,
    event_kinds: str,
    title: str,
    summary: str,
    severity: str,
    deep_link: str,
    payload: str,
) -> int:
    cur = project.db.execute(
        "INSERT INTO notification_items "
        "(source_kind, source_ref, dedupe_key, source_event_ids, "
        "event_count, event_kinds, title, summary, severity, deep_link, payload) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_kind,
            source_ref,
            dedupe_key,
            source_event_ids,
            int(event_count),
            event_kinds,
            title,
            summary,
            severity,
            deep_link,
            payload,
        ),
    )
    project.db.commit()
    return int(cur.lastrowid)


def update_notification_item(
    project: Any,
    notification_id: int | None,
    *,
    source_event_ids: str,
    event_count: int,
    event_kinds: str,
    title: str,
    summary: str,
    severity: str,
    deep_link: str,
    payload: str,
) -> None:
    project.db.execute(
        "UPDATE notification_items SET source_event_ids=?, event_count=?, "
        "event_kinds=?, title=?, summary=?, severity=?, deep_link=?, "
        "payload=?, updated_at=datetime('now') WHERE id=?",
        (
            source_event_ids,
            int(event_count),
            event_kinds,
            title,
            summary,
            severity,
            deep_link,
            payload,
            int(notification_id) if notification_id is not None else None,
        ),
    )
    project.db.commit()


def list_notification_items(
    project: Any,
    *,
    actor_id: str,
    state: str = "all",
    source_kind: str | None = None,
    source_ref: dict[str, Any] | None = None,
    severity: str | None = None,
    visible_delivery_route_ids: Collection[int] | None = None,
    visible_delivery_channel_ids: Collection[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[sqlite3.Row]:
    where, params = _notification_filters(
        project,
        actor_id=actor_id,
        state=state,
        source_kind=source_kind,
        source_ref=source_ref,
        severity=severity,
        visible_delivery_route_ids=visible_delivery_route_ids,
        visible_delivery_channel_ids=visible_delivery_channel_ids,
    )
    return project.db.execute(
        "SELECT i.*, COALESCE(s.state, 'unseen') AS actor_state, "
        "s.seen_at, s.read_at, s.acknowledged_at, s.acknowledged_by "
        "FROM notification_items i "
        "LEFT JOIN notification_actor_state s "
        "ON s.notification_id=i.id AND s.actor_id=? "
        f"WHERE {where} ORDER BY i.created_at DESC, i.id DESC LIMIT ? OFFSET ?",
        (actor_id, *params, int(limit), int(offset)),
    ).fetchall()


def notification_items_total(
    project: Any,
    *,
    actor_id: str,
    state: str = "all",
    source_kind: str | None = None,
    source_ref: dict[str, Any] | None = None,
    severity: str | None = None,
    visible_delivery_route_ids: Collection[int] | None = None,
    visible_delivery_channel_ids: Collection[int] | None = None,
) -> int:
    where, params = _notification_filters(
        project,
        actor_id=actor_id,
        state=state,
        source_kind=source_kind,
        source_ref=source_ref,
        severity=severity,
        visible_delivery_route_ids=visible_delivery_route_ids,
        visible_delivery_channel_ids=visible_delivery_channel_ids,
    )
    row = project.db.execute(
        "SELECT COUNT(*) AS count FROM notification_items i "
        "LEFT JOIN notification_actor_state s "
        "ON s.notification_id=i.id AND s.actor_id=? "
        f"WHERE {where}",
        (actor_id, *params),
    ).fetchone()
    return int(row["count"] if row else 0)


def notification_summary(
    project: Any,
    *,
    actor_id: str,
    visible_delivery_route_ids: Collection[int] | None = None,
    visible_delivery_channel_ids: Collection[int] | None = None,
) -> dict[str, Any]:
    visibility_clause, visibility_params = _notification_delivery_visibility_clause(
        "i",
        visible_delivery_route_ids=visible_delivery_route_ids,
        visible_delivery_channel_ids=visible_delivery_channel_ids,
    )
    visibility_where = f"WHERE {visibility_clause}" if visibility_clause else ""
    counts = {"unseen": 0, "seen": 0, "read": 0, "acknowledged": 0}
    total = 0
    for row in project.db.execute(
        "SELECT COALESCE(s.state, 'unseen') AS actor_state, COUNT(*) AS count "
        "FROM notification_items i "
        "LEFT JOIN notification_actor_state s "
        "ON s.notification_id=i.id AND s.actor_id=? "
        f"{visibility_where} "
        "GROUP BY COALESCE(s.state, 'unseen')",
        (actor_id, *visibility_params),
    ):
        state = str(row["actor_state"] or "unseen")
        count = int(row["count"] or 0)
        total += count
        if state in counts:
            counts[state] = count

    by_severity = {
        str(row["severity"]): int(row["count"] or 0)
        for row in project.db.execute(
            "SELECT severity, COUNT(*) AS count "
            f"FROM notification_items i {visibility_where} "
            "GROUP BY severity ORDER BY severity",
            visibility_params,
        )
    }
    by_source_kind = {
        str(row["source_kind"]): int(row["count"] or 0)
        for row in project.db.execute(
            "SELECT source_kind, COUNT(*) AS count "
            f"FROM notification_items i {visibility_where} "
            "GROUP BY source_kind ORDER BY source_kind",
            visibility_params,
        )
    }
    by_watch = [
        {
            "source_kind": "watch",
            "source_ref": {"watch_id": int(row["watch_id"])},
            "unseen": int(row["unseen"] or 0),
        }
        for row in project.db.execute(
            "SELECT json_extract(i.source_ref, '$.watch_id') AS watch_id, "
            "COUNT(*) AS unseen "
            "FROM notification_items i "
            "LEFT JOIN notification_actor_state s "
            "ON s.notification_id=i.id AND s.actor_id=? "
            "WHERE i.source_kind='watch' "
            "AND COALESCE(s.state, 'unseen')='unseen' "
            "AND json_extract(i.source_ref, '$.watch_id') IS NOT NULL "
            "GROUP BY json_extract(i.source_ref, '$.watch_id') "
            "ORDER BY CAST(json_extract(i.source_ref, '$.watch_id') AS INTEGER)",
            (actor_id,),
        )
    ]
    return {
        "total": total,
        **counts,
        "by_severity": by_severity,
        "by_source_kind": by_source_kind,
        "by_source_ref": by_watch,
    }


def ensure_notification_actor_state(
    project: Any, notification_id: int, actor_id: str
) -> sqlite3.Row:
    project.db.execute(
        "INSERT OR IGNORE INTO notification_actor_state "
        "(notification_id, actor_id, state) VALUES (?, ?, 'unseen')",
        (int(notification_id), actor_id),
    )
    project.db.commit()
    row = project.db.execute(
        "SELECT * FROM notification_actor_state WHERE notification_id=? AND actor_id=?",
        (int(notification_id), actor_id),
    ).fetchone()
    if row is None:
        raise ValueError("notification actor state could not be created")
    return row


def notification_actor_state(
    project: Any, notification_id: int, actor_id: str
) -> dict[str, Any]:
    row = project.ensure_notification_actor_state(notification_id, actor_id)
    return dict(row)


def mark_notifications_seen(
    project: Any,
    *,
    actor_id: str,
    notification_ids: list[int] | None = None,
    source_kind: str | None = None,
    source_ref: dict[str, Any] | None = None,
    before_created_at: str | None = None,
) -> int:
    ids = _select_notification_ids_for_state_change(
        project,
        actor_id=actor_id,
        notification_ids=notification_ids,
        source_kind=source_kind,
        source_ref=source_ref,
        before_created_at=before_created_at,
    )
    count = 0
    for notification_id in ids:
        row = project.ensure_notification_actor_state(notification_id, actor_id)
        if row["seen_at"] is None:
            project.db.execute(
                "UPDATE notification_actor_state SET state=?, "
                "seen_at=COALESCE(seen_at, datetime('now')), "
                "updated_at=datetime('now') WHERE notification_id=? AND actor_id=?",
                ("seen", notification_id, actor_id),
            )
            count += 1
    project.db.commit()
    return count


def mark_notification_read(
    project: Any, notification_id: int, actor_id: str
) -> dict[str, Any]:
    project.ensure_notification_actor_state(notification_id, actor_id)
    project.db.execute(
        "UPDATE notification_actor_state SET state='read', "
        "seen_at=COALESCE(seen_at, datetime('now')), "
        "read_at=COALESCE(read_at, datetime('now')), updated_at=datetime('now') "
        "WHERE notification_id=? AND actor_id=?",
        (int(notification_id), actor_id),
    )
    project.db.commit()
    return project.notification_actor_state(notification_id, actor_id)


def acknowledge_notification(
    project: Any, notification_id: int, actor_id: str
) -> dict[str, Any]:
    project.ensure_notification_actor_state(notification_id, actor_id)
    project.db.execute(
        "UPDATE notification_actor_state SET state='acknowledged', "
        "seen_at=COALESCE(seen_at, datetime('now')), "
        "read_at=COALESCE(read_at, datetime('now')), "
        "acknowledged_at=COALESCE(acknowledged_at, datetime('now')), "
        "acknowledged_by=?, updated_at=datetime('now') "
        "WHERE notification_id=? AND actor_id=?",
        (actor_id, int(notification_id), actor_id),
    )
    project.db.commit()
    return project.notification_actor_state(notification_id, actor_id)


def acknowledge_notifications(
    project: Any,
    *,
    actor_id: str,
    notification_ids: list[int] | None = None,
    source_kind: str | None = None,
    source_ref: dict[str, Any] | None = None,
    before_created_at: str | None = None,
) -> int:
    ids = _select_notification_ids_for_state_change(
        project,
        actor_id=actor_id,
        notification_ids=notification_ids,
        source_kind=source_kind,
        source_ref=source_ref,
        before_created_at=before_created_at,
    )
    count = 0
    for notification_id in ids:
        before = project.ensure_notification_actor_state(notification_id, actor_id)
        project.acknowledge_notification(notification_id, actor_id)
        if before["state"] != "acknowledged":
            count += 1
    return count


def unacknowledge_notification(
    project: Any, notification_id: int, actor_id: str
) -> dict[str, Any]:
    row = project.ensure_notification_actor_state(notification_id, actor_id)
    next_state = "read" if row["read_at"] else "seen" if row["seen_at"] else "unseen"
    project.db.execute(
        "UPDATE notification_actor_state SET state=?, acknowledged_at=NULL, "
        "acknowledged_by=NULL, updated_at=datetime('now') "
        "WHERE notification_id=? AND actor_id=?",
        (next_state, int(notification_id), actor_id),
    )
    project.db.commit()
    return project.notification_actor_state(notification_id, actor_id)


def create_notification_channel(
    project: Any,
    *,
    kind: str,
    name: str,
    enabled: bool = True,
    config: dict[str, Any] | None = None,
    secret_ref: str | None = None,
) -> dict[str, Any]:
    if kind != "in_app":
        project.ensure_default_in_app_notification_channel()
    cur = project.db.execute(
        "INSERT INTO notification_channels "
        "(kind, name, enabled, config_json, secret_ref) VALUES (?,?,?,?,?)",
        (
            kind,
            name,
            int(enabled),
            json.dumps(config or {}, sort_keys=True),
            secret_ref,
        ),
    )
    project.db.commit()
    return project.public_notification_channel(int(cur.lastrowid))


def ensure_default_in_app_notification_channel(project: Any) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT id FROM notification_channels WHERE kind='in_app' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return project.public_notification_channel(int(row["id"]))
    return project.create_notification_channel(
        kind="in_app",
        name="In app",
        enabled=True,
        config={"audience": "project"},
        secret_ref=None,
    )


def notification_channel(project: Any, channel_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_channels WHERE id=?",
        (int(channel_id),),
    ).fetchone()


def notification_channels(project: Any) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT * FROM notification_channels ORDER BY id ASC"
    ).fetchall()


def enabled_notification_channels(project: Any) -> list[sqlite3.Row]:
    project.ensure_default_in_app_notification_channel()
    return project.db.execute(
        "SELECT * FROM notification_channels WHERE enabled=1 ORDER BY id ASC"
    ).fetchall()


def update_notification_channel(
    project: Any,
    channel_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    config: dict[str, Any] | None = None,
    secret_ref: str | None = None,
    set_secret_ref: bool = False,
) -> dict[str, Any]:
    row = project.notification_channel(channel_id)
    if row is None:
        raise ValueError("notification channel not found")
    next_name = name if name is not None else str(row["name"])
    next_enabled = int(enabled) if enabled is not None else int(row["enabled"])
    next_config = (
        json.dumps(config, sort_keys=True)
        if config is not None
        else str(row["config_json"] or "{}")
    )
    next_secret_ref = secret_ref if set_secret_ref else row["secret_ref"]
    project.db.execute(
        "UPDATE notification_channels SET name=?, enabled=?, config_json=?, "
        "secret_ref=?, updated_at=datetime('now') WHERE id=?",
        (next_name, next_enabled, next_config, next_secret_ref, int(channel_id)),
    )
    project.db.commit()
    return project.public_notification_channel(channel_id)


def public_notification_channel(project: Any, channel_id: int) -> dict[str, Any]:
    row = project.notification_channel(channel_id)
    if row is None:
        raise ValueError("notification channel not found")
    return _public_notification_channel_row(project, row)


def public_notification_channels(project: Any) -> list[dict[str, Any]]:
    project.ensure_default_in_app_notification_channel()
    return [
        _public_notification_channel_row(project, row)
        for row in project.notification_channels()
    ]


def notification_route(project: Any, route_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_routes WHERE id=?",
        (int(route_id),),
    ).fetchone()


def notification_routes(
    project: Any, *, enabled_only: bool = False
) -> list[sqlite3.Row]:
    where = "WHERE enabled=1" if enabled_only else ""
    return project.db.execute(
        f"SELECT * FROM notification_routes {where} ORDER BY id ASC"
    ).fetchall()


def create_notification_route(
    project: Any,
    *,
    name: str,
    channel_id: int,
    enabled: bool = True,
    owner_kind: str = "project",
    owner_ref: str | None = None,
    recipient_actor_id: str | None = None,
    source_kind: str | None = None,
    source_ref_match: dict[str, Any] | None = None,
    event_kinds: list[str] | None = None,
    severity_min: str = "info",
    delivery_mode: str = "immediate",
    digest_cadence: str | None = None,
    digest_timezone: str = "UTC",
    digest_anchor_time: str | None = None,
    template_key: str | None = None,
) -> dict[str, Any]:
    cur = project.db.execute(
        "INSERT INTO notification_routes "
        "(name, enabled, owner_kind, owner_ref, recipient_actor_id, channel_id, "
        "source_kind, source_ref_match_json, event_kinds_json, severity_min, "
        "delivery_mode, digest_cadence, digest_timezone, digest_anchor_time, "
        "template_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            name,
            int(enabled),
            owner_kind,
            owner_ref or project.path.stem,
            recipient_actor_id,
            int(channel_id),
            source_kind,
            json.dumps(source_ref_match or {}, sort_keys=True),
            json.dumps(event_kinds or [], sort_keys=True),
            severity_min,
            delivery_mode,
            digest_cadence,
            digest_timezone,
            digest_anchor_time,
            template_key,
        ),
    )
    project.db.commit()
    return project.public_notification_route(int(cur.lastrowid))


def update_notification_route(
    project: Any,
    route_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    owner_kind: str | None = None,
    owner_ref: str | None = None,
    recipient_actor_id: str | None = None,
    set_recipient_actor_id: bool = False,
    channel_id: int | None = None,
    source_kind: str | None = None,
    set_source_kind: bool = False,
    source_ref_match: dict[str, Any] | None = None,
    event_kinds: list[str] | None = None,
    severity_min: str | None = None,
    delivery_mode: str | None = None,
    digest_cadence: str | None = None,
    set_digest_cadence: bool = False,
    digest_timezone: str | None = None,
    digest_anchor_time: str | None = None,
    set_digest_anchor_time: bool = False,
    template_key: str | None = None,
    set_template_key: bool = False,
) -> dict[str, Any]:
    row = project.notification_route(route_id)
    if row is None:
        raise ValueError("notification route not found")
    next_values = {
        "name": name if name is not None else row["name"],
        "enabled": int(enabled) if enabled is not None else int(row["enabled"]),
        "owner_kind": owner_kind if owner_kind is not None else row["owner_kind"],
        "owner_ref": owner_ref if owner_ref is not None else row["owner_ref"],
        "recipient_actor_id": (
            recipient_actor_id if set_recipient_actor_id else row["recipient_actor_id"]
        ),
        "channel_id": int(channel_id)
        if channel_id is not None
        else int(row["channel_id"]),
        "source_kind": source_kind if set_source_kind else row["source_kind"],
        "source_ref_match_json": (
            json.dumps(source_ref_match or {}, sort_keys=True)
            if source_ref_match is not None
            else row["source_ref_match_json"]
        ),
        "event_kinds_json": (
            json.dumps(event_kinds or [], sort_keys=True)
            if event_kinds is not None
            else row["event_kinds_json"]
        ),
        "severity_min": severity_min
        if severity_min is not None
        else row["severity_min"],
        "delivery_mode": delivery_mode
        if delivery_mode is not None
        else row["delivery_mode"],
        "digest_cadence": digest_cadence
        if set_digest_cadence
        else row["digest_cadence"],
        "digest_timezone": digest_timezone
        if digest_timezone is not None
        else row["digest_timezone"],
        "digest_anchor_time": (
            digest_anchor_time if set_digest_anchor_time else row["digest_anchor_time"]
        ),
        "template_key": template_key if set_template_key else row["template_key"],
    }
    project.db.execute(
        "UPDATE notification_routes SET name=?, enabled=?, owner_kind=?, "
        "owner_ref=?, recipient_actor_id=?, channel_id=?, source_kind=?, "
        "source_ref_match_json=?, event_kinds_json=?, severity_min=?, "
        "delivery_mode=?, digest_cadence=?, digest_timezone=?, "
        "digest_anchor_time=?, template_key=?, updated_at=datetime('now') "
        "WHERE id=?",
        (
            next_values["name"],
            next_values["enabled"],
            next_values["owner_kind"],
            next_values["owner_ref"],
            next_values["recipient_actor_id"],
            next_values["channel_id"],
            next_values["source_kind"],
            next_values["source_ref_match_json"],
            next_values["event_kinds_json"],
            next_values["severity_min"],
            next_values["delivery_mode"],
            next_values["digest_cadence"],
            next_values["digest_timezone"],
            next_values["digest_anchor_time"],
            next_values["template_key"],
            int(route_id),
        ),
    )
    project.db.commit()
    return project.public_notification_route(route_id)


def public_notification_route(project: Any, route_id: int) -> dict[str, Any]:
    row = project.notification_route(route_id)
    if row is None:
        raise ValueError("notification route not found")
    return _public_notification_route_row(project, row)


def public_notification_routes(project: Any) -> list[dict[str, Any]]:
    return [
        _public_notification_route_row(project, row)
        for row in project.notification_routes()
    ]


def notification_digest_run(project: Any, digest_run_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_digest_runs WHERE id=?",
        (int(digest_run_id),),
    ).fetchone()


def notification_digest_run_by_route_window(
    project: Any,
    *,
    route_id: int,
    window_key: str,
) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_digest_runs WHERE route_id=? AND window_key=?",
        (int(route_id), str(window_key)),
    ).fetchone()


def create_notification_digest_run(
    project: Any,
    *,
    route_id: int,
    channel_id: int,
    cadence: str,
    window_key: str,
    window_start_at: str,
    window_end_at: str,
    status: str = "composed",
    item_count: int = 0,
) -> dict[str, Any]:
    project.db.execute(
        "INSERT OR IGNORE INTO notification_digest_runs "
        "(route_id, channel_id, cadence, window_key, window_start_at, "
        "window_end_at, status, item_count) VALUES (?,?,?,?,?,?,?,?)",
        (
            int(route_id),
            int(channel_id),
            cadence,
            window_key,
            window_start_at,
            window_end_at,
            status,
            int(item_count),
        ),
    )
    project.db.commit()
    row = project.notification_digest_run_by_route_window(
        route_id=route_id,
        window_key=window_key,
    )
    if row is None:
        raise ValueError("notification digest run could not be created")
    return _public_notification_digest_run_row(project, row)


def set_notification_digest_run_result(
    project: Any,
    digest_run_id: int,
    *,
    status: str,
    item_count: int,
    delivery_request_id: int | None = None,
) -> dict[str, Any]:
    project.db.execute(
        "UPDATE notification_digest_runs SET status=?, item_count=?, "
        "delivery_request_id=COALESCE(?, delivery_request_id), "
        "updated_at=datetime('now') WHERE id=?",
        (status, int(item_count), delivery_request_id, int(digest_run_id)),
    )
    project.db.commit()
    return project.public_notification_digest_run(digest_run_id)


def public_notification_digest_run(project: Any, digest_run_id: int) -> dict[str, Any]:
    row = project.notification_digest_run(digest_run_id)
    if row is None:
        raise ValueError("notification digest run not found")
    return _public_notification_digest_run_row(project, row)


def add_notification_digest_items(
    project: Any,
    digest_run_id: int,
    notification_ids: list[int],
) -> None:
    project.db.executemany(
        "INSERT OR IGNORE INTO notification_digest_items "
        "(digest_run_id, notification_id) VALUES (?, ?)",
        [
            (int(digest_run_id), int(notification_id))
            for notification_id in notification_ids
        ],
    )
    project.db.commit()


def notification_digest_items(
    project: Any,
    digest_run_id: int,
) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT i.* FROM notification_digest_items di "
        "JOIN notification_items i ON i.id=di.notification_id "
        "WHERE di.digest_run_id=? ORDER BY i.created_at ASC, i.id ASC",
        (int(digest_run_id),),
    ).fetchall()


def notification_digest_item_ids(project: Any, digest_run_id: int) -> list[int]:
    return [
        int(row["notification_id"])
        for row in project.db.execute(
            "SELECT notification_id FROM notification_digest_items "
            "WHERE digest_run_id=? ORDER BY notification_id ASC",
            (int(digest_run_id),),
        )
    ]


def create_notification_delivery_request(
    project: Any,
    *,
    route_id: int | None,
    channel_id: int,
    notification_id: int | None = None,
    digest_run_id: int | None = None,
    delivery_kind: str,
    dedupe_key: str,
    status: str = "queued",
    last_error: str | None = None,
) -> dict[str, Any]:
    project.db.execute(
        "INSERT OR IGNORE INTO notification_delivery_requests "
        "(route_id, channel_id, notification_id, digest_run_id, delivery_kind, "
        "dedupe_key, status, last_error) VALUES (?,?,?,?,?,?,?,?)",
        (
            route_id,
            int(channel_id),
            notification_id,
            digest_run_id,
            delivery_kind,
            dedupe_key,
            status,
            last_error,
        ),
    )
    project.db.commit()
    row = project.db.execute(
        "SELECT id FROM notification_delivery_requests WHERE dedupe_key=?",
        (dedupe_key,),
    ).fetchone()
    if row is None:
        raise ValueError("notification delivery request could not be created")
    return project.public_notification_delivery_request(int(row["id"]))


def notification_delivery_request(project: Any, request_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM notification_delivery_requests WHERE id=?",
        (int(request_id),),
    ).fetchone()


def list_notification_delivery_requests(
    project: Any,
    *,
    status: str | None = None,
    route_id: int | None = None,
    channel_id: int | None = None,
    notification_id: int | None = None,
    visible_route_ids: Collection[int] | None = None,
    visible_channel_ids: Collection[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[sqlite3.Row]:
    where, params = _notification_delivery_request_filters(
        project,
        status=status,
        route_id=route_id,
        channel_id=channel_id,
        notification_id=notification_id,
        visible_route_ids=visible_route_ids,
        visible_channel_ids=visible_channel_ids,
    )
    return project.db.execute(
        "SELECT * FROM notification_delivery_requests "
        f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    ).fetchall()


def notification_delivery_requests_total(
    project: Any,
    *,
    status: str | None = None,
    route_id: int | None = None,
    channel_id: int | None = None,
    notification_id: int | None = None,
    visible_route_ids: Collection[int] | None = None,
    visible_channel_ids: Collection[int] | None = None,
) -> int:
    where, params = _notification_delivery_request_filters(
        project,
        status=status,
        route_id=route_id,
        channel_id=channel_id,
        notification_id=notification_id,
        visible_route_ids=visible_route_ids,
        visible_channel_ids=visible_channel_ids,
    )
    row = project.db.execute(
        f"SELECT COUNT(*) AS count FROM notification_delivery_requests WHERE {where}",
        params,
    ).fetchone()
    return int(row["count"] if row else 0)


def public_notification_delivery_request(
    project: Any, request_id: int
) -> dict[str, Any]:
    row = project.notification_delivery_request(request_id)
    if row is None:
        raise ValueError("notification delivery request not found")
    return _public_notification_delivery_request_row(project, row)


def set_notification_delivery_request_job_id(
    project: Any, request_id: int, job_id: int
) -> dict[str, Any]:
    project.db.execute(
        "UPDATE notification_delivery_requests SET job_id=?, "
        "updated_at=datetime('now') WHERE id=?",
        (int(job_id), int(request_id)),
    )
    project.db.commit()
    return project.public_notification_delivery_request(request_id)


def claim_notification_delivery_request(
    project: Any, request_id: int, *, job_id: int | None = None
) -> dict[str, Any] | None:
    params: list[Any] = [int(request_id)]
    job_clause = ""
    if job_id is not None:
        job_clause = " AND (job_id IS NULL OR job_id=?)"
        params.append(int(job_id))
    cur = project.db.execute(
        "UPDATE notification_delivery_requests SET status='processing', "
        "job_id=COALESCE(job_id, ?), updated_at=datetime('now') "
        "WHERE id=? AND status IN ('queued', 'processing')"
        f"{job_clause}",
        ([job_id, *params] if job_id is not None else [None, *params]),
    )
    project.db.commit()
    if cur.rowcount <= 0:
        return None
    return project.public_notification_delivery_request(request_id)


def mark_notification_delivery_request_queued(
    project: Any, request_id: int, *, last_error: str | None = None
) -> dict[str, Any]:
    project.db.execute(
        "UPDATE notification_delivery_requests SET status='queued', "
        "last_error=COALESCE(?, last_error), updated_at=datetime('now') "
        "WHERE id=? AND status='processing'",
        (last_error, int(request_id)),
    )
    project.db.commit()
    return project.public_notification_delivery_request(request_id)


def record_notification_delivery_request_error(
    project: Any,
    request_id: int,
    *,
    last_error: str | None,
) -> dict[str, Any]:
    project.db.execute(
        "UPDATE notification_delivery_requests SET last_error=?, "
        "updated_at=datetime('now') WHERE id=?",
        (last_error, int(request_id)),
    )
    project.db.commit()
    return project.public_notification_delivery_request(request_id)


def terminalize_notification_delivery_request(
    project: Any,
    request_id: int,
    *,
    status: str,
    last_error: str | None = None,
    provider_ref: str | None = None,
) -> dict[str, Any]:
    project.db.execute(
        "UPDATE notification_delivery_requests SET status=?, last_error=?, "
        "provider_ref=COALESCE(?, provider_ref), "
        "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END, "
        "updated_at=datetime('now') WHERE id=?",
        (status, last_error, provider_ref, status, int(request_id)),
    )
    project.db.commit()
    return project.public_notification_delivery_request(request_id)


def processing_notification_delivery_requests(project: Any) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT * FROM notification_delivery_requests "
        "WHERE status='processing' AND job_id IS NOT NULL ORDER BY id"
    ).fetchall()


def record_notification_delivery_attempt(
    project: Any,
    *,
    delivery_request_id: int | None = None,
    notification_id: int | None,
    channel_id: int,
    status: str,
    provider_ref: str | None = None,
    error: str | None = None,
    response_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    notification_param = int(notification_id) if notification_id is not None else None
    row = project.db.execute(
        "SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt "
        "FROM notification_delivery_attempts "
        "WHERE COALESCE(notification_id, 0)=COALESCE(?, 0) AND channel_id=? "
        "AND COALESCE(delivery_request_id, 0)=COALESCE(?, 0)",
        (notification_param, int(channel_id), delivery_request_id),
    ).fetchone()
    attempt = int(row["next_attempt"] if row else 1)
    cur = project.db.execute(
        "INSERT INTO notification_delivery_attempts "
        "(delivery_request_id, notification_id, channel_id, status, attempt, "
        "provider_ref, error, response_meta_json, sent_at) "
        "VALUES (?,?,?,?,?,?,?,?, CASE WHEN ?='sent' THEN datetime('now') ELSE NULL END)",
        (
            delivery_request_id,
            notification_param,
            int(channel_id),
            status,
            attempt,
            provider_ref,
            error,
            json.dumps(response_meta or {}, sort_keys=True),
            status,
        ),
    )
    project.db.commit()
    return project.notification_delivery_attempt(int(cur.lastrowid))


def notification_delivery_attempt(project: Any, attempt_id: int) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT a.*, c.kind AS channel_kind FROM notification_delivery_attempts a "
        "JOIN notification_channels c ON c.id=a.channel_id WHERE a.id=?",
        (int(attempt_id),),
    ).fetchone()
    if row is None:
        raise ValueError("notification delivery attempt not found")
    return {
        "id": int(row["id"]),
        "delivery_request_id": (
            int(row["delivery_request_id"])
            if row["delivery_request_id"] is not None
            else None
        ),
        "notification_id": (
            int(row["notification_id"]) if row["notification_id"] is not None else None
        ),
        "channel_id": int(row["channel_id"]),
        "kind": row["channel_kind"],
        "status": row["status"],
        "attempt": int(row["attempt"]),
        "provider_ref": row["provider_ref"],
        "error": row["error"],
        "response_meta": _loads_json_object(row["response_meta_json"]),
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
    }


def _notification_filters(
    project: Any,
    *,
    actor_id: str,
    state: str,
    source_kind: str | None,
    source_ref: dict[str, Any] | None,
    severity: str | None,
    visible_delivery_route_ids: Collection[int] | None,
    visible_delivery_channel_ids: Collection[int] | None,
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if state != "all":
        clauses.append("COALESCE(s.state, 'unseen')=?")
        params.append(state)
    if source_kind:
        clauses.append("i.source_kind=?")
        params.append(source_kind)
    if severity:
        clauses.append("i.severity=?")
        params.append(severity)
    if source_ref:
        for key, value in source_ref.items():
            if not _safe_json_key(str(key)):
                clauses.append("0=1")
                continue
            clauses.append(f"json_extract(i.source_ref, '$.{key}')=?")
            params.append(value)
    visibility_clause, visibility_params = _notification_delivery_visibility_clause(
        "i",
        visible_delivery_route_ids=visible_delivery_route_ids,
        visible_delivery_channel_ids=visible_delivery_channel_ids,
    )
    if visibility_clause:
        clauses.append(visibility_clause)
        params.extend(visibility_params)
    return " AND ".join(clauses), params


def _notification_delivery_request_filters(
    project: Any,
    *,
    status: str | None,
    route_id: int | None,
    channel_id: int | None,
    notification_id: int | None,
    visible_route_ids: Collection[int] | None,
    visible_channel_ids: Collection[int] | None,
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if route_id is not None:
        clauses.append("route_id=?")
        params.append(int(route_id))
    if channel_id is not None:
        clauses.append("channel_id=?")
        params.append(int(channel_id))
    if notification_id is not None:
        clauses.append("notification_id=?")
        params.append(int(notification_id))
    visibility_clause, visibility_params = _delivery_request_visibility_clause(
        visible_route_ids=visible_route_ids,
        visible_channel_ids=visible_channel_ids,
    )
    if visibility_clause:
        clauses.append(visibility_clause)
        params.extend(visibility_params)
    return " AND ".join(clauses), params


def _select_notification_ids_for_state_change(
    project: Any,
    *,
    actor_id: str,
    notification_ids: list[int] | None,
    source_kind: str | None,
    source_ref: dict[str, Any] | None,
    before_created_at: str | None,
) -> list[int]:
    clauses = ["1=1"]
    params: list[Any] = []
    if notification_ids:
        placeholders = ",".join("?" for _ in notification_ids)
        clauses.append(f"i.id IN ({placeholders})")
        params.extend(int(value) for value in notification_ids)
    if source_kind:
        clauses.append("i.source_kind=?")
        params.append(source_kind)
    if source_ref:
        for key, value in source_ref.items():
            if not _safe_json_key(str(key)):
                clauses.append("0=1")
                continue
            clauses.append(f"json_extract(i.source_ref, '$.{key}')=?")
            params.append(value)
    if before_created_at:
        clauses.append("i.created_at < ?")
        params.append(before_created_at)
    where = " AND ".join(clauses)
    rows = project.db.execute(
        "SELECT i.id FROM notification_items i "
        "LEFT JOIN notification_actor_state s "
        "ON s.notification_id=i.id AND s.actor_id=? "
        f"WHERE {where} ORDER BY i.id",
        (actor_id, *params),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _public_notification_channel_row(project: Any, row: sqlite3.Row) -> dict[str, Any]:
    try:
        config = json.loads(row["config_json"] or "{}")
    except (TypeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    public_config = _public_notification_channel_config(str(row["kind"]), config)
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "owner_kind": str(config.get("owner_kind") or "project"),
        "config": public_config,
        "has_secret": bool(row["secret_ref"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_notification_route_row(project: Any, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "owner_kind": row["owner_kind"],
        "owner_ref": row["owner_ref"],
        "recipient_actor_id": row["recipient_actor_id"],
        "channel_id": int(row["channel_id"]),
        "source_kind": row["source_kind"],
        "source_ref_match": _loads_json_object(row["source_ref_match_json"]),
        "event_kinds": _loads_json_list(row["event_kinds_json"]),
        "severity_min": row["severity_min"],
        "delivery_mode": row["delivery_mode"],
        "digest_cadence": row["digest_cadence"],
        "digest_timezone": row["digest_timezone"],
        "digest_anchor_time": row["digest_anchor_time"],
        "template_key": row["template_key"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_notification_digest_run_row(
    project: Any, row: sqlite3.Row
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "route_id": int(row["route_id"]),
        "channel_id": int(row["channel_id"]),
        "cadence": row["cadence"],
        "window_key": row["window_key"],
        "window_start_at": row["window_start_at"],
        "window_end_at": row["window_end_at"],
        "status": row["status"],
        "item_count": int(row["item_count"]),
        "delivery_request_id": (
            int(row["delivery_request_id"])
            if row["delivery_request_id"] is not None
            else None
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_notification_delivery_request_row(
    project: Any, row: sqlite3.Row
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "route_id": int(row["route_id"]) if row["route_id"] is not None else None,
        "channel_id": int(row["channel_id"]),
        "notification_id": (
            int(row["notification_id"]) if row["notification_id"] is not None else None
        ),
        "digest_run_id": (
            int(row["digest_run_id"]) if row["digest_run_id"] is not None else None
        ),
        "delivery_kind": row["delivery_kind"],
        "dedupe_key": row["dedupe_key"],
        "status": row["status"],
        "job_id": int(row["job_id"]) if row["job_id"] is not None else None,
        "available_at": row["available_at"],
        "last_error": row["last_error"],
        "provider_ref": row["provider_ref"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sent_at": row["sent_at"],
    }
