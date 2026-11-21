"""Notification projections for built-in source facts."""

from __future__ import annotations

from typing import Any

from frisket.notifications.candidates import NotificationCandidate
from frisket.notifications.emit import (
    LOCAL_ACTOR_ID,
    NotificationEmitResult,
    emit_notification_candidate,
    alert_events_dedupe_key,
)
from frisket.engine.store import Project
from frisket.alerts.events import decode_alert_check_event


def alert_check_notification_candidate(
    project: Project,
    *,
    alert_id: int,
    run_id: int,
    limit: int = 500,
) -> NotificationCandidate | None:
    alert = project.get_alert(alert_id)
    run = project.get_alert_check(run_id)
    if alert is None or run is None or int(run["watch_id"]) != int(alert_id):
        return None
    events = [
        decode_alert_check_event(row)
        for row in project.alert_check_events(int(run_id), offset=0, limit=limit)
    ]
    if not events:
        return None
    event_ids = sorted(int(event["id"]) for event in events)
    event_groups = list(
        project.db.execute(
            "SELECT event_kind,severity,COUNT(*) total FROM watch_run_events WHERE run_id=? GROUP BY event_kind,severity",
            (run_id,),
        )
    )
    event_kinds = sorted({str(event["event_kind"]) for event in event_groups})
    event_count = project.alert_check_events_total(int(run_id))
    alert_name = str(alert["name"])
    severity = _highest_severity(
        str(event["severity"] or "info") for event in event_groups
    )
    matched_rows = int(run["matched_rows"] or 0)
    new_rows = int(run["new_rows"] or 0)
    title = f"{alert_name} recorded {event_count} alert event"
    if event_count != 1:
        title += "s"
    summary = (
        f"Check #{int(run_id)} matched {matched_rows} rows, "
        f"including {new_rows} new rows."
    )
    if event_kinds:
        summary = f"{summary} Events: {', '.join(event_kinds)}."
    return {
        "source_kind": "watch",
        "source_ref": {"watch_id": int(alert_id), "run_id": int(run_id)},
        "source_event_ids": event_ids,
        "event_count": event_count,
        "dedupe_key": alert_events_dedupe_key(
            alert_id=int(alert_id),
            run_id=int(run_id),
            event_ids=event_ids,
        ),
        "title": title,
        "summary": summary,
        "severity": severity,  # type: ignore[typeddict-item]
        "deep_link": {
            "kind": "watch_run",
            "watch_id": int(alert_id),
            "run_id": int(run_id),
            "event_ids": event_ids,
        },
        "payload": {
            "watch_name": alert_name,
            "run_status": str(run["status"]),
            "matched_rows": matched_rows,
            "new_rows": new_rows,
            "event_kinds": event_kinds,
            "event_count": event_count,
        },
    }


def emit_alert_check_notification(
    project: Project,
    *,
    alert_id: int,
    run_id: int,
    actor_id: str = LOCAL_ACTOR_ID,
) -> NotificationEmitResult | None:
    candidate = alert_check_notification_candidate(
        project,
        alert_id=alert_id,
        run_id=run_id,
    )
    result = (
        emit_notification_candidate(project, candidate, actor_id=actor_id)
        if candidate is not None
        else None
    )
    # The observation transaction creates this obligation. Only clear it after
    # durable notification and delivery planning; recovery safely repeats either.
    project.db.execute(
        "UPDATE watch_runs SET notification_pending=0 WHERE id=? AND watch_id=?",
        (run_id, alert_id),
    )
    project.db.commit()
    return result


def _highest_severity(values: Any) -> str:
    order = {"info": 0, "warning": 1, "critical": 2}
    highest = "info"
    for value in values:
        severity = str(value).strip().lower()
        if order.get(severity, 0) > order[highest]:
            highest = severity
    return highest
