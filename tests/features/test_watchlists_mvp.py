from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client
from http_test_helpers import post_row_add_as_v1_action


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
    return pid, int(response.json()["sheet_id"])


def _create_fts_watch(client: TestClient, pid: str, *, sheet_id: int | None = None):
    scope: dict[str, Any] = {"kind": "project"}
    if sheet_id is not None:
        scope = {"kind": "sheet", "sheet_id": sheet_id}
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Budget watch",
            "scope": scope,
            "query": {"kind": "fts", "q": "budget", "limit": 10},
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


def test_fts_watch_manual_runs_track_matched_and_new_rows(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)

    watch = _create_fts_watch(client, pid)
    assert watch["query"] == {
        "kind": "fts",
        "q": "budget",
        "mode": "keyword",
        "rerank": "off",
        "limit": 10,
    }
    assert watch["latest_run"] is None

    first = _run_watch(client, pid, watch["id"])
    assert first["run"]["matched_rows"] == 2
    assert first["run"]["new_rows"] == 2
    assert {hit["run_id"] for hit in first["hits"]} == {first["run"]["id"]}
    assert [hit["is_new"] for hit in first["hits"]] == [True, True]
    assert {hit["sheet_id"] for hit in first["hits"]} == {sheet_id}
    assert all(hit["snippet"] for hit in first["hits"])

    second = _run_watch(client, pid, watch["id"])
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

    third = _run_watch(client, pid, watch["id"])
    assert third["run"]["matched_rows"] == 3
    assert third["run"]["new_rows"] == 1
    assert sum(1 for hit in third["hits"] if hit["is_new"]) == 1

    listing = client.get(f"/api/projects/{pid}/watches")
    assert listing.status_code == 200, listing.text
    listed = listing.json()
    assert listed[0]["latest_run"]["id"] == third["run"]["id"]
    assert listed[0]["latest_run"]["new_rows"] == 1
    assert listed[0]["last_evaluated_op"] > 0

    page = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs",
        params={"offset": 1, "limit": 1},
    )
    assert page.status_code == 200, page.text
    runs_page = page.json()
    assert runs_page["schema_version"] == "frisket.watch_runs_page.v1"
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
        f"/api/projects/{pid}/watches/{watch['id']}/runs",
        params={"offset": 0, "limit": 1, "hits_limit": 1},
    )
    assert hit_limited_page.status_code == 200, hit_limited_page.text
    latest_run_page = hit_limited_page.json()
    assert latest_run_page["hits_limit"] == 1
    assert len(latest_run_page["runs"][0]["hits"]) == 1
    assert latest_run_page["runs"][0]["hits_limit"] == 1
    assert latest_run_page["runs"][0]["hits_truncated"] is True


def test_filter_watch_owns_captured_membership_and_uses_shared_tables(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)

    watch_response = client.post(
        f"/api/projects/{pid}/watches",
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
    assert watch_response.status_code == 200, watch_response.text
    watch = watch_response.json()
    assert watch["scope"] == "sheet"
    assert watch["sheet_id"] == sheet_id
    assert watch["query"] == {
        "kind": "filter",
        "sheet_id": sheet_id,
        "filter": {"status": {"eq": "open"}},
    }

    run = _run_watch(client, pid, watch["id"])
    assert run["run"]["matched_rows"] == 2
    assert run["run"]["new_rows"] == 2
    assert [hit["snippet"] for hit in run["hits"]] == [
        "Matched filter",
        "Matched filter",
    ]

    project = client.app.state.workspace.get(pid)
    tables = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"watches", "watch_runs", "watch_run_hits", "watch_seen_rows"}.issubset(
        tables
    )
    indexes = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert {
        "idx_watch_runs_watch_started",
        "idx_watch_run_hits_run_rank",
        "idx_watch_seen_rows_watch",
    }.issubset(indexes)


