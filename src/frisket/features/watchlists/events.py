"""Watch run event vocabulary and serialization helpers."""

from __future__ import annotations

import json
from typing import Any

WATCH_RUN_EVENT_SCHEMA_VERSION = "frisket.watch_run_events_page.v1"
WATCH_EVENT_ROW_ENTERED = "row_entered"
WATCH_EVENT_ROW_EXITED = "row_exited"
WATCH_EVENT_COUNT_CHANGED = "count_changed"
WATCH_EVENT_THRESHOLD_CROSSED = "threshold_crossed"
WATCH_EVENT_ROW_CHANGED = "row_changed"

ALLOWED_EVENT_KINDS = {
    "row_entered",
    "row_exited",
    "row_still_matching",
    "row_changed",
    "row_hidden",
    "row_restored",
    "child_row_entered",
    "child_row_exited",
    "parent_gained_child",
    "parent_lost_child",
    "derived_table_changed",
    "entity_appeared",
    "entity_reappeared",
    "entity_count_changed",
    "entity_threshold_crossed",
    "entity_alias_matched",
    "count_changed",
    "threshold_crossed",
    "group_entered",
    "group_exited",
    "group_metric_changed",
    "geo_point_entered_shape",
    "geo_point_exited_shape",
    "geo_shape_intersected",
    "join_pair_entered",
    "join_pair_exited",
    "join_score_changed",
    "source_row_entered",
    "source_run_matched",
    "import_batch_matched",
    "review_state_changed",
    "confidence_threshold_crossed",
    "current_value_source_changed",
}

ALLOWED_SUBJECT_KINDS = {
    "row",
    "child_row",
    "parent_row",
    "entity",
    "entity_mention",
    "aggregate",
    "group",
    "join_pair",
    "geo_relation",
    "source_run",
    "import_batch",
    "review_target",
    "cell",
}


def row_entered_event(
    *,
    run_id: int,
    watch_id: int,
    sheet_id: int,
    row_id: int,
    rank: int,
    snippet: str | None,
    is_new: bool = True,
    reentered: bool = False,
) -> dict[str, Any]:
    after_json: dict[str, Any] = {"is_new": bool(is_new)}
    if reentered:
        after_json["reentered"] = True
    return {
        "run_id": int(run_id),
        "watch_id": int(watch_id),
        "event_kind": WATCH_EVENT_ROW_ENTERED,
        "subject_kind": "row",
        "subject_ref": {"sheet_id": int(sheet_id), "row_id": int(row_id)},
        "before_json": None,
        "after_json": after_json,
        "delta_json": None,
        "severity": "info",
        "rank": int(rank),
        "snippet": snippet,
    }


def row_exited_event(
    *,
    run_id: int,
    watch_id: int,
    sheet_id: int,
    row_id: int,
    rank: int,
) -> dict[str, Any]:
    return {
        "run_id": int(run_id),
        "watch_id": int(watch_id),
        "event_kind": WATCH_EVENT_ROW_EXITED,
        "subject_kind": "row",
        "subject_ref": {"sheet_id": int(sheet_id), "row_id": int(row_id)},
        "before_json": {"matched": True},
        "after_json": {"matched": False},
        "delta_json": {"membership_delta": -1},
        "severity": "info",
        "rank": int(rank),
        "snippet": None,
    }


def count_changed_event(
    *,
    run_id: int,
    watch_id: int,
    previous_run_id: int | None,
    previous_count: int,
    current_count: int,
    rank: int = 1,
) -> dict[str, Any]:
    return {
        "run_id": int(run_id),
        "watch_id": int(watch_id),
        "event_kind": WATCH_EVENT_COUNT_CHANGED,
        "subject_kind": "aggregate",
        "subject_ref": {"metric": "matched_rows"},
        "before_json": {
            "run_id": previous_run_id,
            "matched_rows": int(previous_count),
        },
        "after_json": {"run_id": int(run_id), "matched_rows": int(current_count)},
        "delta_json": {"count_delta": int(current_count) - int(previous_count)},
        "severity": "info",
        "rank": int(rank),
        "snippet": None,
    }


def threshold_crossed_event(
    *,
    run_id: int,
    watch_id: int,
    previous_run_id: int | None,
    previous_count: int,
    current_count: int,
    threshold: int,
    direction: str,
    crossing: str,
    rank: int = 1,
) -> dict[str, Any]:
    return {
        "run_id": int(run_id),
        "watch_id": int(watch_id),
        "event_kind": WATCH_EVENT_THRESHOLD_CROSSED,
        "subject_kind": "aggregate",
        "subject_ref": {"metric": "matched_rows", "threshold": int(threshold)},
        "before_json": {
            "run_id": previous_run_id,
            "matched_rows": int(previous_count),
        },
        "after_json": {"run_id": int(run_id), "matched_rows": int(current_count)},
        "delta_json": {
            "metric": "matched_rows",
            "threshold": int(threshold),
            "direction": direction,
            "crossing": crossing,
        },
        "severity": "info",
        "rank": int(rank),
        "snippet": None,
    }


def row_changed_event(
    *,
    run_id: int,
    watch_id: int,
    sheet_id: int,
    row_id: int,
    rank: int,
    changed_fields: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "run_id": int(run_id),
        "watch_id": int(watch_id),
        "event_kind": WATCH_EVENT_ROW_CHANGED,
        "subject_kind": "row",
        "subject_ref": {"sheet_id": int(sheet_id), "row_id": int(row_id)},
        "before_json": {
            "fields": [
                {
                    "sheet_id": field["sheet_id"],
                    "column_id": field["column_id"],
                    "name": field.get("name"),
                    "value": field.get("before"),
                }
                for field in changed_fields
            ]
        },
        "after_json": {
            "fields": [
                {
                    "sheet_id": field["sheet_id"],
                    "column_id": field["column_id"],
                    "name": field.get("name"),
                    "value": field.get("after"),
                }
                for field in changed_fields
            ]
        },
        "delta_json": {"changed_fields": changed_fields},
        "severity": "info",
        "rank": int(rank),
        "snippet": None,
    }


def event_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_watch_run_event(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["subject_ref"] = _loads_json(data.get("subject_ref"), {})
    for key in ("before_json", "after_json", "delta_json"):
        data[key] = _loads_json(data.get(key), None)
    return data


def _loads_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
