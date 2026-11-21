from __future__ import annotations

from tests.alert_helpers import add_alert

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import EMBEDDING_REFRESH_KIND, Worker
from frisket.engine.store import Project
from frisket.engine.store.feeds import FeedStore
from frisket.features.alerts.triggers import (
    feed_poll_materialization_from_receipt,
    trigger_source_materialized_alerts,
)
from frisket.features.alerts.service import AlertBindingError
from frisket.engine.store.transactions import write_transaction


PROJECT_ID = "source-watch-project"


def test_standalone_index_race_retries_only_failed_keyword_alert(monkeypatch):
    import frisket.features.alerts.triggers as triggers
    import frisket.search

    alerts = [
        {
            "id": 1,
            "view_spec": '{"selection":{"scope":{"kind":"project"},"search":{"text":"one"}}}',
        },
        {
            "id": 2,
            "view_spec": '{"selection":{"scope":{"kind":"project"},"search":{"text":"two"}}}',
        },
    ]
    calls = {1: 0, 2: 0}
    drains = []
    monkeypatch.setattr(
        triggers, "affected_enabled_alerts", lambda _project, _event: alerts
    )
    monkeypatch.setattr(
        frisket.search, "drain_index", lambda _project: drains.append(1)
    )

    def evaluate(_project, alert):
        alert_id = int(alert["id"])
        calls[alert_id] += 1
        if alert_id == 2 and calls[alert_id] == 1:
            raise AlertBindingError("search_index_not_ready", "edited")
        return {"watch_id": alert_id}

    monkeypatch.setattr(triggers, "check_alert", evaluate)
    event = {
        "kind": "feed_poll_materialized",
        "project_id": PROJECT_ID,
        "feed_id": 1,
        "feed_check_id": 2,
        "sheet_id": 3,
        "op_id": 4,
        "row_ids": [5],
        "materialized_rows": 1,
    }

    results = trigger_source_materialized_alerts(object(), event)

    assert [result["watch_id"] for result in results] == [1, 2]
    assert calls == {1: 1, 2: 2}
    assert len(drains) == 2


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


def _feed_poll_action(source_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "feed.poll",
        "scope": {"kind": "project"},
        "params": {"feed": source_id},
        "idempotency_key": f"feed-watch@sha256:{key}",
    }


def _create_project(tmp_path: Path) -> tuple[Project, int]:
    project = Project.create(tmp_path / "source-watch.frisket", name="Source Watch")
    with write_transaction(project.db):
        source_id = FeedStore(project).add_feed(
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
        _feed_poll_action(source_id, key),
        project_id=PROJECT_ID,
        rss_fetcher=lambda _url: feed,
    )
    assert result.status == "completed", result
    assert result.receipt_id
    return result


