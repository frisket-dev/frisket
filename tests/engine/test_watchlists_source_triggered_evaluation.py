from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import EMBEDDING_REFRESH_KIND
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore
from frisket.features.watchlists.triggers import (
    source_poll_materialization_from_receipt,
    trigger_source_materialized_watches,
)


PROJECT_ID = "source-watch-project"


def _feed(items: list[dict[str, Any]], feed_title: str = "Public Notices") -> str:
    entries: list[str] = []
    for item in items:
        parts: list[str] = []
        if "guid" in item:
            parts.append(f"<guid isPermaLink='false'>{item['guid']}</guid>")
        if "title" in item:
            parts.append(f"<title>{item['title']}</title>")
        if "link" in item:
            parts.append(f"<link>{item['link']}</link>")
        if "description" in item:
            parts.append(f"<description>{item['description']}</description>")
        entries.append("<item>" + "".join(parts) + "</item>")
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        f"<title>{feed_title}</title>" + "".join(entries) + "</channel></rss>"
    )


def _source_poll_action(source_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": {"source": source_id},
        "idempotency_key": f"source-watch@sha256:{key}",
    }


def _create_project(tmp_path: Path) -> tuple[Project, int]:
    project = Project.create(tmp_path / "source-watch.frisket", name="Source Watch")
    source_id = SourceStore(project).add_source(
        "Public Notices",
        kind="rss",
        url="https://feeds.example/notices.xml",
        config={"merge_strategy": "append-new"},
        schedule="@hourly",
    )
    return project, source_id


def _run_source_poll(
    project: Project,
    source_id: int,
    *,
    key: str,
    feed: str,
):
    result = run_action_spec(
        project,
        _source_poll_action(source_id, key),
        project_id=PROJECT_ID,
        rss_fetcher=lambda _url: feed,
    )
    assert result.status == "completed", result
    assert result.receipt_id
    return result


def test_completed_source_poll_evaluates_affected_enabled_watches(
    tmp_path: Path,
) -> None:
    project, source_id = _create_project(tmp_path)
    try:
        first = _run_source_poll(
            project,
            source_id,
            key="initial",
            feed=_feed(
                [
                    {
                        "guid": "a",
                        "title": "Budget hearing",
                        "link": "https://x/a",
                        "description": "Council budget oversight",
                    }
                ]
            ),
        )
        first_materialized = source_poll_materialization_from_receipt(
            project,
            project_id=PROJECT_ID,
            receipt_id=first.receipt_id,
        )
        assert first_materialized is not None
        sheet_id = first_materialized["sheet_id"]

        project_watch = project.add_watch(
            "Project budget watch",
            scope="project",
            query={"kind": "fts", "q": "budget", "limit": 20},
        )
        sheet_watch = project.add_watch(
            "Sheet budget watch",
            scope="sheet",
            sheet_id=sheet_id,
            query={"kind": "fts", "q": "budget", "limit": 20},
        )
        disabled_watch = project.add_watch(
            "Disabled budget watch",
            scope="project",
            query={"kind": "fts", "q": "budget", "limit": 20},
            enabled=False,
        )
        other_sheet = project.add_sheet("Other source")
        other_sheet_watch = project.add_watch(
            "Other sheet watch",
            scope="sheet",
            sheet_id=other_sheet,
            query={"kind": "fts", "q": "budget", "limit": 20},
        )

        second = _run_source_poll(
            project,
            source_id,
            key="second",
            feed=_feed(
                [
                    {
                        "guid": "a",
                        "title": "Budget hearing",
                        "link": "https://x/a",
                        "description": "Council budget oversight",
                    },
                    {
                        "guid": "b",
                        "title": "Budget amendment",
                        "link": "https://x/b",
                        "description": "A late budget item arrived",
                    },
                ]
            ),
        )
        materialized = source_poll_materialization_from_receipt(
            project,
            project_id=PROJECT_ID,
            receipt_id=second.receipt_id,
        )
        assert materialized is not None
        assert materialized["source_id"] == source_id
        assert materialized["sheet_id"] == sheet_id
        assert materialized["op_id"] is not None
        assert materialized["materialized_rows"] == 1
        assert len(materialized["row_ids"]) == 1

        triggered = trigger_source_materialized_watches(project, materialized)

        assert [item["watch_id"] for item in triggered] == [project_watch, sheet_watch]
        assert {item["trigger_kind"] for item in triggered} == {
            "source_poll_materialized"
        }
        assert all(
            item["source_run_id"] == materialized["source_run_id"] for item in triggered
        )
        assert all(item["run"]["matched_rows"] == 2 for item in triggered)
        assert all(item["run"]["new_rows"] == 2 for item in triggered)

        for watch_id in (project_watch, sheet_watch):
            latest = project.watch_latest_run(watch_id)
            assert latest is not None
            events = project.watch_run_events(int(latest["id"]), limit=10)
            assert len(events) == 2
            assert {event["event_kind"] for event in events} == {"row_entered"}

        assert project.watch_latest_run(disabled_watch) is None
        assert project.watch_latest_run(other_sheet_watch) is None
    finally:
        project.close()


