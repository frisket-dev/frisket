from __future__ import annotations

from tests.alert_helpers import post_alert

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client
from http_test_helpers import post_row_add_as_v1_action
from frisket.search_index import drain_index


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Watchlists"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert response.status_code == 200, response.text
    drain_index(client.app.state.workspace.get(pid))
    return pid, int(response.json()["sheet_id"])


def _create_fts_alert(client: TestClient, pid: str, *, sheet_id: int | None = None):
    scope: dict[str, Any] = {"kind": "project"}
    if sheet_id is not None:
        scope = {"kind": "sheet", "sheet_id": sheet_id}
    response = post_alert(
        client,
        f"/api/projects/{pid}/alerts",
        json={
            "name": "Budget watch",
            "scope": scope,
            "query": {"kind": "fts", "q": "budget", "limit": 10},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _check_alert(client: TestClient, pid: str, alert_id: int) -> dict[str, Any]:
    response = client.post(f"/api/projects/{pid}/alerts/{alert_id}/check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.alert_check.v1"
    return body


def test_fts_alert_manual_runs_track_matched_and_new_rows(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)

    alert = _create_fts_alert(client, pid)
    view = client.get(f"/api/projects/{pid}/views/{alert['view_id']}").json()
    assert view["spec"]["selection"]["search"] == {"text": "budget"}
    assert alert["latest_run"] is None

    first = _check_alert(client, pid, alert["id"])
    assert first["run"]["matched_rows"] == 2
    assert first["run"]["new_rows"] == 0
    assert {hit["run_id"] for hit in first["hits"]} == {first["run"]["id"]}
    assert [hit["is_new"] for hit in first["hits"]] == [False, False]
    assert {hit["sheet_id"] for hit in first["hits"]} == {sheet_id}
    assert all(hit["snippet"] is None for hit in first["hits"])

    second = _check_alert(client, pid, alert["id"])
    assert second["run"]["matched_rows"] == 2
    assert second["run"]["new_rows"] == 0
    assert [hit["is_new"] for hit in second["hits"]] == [False, False]

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
    drain_index(client.app.state.workspace.get(pid))

    third = _check_alert(client, pid, alert["id"])
    assert third["run"]["matched_rows"] == 3
    assert third["run"]["new_rows"] == 1
    assert sum(1 for hit in third["hits"] if hit["is_new"]) == 1

    listing = client.get(f"/api/projects/{pid}/alerts")
    assert listing.status_code == 200, listing.text
    listed = listing.json()
    assert listed[0]["latest_run"]["id"] == third["run"]["id"]
    assert listed[0]["latest_run"]["new_rows"] == 1
    assert listed[0]["last_evaluated_change"] > 0

    page = client.get(
        f"/api/projects/{pid}/alerts/{alert['id']}/checks",
        params={"offset": 1, "limit": 1},
    )
    assert page.status_code == 200, page.text
    runs_page = page.json()
    assert runs_page["schema_version"] == "frisket.alert_checks_page.v1"
    assert runs_page["order"] == "desc"
    assert runs_page["offset"] == 1
    assert runs_page["limit"] == 1
    assert runs_page["total"] == 3
    assert runs_page["has_more"] is True
    assert runs_page["next_offset"] == 2
    assert len(runs_page["runs"]) == 1
    assert runs_page["hits_limit"] == 20
    assert len(runs_page["runs"][0]["hits"]) <= 20

    hit_limited_page = client.get(
        f"/api/projects/{pid}/alerts/{alert['id']}/checks",
        params={"offset": 0, "limit": 1, "hits_limit": 1},
    )
    assert hit_limited_page.status_code == 200, hit_limited_page.text
    latest_run_page = hit_limited_page.json()
    assert latest_run_page["hits_limit"] == 1
    assert len(latest_run_page["runs"][0]["hits"]) == 1
    assert latest_run_page["runs"][0]["hits_limit"] == 1
    assert latest_run_page["runs"][0]["hits_truncated"] is True


def test_filter_alert_owns_captured_membership_and_uses_shared_tables(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)

    alert_response = post_alert(
        client,
        f"/api/projects/{pid}/alerts",
        json={
            "name": "Open rows watch",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": {"status": {"eq": "open"}},
                "sort": [{"column": "title", "dir": "asc"}],
            },
        },
    )
    assert alert_response.status_code == 200, alert_response.text
    alert = alert_response.json()
    assert alert["sheet_id"] == sheet_id
    view = client.get(f"/api/projects/{pid}/views/{alert['view_id']}").json()
    assert view["spec"]["selection"]["scope"] == {"kind": "sheet", "sheet_id": sheet_id}
    run = _check_alert(client, pid, alert["id"])
    assert run["run"]["matched_rows"] == 2
    assert run["run"]["new_rows"] == 0
    assert [hit["snippet"] for hit in run["hits"]] == [None, None]

    project = client.app.state.workspace.get(pid)
    tables = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"alerts", "alert_checks", "alert_check_hits", "alert_seen_rows"}.issubset(
        tables
    )
    indexes = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert {
        "idx_alert_checks_alert_started",
        "idx_alert_check_hits_run_rank",
        "idx_alert_seen_rows_alert",
    }.issubset(indexes)


def test_fts_alert_preserves_501_requested_matches_through_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The 500-hit response page must not cap evaluation or durable matches."""
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Large watch"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("Documents")
    text_column_id = project.add_column(sheet_id, "text", type="text")
    project.add_rows(
        sheet_id,
        [{"text": f"needle {index}"} for index in range(501)],
        {"text": text_column_id},
    )

    drain_index(project)
    created = post_alert(
        client,
        f"/api/projects/{pid}/alerts",
        json={
            "name": "All needles",
            "scope": {"kind": "project"},
            "query": {
                "kind": "fts",
                "q": "needle",
                "rerank": "off",
                "limit": 501,
            },
        },
    )
    assert created.status_code == 200, created.text
    alert = created.json()

    result = _check_alert(client, pid, alert["id"])
    run_id = result["run"]["id"]
    persisted = project.db.execute(
        "SELECT COUNT(*) AS count FROM alert_check_hits WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert (
        client.get(f"/api/projects/{pid}/views/{alert['view_id']}").json()["spec"][
            "selection"
        ]["limit"]
        == 501
    )
    assert result["run"]["matched_rows"] == 501
    assert result["run"]["new_rows"] == 0
    assert persisted["count"] == 500
    assert (
        project.db.execute(
            "SELECT count(*) FROM alert_seen_rows WHERE alert_id=?", (alert["id"],)
        ).fetchone()[0]
        == 501
    )
    assert len(result["hits"]) <= 500


@pytest.mark.parametrize(
    "query",
    [
        {
            "kind": "embedding_similarity",
            "sheet_id": 7,
            "embedding_index_id": "embidx_test",
            "anchor": {"kind": "row", "row_id": 11},
            "limit": 501,
        },
        {
            "kind": "embedding_hybrid",
            "sheet_id": 7,
            "embedding_index_id": "embidx_test",
            "text": "needle",
            "limit": 501,
        },
    ],
    ids=["embedding_similarity", "embedding_hybrid"],
)
def test_embedding_query_normalization_preserves_501_requested_limit(
    query: dict[str, Any],
) -> None:
    from frisket.contracts.query_spec import normalize_query_spec

    assert normalize_query_spec(query)["limit"] == 501


def test_alert_patch_pause_resume_preserves_execution_state_and_manual_runs(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    alert = _create_fts_alert(client, pid)
    first_run = _check_alert(client, pid, alert["id"])["run"]
    before = client.get(f"/api/projects/{pid}/alerts").json()[0]

    renamed = client.patch(
        f"/api/projects/{pid}/alerts/{alert['id']}",
        json={"name": "  Budget alerts  "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Budget alerts"
    for field in (
        "view_id",
        "enabled",
        "last_evaluated_change",
        "last_run_id",
    ):
        assert renamed.json()[field] == before[field]

    paused = client.patch(
        f"/api/projects/{pid}/alerts/{alert['id']}", json={"enabled": False}
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False
    assert paused.json()["last_run_id"] == first_run["id"]
    assert paused.json()["last_evaluated_change"] == before["last_evaluated_change"]

    # Pause affects automatic selection only. A deliberate Run now remains a
    # manual evaluation and extends, rather than replaces, history.
    manual_while_paused = _check_alert(client, pid, alert["id"])["run"]
    assert manual_while_paused["id"] != first_run["id"]
    assert (
        client.get(f"/api/projects/{pid}/alerts/{alert['id']}/checks").json()["total"]
        == 2
    )

    paused_cursor = paused.json()["last_evaluated_change"]
    paused_history = manual_while_paused["id"]
    resumed = client.patch(
        f"/api/projects/{pid}/alerts/{alert['id']}", json={"enabled": True}
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["enabled"] is True
    assert resumed.json()["last_evaluated_change"] >= paused_cursor
    assert resumed.json()["last_run_id"] == paused_history
    # Resuming changes scheduling eligibility only; it never evaluates inline.
    assert (
        client.get(f"/api/projects/{pid}/alerts/{alert['id']}/checks").json()["total"]
        == 2
    )


def test_alert_delete_cascades_only_its_state_and_single_alert_notifications(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    deleted_alert = _create_fts_alert(client, pid)
    retained_alert = post_alert(
        client,
        f"/api/projects/{pid}/alerts",
        json={
            "name": "Retained watch",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "bridge", "limit": 10},
        },
    ).json()
    from alert_helpers import seed_empty_alert_baseline

    project = client.app.state.workspace.get(pid)
    seed_empty_alert_baseline(project, deleted_alert["id"])
    seed_empty_alert_baseline(project, retained_alert["id"])
    deleted_run = _check_alert(client, pid, deleted_alert["id"])["run"]
    retained_run = _check_alert(client, pid, retained_alert["id"])["run"]
    project = client.app.state.workspace.get(pid)

    channel = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "email",
            "name": "Watch email",
            "to": "alerts@example.com",
            "secret_ref": "env:RESEND_API_KEY",
        },
    )
    assert channel.status_code == 200, channel.text
    for alert in (deleted_alert, retained_alert):
        route = client.post(
            f"/api/projects/{pid}/notification-routes",
            json={
                "name": f"Route for {alert['id']}",
                "channel_id": channel.json()["id"],
                "source_kind": "alert",
                "source_ref_match": {"alert_id": alert["id"]},
                "delivery_mode": "immediate",
            },
        )
        assert route.status_code == 200, route.text

    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_items WHERE "
            "json_extract(source_ref, '$.alert_id')=?",
            (deleted_alert["id"],),
        ).fetchone()["n"]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_routes WHERE "
            "source_kind='alert' AND json_extract(source_ref_match_json, '$.alert_id')=?",
            (deleted_alert["id"],),
        ).fetchone()["n"]
        == 1
    )

    removed = client.delete(f"/api/projects/{pid}/alerts/{deleted_alert['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"ok": True, "deleted": deleted_alert["id"]}

    listed = client.get(f"/api/projects/{pid}/alerts").json()
    assert [row["id"] for row in listed] == [retained_alert["id"]]
    for table, column, value in (
        ("alert_checks", "alert_id", deleted_alert["id"]),
        ("alert_check_events", "alert_id", deleted_alert["id"]),
        ("alert_seen_rows", "alert_id", deleted_alert["id"]),
        ("alert_row_field_state", "alert_id", deleted_alert["id"]),
    ):
        assert (
            project.db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=?", (value,)
            ).fetchone()["n"]
            == 0
        )
    assert project.get_alert_check(deleted_run["id"]) is None
    assert project.get_alert_check(retained_run["id"]) is not None
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM alert_check_hits WHERE run_id=?",
            (deleted_run["id"],),
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_items WHERE "
            "json_extract(source_ref, '$.alert_id')=?",
            (deleted_alert["id"],),
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_items WHERE "
            "json_extract(source_ref, '$.alert_id')=?",
            (retained_alert["id"],),
        ).fetchone()["n"]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_routes WHERE "
            "source_kind='alert' AND json_extract(source_ref_match_json, '$.alert_id')=?",
            (deleted_alert["id"],),
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_routes WHERE "
            "source_kind='alert' AND json_extract(source_ref_match_json, '$.alert_id')=?",
            (retained_alert["id"],),
        ).fetchone()["n"]
        == 1
    )
    # This transaction only owns local projections/routes. An already-delivered
    # email or other external message is deliberately outside its runtime scope.