def test_fts_watch_preserves_501_requested_matches_through_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The 500-hit response page must not cap evaluation or durable matches."""
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Large watch"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("Documents")
    text_column_id = project.add_column(sheet_id, "text", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"text": f"needle {index}"} for index in range(501)],
        {"text": text_column_id},
    )

    import frisket.search as search

    observed_limits: list[int] = []

    def fake_search_project(project_arg, query, limit=50, rerank="auto"):
        assert project_arg is project
        assert query == "needle"
        assert rerank == "off"
        observed_limits.append(limit)
        return [
            {
                "sheet_id": sheet_id,
                "row_id": row_id,
                "column_id": text_column_id,
                "snip": f"needle {index}",
            }
            for index, row_id in enumerate(row_ids)
        ]

    monkeypatch.setattr(search, "search_project", fake_search_project)
    created = client.post(
        f"/api/projects/{pid}/watches",
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
    watch = created.json()

    result = _run_watch(client, pid, watch["id"])
    run_id = result["run"]["id"]
    persisted = project.db.execute(
        "SELECT COUNT(*) AS count FROM watch_run_hits WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert watch["query"]["limit"] == 501
    assert observed_limits == [501]
    assert result["run"]["matched_rows"] == 501
    assert result["run"]["new_rows"] == 501
    assert persisted["count"] == 501
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
    from frisket.features.watchlists.specs import normalize_query_spec

    assert normalize_query_spec(query)["limit"] == 501


def test_watch_patch_pause_resume_preserves_execution_state_and_manual_runs(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    watch = _create_fts_watch(client, pid)
    first_run = _run_watch(client, pid, watch["id"])["run"]
    before = client.get(f"/api/projects/{pid}/watches").json()[0]

    renamed = client.patch(
        f"/api/projects/{pid}/watches/{watch['id']}",
        json={"name": "  Budget alerts  "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Budget alerts"
    for field in (
        "query",
        "enabled",
        "last_evaluated_op",
        "last_run_id",
    ):
        assert renamed.json()[field] == before[field]

    paused = client.patch(
        f"/api/projects/{pid}/watches/{watch['id']}", json={"enabled": False}
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False
    assert paused.json()["last_run_id"] == first_run["id"]
    assert paused.json()["last_evaluated_op"] == before["last_evaluated_op"]

    # Pause affects automatic selection only. A deliberate Run now remains a
    # manual evaluation and extends, rather than replaces, history.
    manual_while_paused = _run_watch(client, pid, watch["id"])["run"]
    assert manual_while_paused["id"] != first_run["id"]
    assert (
        client.get(f"/api/projects/{pid}/watches/{watch['id']}/runs").json()["total"]
        == 2
    )

    paused_cursor = paused.json()["last_evaluated_op"]
    paused_history = manual_while_paused["id"]
    resumed = client.patch(
        f"/api/projects/{pid}/watches/{watch['id']}", json={"enabled": True}
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["enabled"] is True
    assert resumed.json()["last_evaluated_op"] >= paused_cursor
    assert resumed.json()["last_run_id"] == paused_history
    # Resuming changes scheduling eligibility only; it never evaluates inline.
    assert (
        client.get(f"/api/projects/{pid}/watches/{watch['id']}/runs").json()["total"]
        == 2
    )


def test_watch_delete_cascades_only_its_state_and_single_watch_notifications(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    deleted_watch = _create_fts_watch(client, pid)
    retained_watch = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Retained watch",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "bridge", "limit": 10},
        },
    ).json()
    deleted_run = _run_watch(client, pid, deleted_watch["id"])["run"]
    retained_run = _run_watch(client, pid, retained_watch["id"])["run"]
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
    for watch in (deleted_watch, retained_watch):
        route = client.post(
            f"/api/projects/{pid}/notification-routes",
            json={
                "name": f"Route for {watch['id']}",
                "channel_id": channel.json()["id"],
                "source_kind": "watch",
                "source_ref_match": {"watch_id": watch["id"]},
                "delivery_mode": "immediate",
            },
        )
        assert route.status_code == 200, route.text

    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_items WHERE "
            "json_extract(source_ref, '$.watch_id')=?",
            (deleted_watch["id"],),
        ).fetchone()["n"]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_routes WHERE "
            "source_kind='watch' AND json_extract(source_ref_match_json, '$.watch_id')=?",
            (deleted_watch["id"],),
        ).fetchone()["n"]
        == 1
    )

    removed = client.delete(f"/api/projects/{pid}/watches/{deleted_watch['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"ok": True, "deleted": deleted_watch["id"]}

    listed = client.get(f"/api/projects/{pid}/watches").json()
    assert [row["id"] for row in listed] == [retained_watch["id"]]
    for table, column, value in (
        ("watch_runs", "watch_id", deleted_watch["id"]),
        ("watch_run_events", "watch_id", deleted_watch["id"]),
        ("watch_seen_rows", "watch_id", deleted_watch["id"]),
        ("watch_row_field_state", "watch_id", deleted_watch["id"]),
    ):
        assert (
            project.db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=?", (value,)
            ).fetchone()["n"]
            == 0
        )
    assert project.get_watch_run(deleted_run["id"]) is None
    assert project.get_watch_run(retained_run["id"]) is not None
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM watch_run_hits WHERE run_id=?",
            (deleted_run["id"],),
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_items WHERE "
            "json_extract(source_ref, '$.watch_id')=?",
            (deleted_watch["id"],),
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_items WHERE "
            "json_extract(source_ref, '$.watch_id')=?",
            (retained_watch["id"],),
        ).fetchone()["n"]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_routes WHERE "
            "source_kind='watch' AND json_extract(source_ref_match_json, '$.watch_id')=?",
            (deleted_watch["id"],),
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_routes WHERE "
            "source_kind='watch' AND json_extract(source_ref_match_json, '$.watch_id')=?",
            (retained_watch["id"],),
        ).fetchone()["n"]
        == 1
    )
    # This transaction only owns local projections/routes. An already-delivered
    # email or other external message is deliberately outside its runtime scope.