def test_source_poll_selects_sheet_filter_watch_from_owned_query(
    tmp_path: Path,
) -> None:
    project, source_id = _create_project(tmp_path)
    try:
        first = _run_source_poll(
            project,
            source_id,
            key="follow-first",
            feed=_feed(
                [
                    {
                        "guid": "a",
                        "title": "Budget hearing",
                        "link": "https://x/a",
                        "description": "Council budget oversight",
                    }
                ]
            ),
        )
        initial = source_poll_materialization_from_receipt(
            project, project_id=PROJECT_ID, receipt_id=first.receipt_id
        )
        assert initial is not None
        sheet_id = initial["sheet_id"]
        filter_id = project.add_watch(
            "All source rows",
            scope="sheet",
            sheet_id=sheet_id,
            query={"kind": "filter", "sheet_id": sheet_id, "filter": {}},
        )
        later_id = project.add_watch(
            "Later direct watch",
            scope="sheet",
            sheet_id=sheet_id,
            query={"kind": "fts", "q": "budget", "limit": 20},
        )

        second = _run_source_poll(
            project,
            source_id,
            key="follow-second",
            feed=_feed(
                [
                    {
                        "guid": "a",
                        "title": "Budget hearing",
                        "link": "https://x/a",
                        "description": "Council budget oversight",
                    },
                    {
                        "guid": "b",
                        "title": "Budget amendment",
                        "link": "https://x/b",
                        "description": "A late budget item arrived",
                    },
                ]
            ),
        )
        materialized = source_poll_materialization_from_receipt(
            project, project_id=PROJECT_ID, receipt_id=second.receipt_id
        )
        assert materialized is not None

        triggered = trigger_source_materialized_watches(project, materialized)

        assert [item["watch_id"] for item in triggered] == [filter_id, later_id]
        assert triggered[0]["run"]["status"] == "ok"
        assert triggered[0]["run"]["matched_rows"] == 2
        assert triggered[1]["run"]["matched_rows"] == 2
    finally:
        project.close()


