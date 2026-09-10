"""Notification projections for built-in source facts."""

from __future__ import annotations

from typing import Any

from frisket.server.notifications.candidates import NotificationCandidate
from frisket.server.notifications.service import (
    LOCAL_ACTOR_ID,
    NotificationEmitResult,
    emit_notification_candidate,
    watch_events_dedupe_key,
)
from frisket.engine.store import Project
from frisket.features.watchlists.events import decode_watch_run_event


def watch_run_notification_candidate(
    project: Project,
    *,
    watch_id: int,
    run_id: int,
    limit: int = 500,
) -> NotificationCandidate | None:
    watch = project.get_watch(watch_id)
    run = project.get_watch_run(run_id)
    if watch is None or run is None or int(run["watch_id"]) != int(watch_id):
        return None
    events = [
        decode_watch_run_event(row)
        for row in project.watch_run_events(int(run_id), offset=0, limit=limit)
    ]
    if not events:
        return None
    event_ids = sorted(int(event["id"]) for event in events)
    event_kinds = sorted({str(event["event_kind"]) for event in events})
    event_count = len(events)
    watch_name = str(watch["name"])
    severity = _highest_severity(
        str(event.get("severity") or "info") for event in events
    )
    matched_rows = int(run["matched_rows"] or 0)
    new_rows = int(run["new_rows"] or 0)
    title = f"{watch_name} recorded {event_count} watch event"
    if event_count != 1:
        title += "s"
    summary = (
        f"Run #{int(run_id)} matched {matched_rows} rows, "
        f"including {new_rows} new rows."
    )
    if event_kinds:
        summary = f"{summary} Events: {', '.join(event_kinds)}."
    return {
        "source_kind": "watch",
        "source_ref": {"watch_id": int(watch_id), "run_id": int(run_id)},
        "source_event_ids": event_ids,
        "dedupe_key": watch_events_dedupe_key(
            watch_id=int(watch_id),
            run_id=int(run_id),
            event_ids=event_ids,
        ),
        "title": title,
        "summary": summary,
        "severity": severity,  # type: ignore[typeddict-item]
        "deep_link": {
            "kind": "watch_run",
            "watch_id": int(watch_id),
            "run_id": int(run_id),
            "event_ids": event_ids,
        },
        "payload": {
            "watch_name": watch_name,
            "run_status": str(run["status"]),
            "matched_rows": matched_rows,
            "new_rows": new_rows,
            "event_kinds": event_kinds,
        },
    }


def emit_watch_run_notification(
    project: Project,
    *,
    watch_id: int,
    run_id: int,
    actor_id: str = LOCAL_ACTOR_ID,
) -> NotificationEmitResult | None:
    candidate = watch_run_notification_candidate(
        project,
        watch_id=watch_id,
        run_id=run_id,
    )
    if candidate is None:
        return None
    return emit_notification_candidate(project, candidate, actor_id=actor_id)


def _highest_severity(values: Any) -> str:
    order = {"info": 0, "warning": 1, "critical": 2}
    highest = "info"
    for value in values:
        severity = str(value).strip().lower()
        if order.get(severity, 0) > order[highest]:
            highest = severity
    return highest
