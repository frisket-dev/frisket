from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.server.notifications.candidates import validate_notification_candidate
from frisket.features.watchlists.specs import (
    QUERY_SPEC_VERSION,
    WATCH_SPEC_VERSION,
    normalize_detection_policy,
    normalize_lens_spec,
    normalize_query_spec,
    normalize_watch_spec,
    query_spec_hash,
)
from http_test_helpers import post_row_add_as_v1_action


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Watchlists v2"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return pid, int(response.json()["sheet_id"])


def _create_fts_watch(
    client: TestClient, pid: str, *, limit: int = 10
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Budget watch",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget", "limit": limit},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _run_watch(client: TestClient, pid: str, watch_id: int) -> dict[str, Any]:
    response = client.post(f"/api/projects/{pid}/watches/{watch_id}/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.watch_run.v1"
    return body


def test_query_lens_watch_and_notification_candidate_helpers_are_versioned() -> None:
    fts_query = normalize_query_spec(
        {"kind": "fts", "q": "budget", "limit": 25},
        scope={"kind": "project"},
    )
    assert fts_query == {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": "search.fts",
        "scope": {"kind": "project"},
        "q": "budget",
        "mode": "keyword",
        "rerank": "off",
        "limit": 25,
    }
    assert query_spec_hash({"limit": 25, **fts_query}) == query_spec_hash(fts_query)

    filter_query = normalize_query_spec(
        {
            "kind": "filter",
            "sheet_id": 7,
            "filter": {"status": {"eq": "open"}},
            "sort": [{"dir": "asc", "column": "title"}],
        },
        scope={"kind": "sheet", "sheet_id": 7},
    )
    assert filter_query == {
        "schema_version": QUERY_SPEC_VERSION,
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": 7},
        "filter": {"status": {"eq": "open"}},
        "sort": [{"column": "title", "dir": "asc"}],
    }
    assert normalize_lens_spec(filter_query)["query"] == filter_query

    policy = normalize_detection_policy(None)
    assert policy == {"kind": "new_matches"}
    watch_spec = normalize_watch_spec(
        filter_query,
        detection_policy=policy,
    )
    assert watch_spec["schema_version"] == WATCH_SPEC_VERSION
    assert watch_spec["query"] == filter_query
    assert watch_spec["query_hash"].startswith("sha256:")
    assert watch_spec["detection_policy"] == {"kind": "new_matches"}

    candidate = validate_notification_candidate(
        {
            "source_kind": "watch",
            "source_ref": {"watch_id": 1, "run_id": 2},
            "source_event_ids": [3],
            "dedupe_key": "watch:1:run:2",
            "title": "Budget watch found 1 new row",
            "summary": '1 row newly matched Search "budget".',
            "severity": "info",
            "deep_link": {"kind": "watch_run", "watch_id": 1, "run_id": 2},
            "payload": {},
        }
    )
    assert candidate["source_kind"] == "watch"
    assert candidate["source_event_ids"] == [3]

    with pytest.raises(ValueError, match="dedupe_key"):
        validate_notification_candidate(
            {
                "source_kind": "watch",
                "source_ref": {"watch_id": 1},
                "dedupe_key": "",
                "title": "Missing key",
                "summary": "Bad candidate",
            }
        )


def test_watch_runs_record_row_entered_events_and_keep_mvp_tables(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    watch = _create_fts_watch(client, pid)

    project = client.app.state.workspace.get(pid)
    stored_watch = project.get_watch(watch["id"])
    assert stored_watch is not None
    assert stored_watch["query_version"] == QUERY_SPEC_VERSION
    assert stored_watch["query_hash"].startswith("sha256:")
    assert json.loads(stored_watch["detection_policy"]) == {"kind": "new_matches"}

    first = _run_watch(client, pid, watch["id"])
    assert first["run"]["matched_rows"] == 2
    assert first["run"]["new_rows"] == 2
    assert [hit["is_new"] for hit in first["hits"]] == [True, True]

    route = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{first['run']['id']}/events",
        params={"offset": 0, "limit": 1},
    )
    assert route.status_code == 200, route.text
    events_page = route.json()
    assert events_page["schema_version"] == "frisket.watch_run_events_page.v1"
    assert events_page["order"] == "asc"
    assert events_page["offset"] == 0
    assert events_page["limit"] == 1
    assert events_page["total"] == 2
    assert events_page["has_more"] is True
    assert events_page["next_offset"] == 1
    assert len(events_page["events"]) == 1
    event = events_page["events"][0]
    assert event["event_kind"] == "row_entered"
    assert event["subject_kind"] == "row"
    assert event["subject_ref"]["sheet_id"] == sheet_id
    assert event["subject_ref"]["row_id"] in {hit["row_id"] for hit in first["hits"]}
    assert event["after_json"] == {"is_new": True}
    assert event["before_json"] is None
    assert event["delta_json"] is None
    assert event["severity"] == "info"
    assert event["rank"] == 1
    assert event["snippet"]

    filtered = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{first['run']['id']}/events",
        params={"event_kind": "row_entered", "limit": 10},
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 2

    no_such_kind = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{first['run']['id']}/events",
        params={"event_kind": "row_exited", "limit": 10},
    )
    assert no_such_kind.status_code == 200, no_such_kind.text
    assert no_such_kind.json()["total"] == 0

    second = _run_watch(client, pid, watch["id"])
    assert second["run"]["new_rows"] == 0
    second_events = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{second['run']['id']}/events"
    )
    assert second_events.status_code == 200, second_events.text
    assert second_events.json()["total"] == 0

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
    third = _run_watch(client, pid, watch["id"])
    assert third["run"]["new_rows"] == 1
    third_events = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{third['run']['id']}/events"
    )
    assert third_events.status_code == 200, third_events.text
    assert third_events.json()["total"] == 1

    tables = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "watches",
        "watch_runs",
        "watch_run_hits",
        "watch_seen_rows",
        "watch_run_events",
    }.issubset(tables)
    indexes = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert {
        "idx_watch_run_events_run_rank",
        "idx_watch_run_events_watch_id",
        "idx_watch_run_events_kind",
    }.issubset(indexes)


def test_watch_run_events_route_rejects_run_from_another_watch(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    first_watch = _create_fts_watch(client, pid)
    second_watch = _create_fts_watch(client, pid, limit=5)
    run = _run_watch(client, pid, first_watch["id"])

    wrong_watch = client.get(
        f"/api/projects/{pid}/watches/{second_watch['id']}/runs/{run['run']['id']}/events"
    )
    assert wrong_watch.status_code == 404, wrong_watch.text
    assert "watch run not found" in wrong_watch.json()["detail"]