def test_v1_source_poll_action_triggers_watch_run(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from frisket import ingest

    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Source trigger route"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    source_id = SourceStore(project).add_source(
        "Public Notices",
        kind="rss",
        url="https://feeds.example/notices.xml",
        config={"merge_strategy": "append-new"},
        schedule="@hourly",
    )
    watch_id = project.add_watch(
        "Budget watch",
        scope="project",
        query={"kind": "fts", "q": "budget", "limit": 20},
    )
    monkeypatch.setattr(
        ingest,
        "fetch_feed_text",
        lambda _url: _feed(
            [
                {
                    "guid": "route-a",
                    "title": "Budget hearing",
                    "link": "https://x/route-a",
                    "description": "Council budget oversight",
                }
            ]
        ),
    )

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_source_poll_action(source_id, "http-route"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    run = project.watch_latest_run(watch_id)
    assert run is not None
    assert run["matched_rows"] == 1
    assert run["new_rows"] == 1
    events = project.watch_run_events(int(run["id"]), limit=10)
    assert [event["event_kind"] for event in events] == ["row_entered"]


def test_noop_and_failed_source_polls_do_not_trigger_watch_runs(tmp_path: Path) -> None:
    project, source_id = _create_project(tmp_path)
    try:
        first = _run_source_poll(
            project,
            source_id,
            key="first",
            feed=_feed(
                [
                    {
                        "guid": "a",
                        "title": "Budget hearing",
                        "link": "https://x/a",
                        "description": "Council budget oversight",
                    }
                ]
            ),
        )
        first_materialized = source_poll_materialization_from_receipt(
            project,
            project_id=PROJECT_ID,
            receipt_id=first.receipt_id,
        )
        assert first_materialized is not None
        watch_id = project.add_watch(
            "Budget watch",
            scope="project",
            query={"kind": "fts", "q": "budget", "limit": 20},
        )

        noop = _run_source_poll(
            project,
            source_id,
            key="noop",
            feed=_feed(
                [
                    {
                        "guid": "a",
                        "title": "Budget hearing",
                        "link": "https://x/a",
                        "description": "Council budget oversight",
                    }
                ]
            ),
        )
        assert (
            source_poll_materialization_from_receipt(
                project,
                project_id=PROJECT_ID,
                receipt_id=noop.receipt_id,
            )
            is None
        )

        failed = run_action_spec(
            project,
            _source_poll_action(source_id, "failed"),
            project_id=PROJECT_ID,
            rss_fetcher=lambda _url: (_ for _ in ()).throw(RuntimeError("feed down")),
        )
        assert failed.status == "failed"
        assert failed.receipt_id
        assert (
            source_poll_materialization_from_receipt(
                project,
                project_id=PROJECT_ID,
                receipt_id=failed.receipt_id,
            )
            is None
        )

        assert project.watch_latest_run(watch_id) is None
    finally:
        project.close()


def test_v1_source_poll_inline_enqueues_embedding_refresh(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    # The inline v1 source.poll path mirrors the queued worker path: appends enqueue
    # an embedding refresh so embedding_similarity watches evaluate after refresh.
    from frisket import ingest

    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "src emb"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    source_id = SourceStore(project).add_source(
        "Notices",
        kind="rss",
        url="https://feeds.example/x.xml",
        config={"merge_strategy": "append-new"},
        schedule="@hourly",
    )

    def _one(guid: str, title: str) -> dict[str, Any]:
        return {
            "guid": guid,
            "title": title,
            "link": f"https://x/{guid}",
            "description": "b",
        }

    # first fetch creates the feed sheet + a row
    monkeypatch.setattr(
        ingest, "fetch_feed_text", lambda _u: _feed([_one("a", "First")])
    )
    r1 = client.post(
        f"/api/projects/{pid}/actions/v1/run", json=_source_poll_action(source_id, "f1")
    )
    assert r1.json()["status"] == "completed", r1.text
    mat = source_poll_materialization_from_receipt(
        project, project_id=pid, receipt_id=r1.json()["receipt_id"]
    )
    assert mat is not None
    sheet_id = int(mat["sheet_id"])
    title_col = next(
        c["name"] for c in project.columns(sheet_id) if c["name"].lower() == "title"
    )

    # an on_source_append index over the feed's title column
    create = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet_id,
                "source_columns": [title_col],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "maintenance_policy": {"mode": "on_source_append"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "emb_create@1",
        },
        project_id=pid,
    )
    assert create.status == "completed", create.errors
    index_id = create.outputs[0].ref["index_id"]

    # second fetch appends a new row -> inline path enqueues the embedding refresh
    monkeypatch.setattr(
        ingest,
        "fetch_feed_text",
        lambda _u: _feed([_one("a", "First"), _one("b", "Second")]),
    )
    r2 = client.post(
        f"/api/projects/{pid}/actions/v1/run", json=_source_poll_action(source_id, "f2")
    )
    assert r2.json()["status"] == "completed", r2.text

    jobs = client.app.state.workspace.queue.list_project_jobs(
        pid, kind=EMBEDDING_REFRESH_KIND
    )
    assert any((j.payload or {}).get("index_id") == index_id for j in jobs)