def test_completed_source_poll_evaluates_affected_enabled_alerts(
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
        first_materialized = feed_poll_materialization_from_receipt(
            project,
            project_id=PROJECT_ID,
            receipt_id=first.receipt_id,
        )
        assert first_materialized is not None
        sheet_id = first_materialized["sheet_id"]

        project_alert = add_alert(
            project,
            "Project budget watch",
            scope="project",
            query={"kind": "fts", "q": "budget", "limit": 20},
        )
        sheet_alert = add_alert(
            project,
            "Sheet budget watch",
            scope="sheet",
            sheet_id=sheet_id,
            query={"kind": "fts", "q": "budget", "limit": 20},
        )
        disabled_alert = add_alert(
            project,
            "Disabled budget watch",
            scope="project",
            query={"kind": "fts", "q": "budget", "limit": 20},
            enabled=False,
        )
        other_sheet = project.add_sheet("Other source")
        other_sheet_alert = add_alert(
            project,
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
        materialized = feed_poll_materialization_from_receipt(
            project,
            project_id=PROJECT_ID,
            receipt_id=second.receipt_id,
        )
        assert materialized is not None
        assert materialized["feed_id"] == source_id
        assert materialized["sheet_id"] == sheet_id
        assert materialized["op_id"] is not None
        assert materialized["materialized_rows"] == 1
        assert len(materialized["row_ids"]) == 1

        triggered = trigger_source_materialized_alerts(project, materialized)

        assert [item["watch_id"] for item in triggered] == [project_alert, sheet_alert]
        assert {item["trigger_kind"] for item in triggered} == {
            "feed_poll_materialized"
        }
        assert all(
            item["feed_check_id"] == materialized["feed_check_id"] for item in triggered
        )
        assert all(item["run"]["matched_rows"] == 2 for item in triggered)
        assert all(item["run"]["new_rows"] == 0 for item in triggered)

        for alert_id in (project_alert, sheet_alert):
            latest = project.alert_latest_run(alert_id)
            assert latest is not None
            events = project.alert_check_events(int(latest["id"]), limit=10)
            assert events == []

        assert project.alert_latest_run(disabled_alert) is None
        assert project.alert_latest_run(other_sheet_alert) is None
    finally:
        project.close()


def test_source_poll_selects_sheet_filter_alert_from_owned_query(
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
        initial = feed_poll_materialization_from_receipt(
            project, project_id=PROJECT_ID, receipt_id=first.receipt_id
        )
        assert initial is not None
        sheet_id = initial["sheet_id"]
        filter_id = add_alert(
            project,
            "All source rows",
            scope="sheet",
            sheet_id=sheet_id,
            query={"kind": "filter", "sheet_id": sheet_id, "filter": {}},
        )
        later_id = add_alert(
            project,
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
        materialized = feed_poll_materialization_from_receipt(
            project, project_id=PROJECT_ID, receipt_id=second.receipt_id
        )
        assert materialized is not None

        triggered = trigger_source_materialized_alerts(project, materialized)

        assert [item["watch_id"] for item in triggered] == [filter_id, later_id]
        assert triggered[0]["run"]["status"] == "ok"
        assert triggered[0]["run"]["matched_rows"] == 2
        assert triggered[1]["run"]["matched_rows"] == 2
    finally:
        project.close()


def _finish_source_poll_job(client: TestClient, body: dict[str, Any]):
    assert body["status"] == "queued"
    workspace = client.app.state.workspace
    queue = workspace.queue
    job_id = body["job_id"]
    assert queue.get(job_id).kind == "feed.poll"
    worker = Worker(queue, workspace.registry)
    for _ in range(20):
        job = queue.get(job_id)
        if job.status in {"done", "failed", "cancelled"}:
            break
        assert worker.run_once()
    job = queue.get(job_id)
    assert job.status == "done", job.error
    assert job.result["receipt_id"]
    return job


def test_v1_source_poll_action_triggers_alert_check(
    client,
    monkeypatch: Any,
) -> None:
    from frisket import ingest

    project_id = client.post(
        "/api/projects", json={"name": "Source trigger route"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    with write_transaction(project.db):
        source_id = FeedStore(project).add_feed(
            "Public Notices",
            kind="rss",
            url="https://feeds.example/notices.xml",
            config={"merge_strategy": "append-new"},
            schedule="@hourly",
        )
    alert_id = add_alert(
        project,
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
        json=_feed_poll_action(source_id, "http-route"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert project.alert_latest_run(alert_id) is None
    _finish_source_poll_job(client, body)
    worker = Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    )
    for _ in range(20):
        if project.alert_latest_run(alert_id) is not None:
            break
        assert worker.run_once()
    run = project.alert_latest_run(alert_id)
    assert run is not None
    assert run["matched_rows"] == 1
    assert run["new_rows"] == 0
    events = project.alert_check_events(int(run["id"]), limit=10)
    assert events == []


def test_noop_and_failed_source_polls_do_not_trigger_alert_checks(
    tmp_path: Path,
) -> None:
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
        first_materialized = feed_poll_materialization_from_receipt(
            project,
            project_id=PROJECT_ID,
            receipt_id=first.receipt_id,
        )
        assert first_materialized is not None
        alert_id = add_alert(
            project,
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
            feed_poll_materialization_from_receipt(
                project,
                project_id=PROJECT_ID,
                receipt_id=noop.receipt_id,
            )
            is None
        )

        failed = run_action_spec(
            project,
            _feed_poll_action(source_id, "failed"),
            project_id=PROJECT_ID,
            rss_fetcher=lambda _url: (_ for _ in ()).throw(RuntimeError("feed down")),
        )
        assert failed.status == "failed"
        assert failed.receipt_id
        assert (
            feed_poll_materialization_from_receipt(
                project,
                project_id=PROJECT_ID,
                receipt_id=failed.receipt_id,
            )
            is None
        )

        assert project.alert_latest_run(alert_id) is None
    finally:
        project.close()


def test_v1_source_poll_job_enqueues_embedding_refresh(
    client,
    monkeypatch: Any,
) -> None:
    # HTTP admission queues source.poll; its worker must still enqueue the
    # refresh needed by embedding_similarity alerts after appending source rows.
    from frisket import ingest

    pid = client.post("/api/projects", json={"name": "src emb"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    with write_transaction(project.db):
        source_id = FeedStore(project).add_feed(
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
        f"/api/projects/{pid}/actions/v1/run", json=_feed_poll_action(source_id, "f1")
    )
    assert r1.status_code == 200, r1.text
    first_job = _finish_source_poll_job(client, r1.json())
    mat = feed_poll_materialization_from_receipt(
        project, project_id=pid, receipt_id=first_job.result["receipt_id"]
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

    # A second queued poll appends one row and enqueues its embedding refresh.
    monkeypatch.setattr(
        ingest,
        "fetch_feed_text",
        lambda _u: _feed([_one("a", "First"), _one("b", "Second")]),
    )
    r2 = client.post(
        f"/api/projects/{pid}/actions/v1/run", json=_feed_poll_action(source_id, "f2")
    )
    assert r2.status_code == 200, r2.text
    second_job = _finish_source_poll_job(client, r2.json())
    assert second_job.result["new_rows"] == 1

    jobs = client.app.state.workspace.queue.list_project_jobs(
        pid, kind=EMBEDDING_REFRESH_KIND
    )
    refresh = next(j for j in jobs if j.payload.get("index_id") == index_id)
    assert refresh.status == "queued"
    assert (
        refresh.payload["trigger_ref"]["feed_check_id"] == second_job.result["run_id"]
    )
