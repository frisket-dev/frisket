from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import make_client as _client
from http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_row_add_as_v1_action,
)


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Watch policies"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return pid, int(response.json()["sheet_id"])


def _sheet_snapshot(
    client: TestClient, pid: str, sheet_id: int
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    response = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data")
    assert response.status_code == 200, response.text
    body = response.json()
    columns = {col["name"]: int(col["id"]) for col in body["columns"]}
    by_title = {
        row["cells"][str(columns["title"])]: {
            "id": int(row["id"]),
            "cells": row["cells"],
        }
        for row in body["rows"]
    }
    return columns, by_title


def _create_filter_watch(
    client: TestClient,
    pid: str,
    sheet_id: int,
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": f"{policy['kind']} watch",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": {"status": {"eq": "open"}},
                "sort": [{"column": "title", "dir": "asc"}],
            },
            "detection_policy": policy,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["detection_policy"] == policy
    return body


def _run_watch(client: TestClient, pid: str, watch_id: int) -> dict[str, Any]:
    response = client.post(f"/api/projects/{pid}/watches/{watch_id}/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.watch_run.v1"
    return body


def _events(
    client: TestClient,
    pid: str,
    watch_id: int,
    run_id: int,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    params = {"limit": 50}
    if kind is not None:
        params["event_kind"] = kind
    response = client.get(
        f"/api/projects/{pid}/watches/{watch_id}/runs/{run_id}/events",
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()["events"]


def test_membership_changed_emits_entered_and_exited_rows(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    columns, by_title = _sheet_snapshot(client, pid, sheet_id)
    watch = _create_filter_watch(
        client,
        pid,
        sheet_id,
        policy={"kind": "membership_changed"},
    )

    first = _run_watch(client, pid, watch["id"])
    assert first["run"]["matched_rows"] == 2
    first_events = _events(client, pid, watch["id"], first["run"]["id"])
    assert [event["event_kind"] for event in first_events] == [
        "row_entered",
        "row_entered",
    ]
    assert {event["after_json"]["is_new"] for event in first_events} == {True}

    edit = post_cell_edit_as_v1_action(
        client,
        pid,
        [
            {
                "row_id": by_title["Budget hearing"]["id"],
                "column_id": columns["status"],
                "value": "closed",
            }
        ],
    )
    assert edit.status_code == 200, edit.text
    added = post_row_add_as_v1_action(
        client,
        pid,
        sheet_id,
        {
            "title": "Budget amendment",
            "status": "open",
            "body": "A late budget amendment arrived.",
        },
    )
    assert added.status_code == 200, added.text

    second = _run_watch(client, pid, watch["id"])
    assert second["run"]["matched_rows"] == 2
    assert second["run"]["new_rows"] == 1
    second_events = _events(client, pid, watch["id"], second["run"]["id"])
    assert [event["event_kind"] for event in second_events] == [
        "row_entered",
        "row_exited",
    ]
    entered = next(
        event for event in second_events if event["event_kind"] == "row_entered"
    )
    exited = next(
        event for event in second_events if event["event_kind"] == "row_exited"
    )
    assert entered["after_json"] == {"is_new": True}
    assert exited["subject_ref"] == {
        "sheet_id": sheet_id,
        "row_id": by_title["Budget hearing"]["id"],
    }
    assert exited["before_json"]["matched"] is True
    assert exited["after_json"]["matched"] is False


def test_count_and_threshold_events_use_approved_first_run_baseline(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    count_watch = _create_filter_watch(
        client,
        pid,
        sheet_id,
        policy={"kind": "count_changed"},
    )
    threshold_watch = _create_filter_watch(
        client,
        pid,
        sheet_id,
        policy={
            "kind": "threshold_crossed",
            "metric": "matched_rows",
            "threshold": 2,
            "direction": "at_or_above",
        },
    )

    first_count = _run_watch(client, pid, count_watch["id"])
    count_events = _events(client, pid, count_watch["id"], first_count["run"]["id"])
    assert [event["event_kind"] for event in count_events] == ["count_changed"]
    assert count_events[0]["before_json"]["matched_rows"] == 0
    assert count_events[0]["after_json"]["matched_rows"] == 2
    assert count_events[0]["delta_json"]["count_delta"] == 2

    unchanged_count = _run_watch(client, pid, count_watch["id"])
    assert _events(client, pid, count_watch["id"], unchanged_count["run"]["id"]) == []

    first_threshold = _run_watch(client, pid, threshold_watch["id"])
    threshold_events = _events(
        client, pid, threshold_watch["id"], first_threshold["run"]["id"]
    )
    assert [event["event_kind"] for event in threshold_events] == ["threshold_crossed"]
    assert threshold_events[0]["before_json"]["matched_rows"] == 0
    assert threshold_events[0]["after_json"]["matched_rows"] == 2
    assert threshold_events[0]["delta_json"] == {
        "metric": "matched_rows",
        "threshold": 2,
        "direction": "at_or_above",
        "crossing": "up",
    }


def test_row_changed_is_bounded_to_configured_fields_and_not_first_observation(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    columns, by_title = _sheet_snapshot(client, pid, sheet_id)
    watch = _create_filter_watch(
        client,
        pid,
        sheet_id,
        policy={
            "kind": "row_changed",
            "fields": [
                {
                    "sheet_id": sheet_id,
                    "column_id": columns["body"],
                    "name": "body",
                }
            ],
        },
    )

    first = _run_watch(client, pid, watch["id"])
    assert first["run"]["matched_rows"] == 2
    assert _events(client, pid, watch["id"], first["run"]["id"]) == []

    project = client.app.state.workspace.get(pid)
    state_rows = project.db.execute(
        "SELECT watch_id, sheet_id, row_id, column_id FROM watch_row_field_state"
    ).fetchall()
    assert len(state_rows) == 2
    assert {row["column_id"] for row in state_rows} == {columns["body"]}

    edit = post_cell_edit_as_v1_action(
        client,
        pid,
        [
            {
                "row_id": by_title["Budget audit"]["id"],
                "column_id": columns["body"],
                "value": "Auditors found larger budget variances.",
            },
            {
                "row_id": by_title["Budget hearing"]["id"],
                "column_id": columns["title"],
                "value": "Budget hearing renamed",
            },
        ],
    )
    assert edit.status_code == 200, edit.text

    second = _run_watch(client, pid, watch["id"])
    changed_events = _events(client, pid, watch["id"], second["run"]["id"])
    assert [event["event_kind"] for event in changed_events] == ["row_changed"]
    changed = changed_events[0]
    assert changed["subject_ref"] == {
        "sheet_id": sheet_id,
        "row_id": by_title["Budget audit"]["id"],
    }
    assert changed["delta_json"]["changed_fields"] == [
        {
            "sheet_id": sheet_id,
            "column_id": columns["body"],
            "name": "body",
            "before": "Auditors found budget variances.",
            "after": "Auditors found larger budget variances.",
        }
    ]

    added = post_row_add_as_v1_action(
        client,
        pid,
        sheet_id,
        {
            "title": "Budget memo",
            "status": "open",
            "body": "First observation should seed state only.",
        },
    )
    assert added.status_code == 200, added.text
    third = _run_watch(client, pid, watch["id"])
    assert _events(client, pid, watch["id"], third["run"]["id"]) == []


def test_detection_policy_validation_rejects_unbounded_or_bad_combinations(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)

    empty_fields = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "bad row changed",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": {"status": {"eq": "open"}},
            },
            "detection_policy": {"kind": "row_changed", "fields": []},
        },
    )
    assert empty_fields.status_code == 400, empty_fields.text
    assert "fields" in empty_fields.json()["detail"]

    bad_metric = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "bad threshold",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": {"status": {"eq": "open"}},
            },
            "detection_policy": {
                "kind": "threshold_crossed",
                "metric": "tokens",
                "threshold": 1,
            },
        },
    )
    assert bad_metric.status_code == 400, bad_metric.text
    assert "matched_rows" in bad_metric.json()["detail"]
