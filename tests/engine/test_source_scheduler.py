"""Queued live-source polling.

Scheduled RSS sources enter the same worker queue as recipe runs via the
internal ``source.poll`` job kind and dispatch the public v1 ``source.poll``
action.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from frisket.engine.jobs import (
    JobHandlerContext,
    NOTIFICATION_DELIVER_KIND,
    SOURCE_POLL_KIND,
    SqliteJobQueue,
    Worker,
    default_registry,
    enqueue_enclosure_downloads,
    enqueue_due_source_polls,
    register_source_poll_handler,
)
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


def _feed(items: list[dict], feed_title: str = "Queued Feed") -> str:
    entries = []
    for item in items:
        parts = []
        if "guid" in item:
            parts.append(f"<guid isPermaLink='false'>{item['guid']}</guid>")
        if "title" in item:
            parts.append(f"<title>{item['title']}</title>")
        if "link" in item:
            parts.append(f"<link>{item['link']}</link>")
        if "enclosure" in item:
            enc = item["enclosure"]
            parts.append(
                "<enclosure "
                f"url='{enc['url']}' "
                f"type='{enc.get('type', 'audio/mpeg')}' "
                f"length='{enc.get('length', 0)}' />"
            )
        entries.append("<item>" + "".join(parts) + "</item>")
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        f"<title>{feed_title}</title>" + "".join(entries) + "</channel></rss>"
    )


def _project(workspace, project_id: str = "news") -> Project:
    workspace.mkdir(parents=True, exist_ok=True)
    return Project.create(workspace / f"{project_id}.frisket", name=project_id)


def _register(workspace, fetch, queue=None):
    reg = default_registry()
    register_source_poll_handler(
        reg, workspace_root=workspace, queue=queue, fetch=fetch
    )
    return reg


def test_source_poll_worker_ingests_enabled_rss(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="News",
        kind="rss",
        url="https://example.com/feed.xml",
        schedule="@hourly",
    )
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    jid = q.enqueue(
        SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid}, max_attempts=1
    )

    worker = Worker(
        q,
        _register(
            workspace,
            lambda url: _feed([{"guid": "a", "title": "Alpha", "link": "https://x/a"}]),
            queue=q,
        ),
    )
    assert worker.run_once()

    job = q.get(jid)
    assert job.status == "done"
    assert job.result["new_rows"] == 1
    assert job.result["revisions"] == 0
    reopened = Project(workspace / "news.frisket")
    runs = [dict(r) for r in SourceStore(reopened).source_runs(sid)]
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["new_rows"] == 1
    reopened.close()
    q.close()


def test_source_poll_watch_notification_enqueues_external_delivery_only_for_events(
    tmp_path,
):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="News",
        kind="rss",
        url="https://example.com/feed.xml",
        schedule="@hourly",
    )
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    feeds = [
        _feed(
            [
                {
                    "guid": "a",
                    "title": "Budget hearing",
                    "link": "https://x/a",
                }
            ]
        ),
        _feed(
            [
                {
                    "guid": "a",
                    "title": "Budget hearing",
                    "link": "https://x/a",
                },
                {
                    "guid": "b",
                    "title": "Budget amendment",
                    "link": "https://x/b",
                },
            ]
        ),
        _feed(
            [
                {
                    "guid": "a",
                    "title": "Budget hearing",
                    "link": "https://x/a",
                },
                {
                    "guid": "b",
                    "title": "Budget amendment",
                    "link": "https://x/b",
                },
                {
                    "guid": "c",
                    "title": "Road closure",
                    "link": "https://x/c",
                },
            ]
        ),
    ]
    worker = Worker(
        q,
        _register(
            workspace,
            lambda _url: feeds[-1] if len(feeds) == 1 else feeds.pop(0),
            queue=q,
        ),
    )

    # Establish the source-owned sheet before creating a Watch, so only the
    # second poll has a Watch to evaluate.
    q.enqueue(SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid})
    assert worker.run_once()
    p = Project(workspace / "news.frisket")
    watch_id = p.add_watch(
        "Budget watch",
        scope="project",
        query={"kind": "fts", "q": "budget", "limit": 20},
    )
    channel = p.create_notification_channel(
        kind="email",
        name="Watch email",
        config={"to": "alerts@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    p.create_notification_route(
        name="Budget route",
        channel_id=int(channel["id"]),
        source_kind="watch",
        source_ref_match={"watch_id": watch_id},
    )
    p.close()

    q.enqueue(SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid})
    assert worker.run_once()
    delivery_jobs = q.list_project_jobs("news", kind=NOTIFICATION_DELIVER_KIND)
    assert len(delivery_jobs) == 1
    assert delivery_jobs[0].status == "queued"
    assert delivery_jobs[0].payload["workspace_root"] == str(workspace)

    # A later materialization still evaluates the Watch, but a non-matching
    # row emits no notification and cannot enqueue a second delivery.
    q.enqueue(SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid})
    assert worker.run_once()
    assert len(q.list_project_jobs("news", kind=NOTIFICATION_DELIVER_KIND)) == 1
    q.close()


def test_source_poll_worker_uses_v1_source_poll_receipt_and_enqueues_enclosures(
    tmp_path,
):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="Podcast",
        kind="rss",
        url="https://example.com/podcast.xml",
        schedule="@hourly",
        config={"download_enclosures": "queued"},
    )
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    jid = q.enqueue(
        SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid}, max_attempts=1
    )

    worker = Worker(
        q,
        _register(
            workspace,
            lambda url: _feed(
                [
                    {
                        "guid": "ep1",
                        "title": "Episode One",
                        "link": "https://x/ep1",
                        "enclosure": {
                            "url": "https://media.example/ep1.mp3",
                            "type": "audio/mpeg",
                            "length": 123,
                        },
                    }
                ],
                feed_title="Podcast",
            ),
            queue=q,
        ),
    )

    assert worker.run_once()

    job = q.get(jid)
    assert job.status == "done"
    assert job.result["action_kind"] == "source.poll"
    assert job.result["receipt_id"]
    assert job.result["new_rows"] == 1
    assert job.result["enclosure_jobs"] == 1
    enclosure_jobs = [
        queued
        for queued in q.list_jobs(status="queued")
        if queued.kind != SOURCE_POLL_KIND
    ]
    assert len(enclosure_jobs) == 1

    reopened = Project(workspace / "news.frisket")
    receipts = [
        dict(row)
        for row in reopened.db.execute(
            "SELECT * FROM receipts WHERE action_kind='source.poll'"
        ).fetchall()
    ]
    assert len(receipts) == 1
    assert receipts[0]["id"] == job.result["receipt_id"]
    receipt = json.loads(receipts[0]["body"])
    refs = [item["ref"] for item in [*receipt["outputs"], *receipt["evidence"]]]
    source_run_ref = next(ref for ref in refs if ref["kind"] == "source_poll_run")
    enclosure_ref = next(
        ref for ref in refs if ref["kind"] == "source_poll_enclosure_pointers"
    )
    assert source_run_ref["new_rows"] == 1
    assert enclosure_ref["row_ids"]
    runs = [dict(r) for r in SourceStore(reopened).source_runs(sid)]
    assert len(runs) == 1
    assert runs[0]["op_id"] == source_run_ref["op_id"]
    assert (
        enqueue_enclosure_downloads(
            q,
            project=reopened,
            project_id="news",
            workspace_root=workspace,
            source={"config": {"download_enclosures": "queued"}},
            sheet_id=enclosure_ref["sheet_id"],
            row_ids=list(enclosure_ref["row_ids"]),
        )
        == []
    )
    enclosure_jobs = [
        queued
        for queued in q.list_jobs(status="queued")
        if queued.kind != SOURCE_POLL_KIND
    ]
    assert len(enclosure_jobs) == 1
    reopened.close()
    q.close()


def test_source_poll_worker_skips_disabled_source(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="Paused",
        kind="rss",
        url="https://example.com/feed.xml",
        schedule="@hourly",
        enabled=False,
    )
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    jid = q.enqueue(
        SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid}, max_attempts=1
    )
    called = False

    def fetch(url):
        nonlocal called
        called = True
        return _feed([])

    assert Worker(q, _register(workspace, fetch, queue=q)).run_once()

    job = q.get(jid)
    assert job.status == "done"
    assert job.result == {
        "project_id": "news",
        "source_id": sid,
        "skipped": True,
        "reason": "disabled",
    }
    assert called is False
    reopened = Project(workspace / "news.frisket")
    assert SourceStore(reopened).source_runs(sid) == []
    reopened.close()
    q.close()


def test_source_poll_worker_skips_missing_source(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    jid = q.enqueue(
        SOURCE_POLL_KIND, {"project_id": "news", "source_id": 404}, max_attempts=1
    )
    called = False

    def fetch(url):
        nonlocal called
        called = True
        return _feed([])

    assert Worker(q, _register(workspace, fetch, queue=q)).run_once()

    job = q.get(jid)
    assert job.status == "done"
    assert job.result == {
        "project_id": "news",
        "source_id": 404,
        "skipped": True,
        "reason": "not_found",
    }
    assert called is False
    q.close()


def test_source_poll_worker_skips_non_rss_source(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="API",
        kind="api",
        url="https://example.com/api",
        schedule="@hourly",
    )
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    jid = q.enqueue(
        SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid}, max_attempts=1
    )
    called = False

    def fetch(url):
        nonlocal called
        called = True
        return _feed([])

    assert Worker(q, _register(workspace, fetch, queue=q)).run_once()

    job = q.get(jid)
    assert job.status == "done"
    assert job.result == {
        "project_id": "news",
        "source_id": sid,
        "skipped": True,
        "reason": "unsupported_kind:api",
    }
    assert called is False
    reopened = Project(workspace / "news.frisket")
    assert SourceStore(reopened).source_runs(sid) == []
    reopened.close()
    q.close()


def test_source_poll_failure_retry_replays_same_logical_poll(tmp_path):
    from deterministic_time import controlled_time

    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="Flaky",
        kind="rss",
        url="https://example.com/feed.xml",
        schedule="@hourly",
    )
    p.close()
    with controlled_time() as t:
        q = t.queue(workspace / ".queue.db")
        jid = q.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": sid,
                "poll_id": "news:1:2026-06-13T12:00:00.000000+00:00",
                "workspace_root": str(workspace),
            },
            max_attempts=2,
        )
        calls = 0

        def fetch(url):
            nonlocal calls
            calls += 1
            raise RuntimeError("feed down")

        worker = t.worker(
            q, _register(workspace, fetch, queue=q), retry_base_seconds=0.01
        )
        assert worker.run_once()
        first = q.get(jid)
        assert first.status == "queued"
        assert first.attempts == 1
        assert "feed down" in first.error
        reopened = Project(workspace / "news.frisket")
        runs = [dict(r) for r in SourceStore(reopened).source_runs(sid)]
        assert len(runs) == 1
        assert runs[0]["status"] == "error"
        source_notifications = [
            dict(row)
            for row in reopened.db.execute(
                "SELECT * FROM notification_items WHERE source_kind='source'"
            ).fetchall()
        ]
        assert len(source_notifications) == 1
        source_ref = json.loads(source_notifications[0]["source_ref"])
        assert source_ref["source_id"] == sid
        assert source_ref["status"] == "failed"
        assert "feed down" in source_notifications[0]["payload"]
        reopened.close()

        t.advance_seconds(1)  # past the (tiny) backoff
        assert worker.run_once()

        job = q.get(jid)
        assert job.status == "failed"
        assert job.attempts == 2
        assert calls == 1
        reopened = Project(workspace / "news.frisket")
        runs = [dict(r) for r in SourceStore(reopened).source_runs(sid)]
        assert len(runs) == 1
        assert runs[0]["status"] == "error"
        receipts = [
            dict(row)
            for row in reopened.db.execute(
                "SELECT idempotency_key, status FROM receipts WHERE action_kind='source.poll'"
            ).fetchall()
        ]
        assert len(receipts) == 1
        assert receipts[0]["status"] == "failed"
        reopened.close()


def test_scheduler_enqueues_due_enabled_sources_once(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    due = SourceStore(p).add_source(
        name="Due",
        kind="rss",
        url="https://example.com/due.xml",
        schedule="@hourly",
    )
    SourceStore(p).add_source(
        name="Disabled",
        kind="rss",
        url="https://example.com/disabled.xml",
        schedule="@hourly",
        enabled=False,
    )
    SourceStore(p).add_source(
        name="Manual",
        kind="rss",
        url="https://example.com/manual.xml",
        schedule=None,
    )
    SourceStore(p).add_source(
        name="Unsupported",
        kind="api",
        url="https://example.com/api",
        schedule="@hourly",
    )
    fresh = SourceStore(p).add_source(
        name="Fresh",
        kind="rss",
        url="https://example.com/fresh.xml",
        schedule="@hourly",
    )
    p.db.execute(
        "UPDATE sources SET last_checked_at=? WHERE id=?",
        ("2026-06-13 11:30:00", fresh),
    )
    p.db.commit()
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")

    jobs = enqueue_due_source_polls(
        workspace_root=workspace,
        queue=q,
        now=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    assert len(jobs) == 1
    assert jobs[0]["project_id"] == "news"
    assert jobs[0]["source_id"] == due
    queued = q.list_jobs(status="queued")
    assert len(queued) == 1
    assert queued[0].kind == SOURCE_POLL_KIND
    assert queued[0].payload == {
        "project_id": "news",
        "source_id": due,
        "poll_id": "news:1:2026-06-13T12:00:00.000000+00:00",
        "workspace_root": str(workspace),
    }

    assert (
        enqueue_due_source_polls(
            workspace_root=workspace,
            queue=q,
            now=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
        )
        == []
    )
    q.close()


def test_source_poll_success_after_error_emits_recovery_notification(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    sid = SourceStore(p).add_source(
        name="Recovering",
        kind="rss",
        url="https://example.com/recovering.xml",
        schedule="@hourly",
    )
    p.db.execute("UPDATE sources SET last_status='error' WHERE id=?", (sid,))
    p.db.commit()
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")
    q.enqueue(
        SOURCE_POLL_KIND, {"project_id": "news", "source_id": sid}, max_attempts=1
    )

    worker = Worker(
        q,
        _register(
            workspace,
            lambda url: _feed([{"guid": "ok", "title": "Recovered"}]),
            queue=q,
        ),
    )
    assert worker.run_once()

    reopened = Project(workspace / "news.frisket")
    source_notifications = [
        dict(row)
        for row in reopened.db.execute(
            "SELECT * FROM notification_items WHERE source_kind='source'"
        ).fetchall()
    ]
    assert len(source_notifications) == 1
    source_ref = json.loads(source_notifications[0]["source_ref"])
    assert source_ref["source_id"] == sid
    assert source_ref["status"] == "recovered"
    reopened.close()
    q.close()


def test_scheduler_enqueues_stale_checked_source(tmp_path):
    workspace = tmp_path / "ws"
    p = _project(workspace)
    stale = SourceStore(p).add_source(
        name="Stale",
        kind="rss",
        url="https://example.com/stale.xml",
        schedule="@hourly",
    )
    p.db.execute(
        "UPDATE sources SET last_checked_at=? WHERE id=?",
        ("2026-06-13 10:00:00", stale),
    )
    p.db.commit()
    p.close()
    q = SqliteJobQueue(workspace / ".queue.db")

    jobs = enqueue_due_source_polls(
        workspace_root=workspace,
        queue=q,
        now=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )

    assert len(jobs) == 1
    assert jobs[0]["source_id"] == stale
    reopened = Project(workspace / "news.frisket")
    source_notifications = [
        dict(row)
        for row in reopened.db.execute(
            "SELECT * FROM notification_items WHERE source_kind='source'"
        ).fetchall()
    ]
    assert len(source_notifications) == 1
    source_ref = json.loads(source_notifications[0]["source_ref"])
    assert source_ref["source_id"] == stale
    assert source_ref["status"] == "stale"
    assert source_ref["stale_reason"] == "missed_two_intervals"
    reopened.close()
    q.close()


def test_hosted_source_poll_handler_enqueues_followups_under_claimed_root(tmp_path):
    """The opener branch must still know WHICH directory the poll opened.

    A hosted registration injects a ProjectOpener, so the handler never reads
    the payload's workspace_root; the enclosure/embedding jobs it enqueues
    still have to carry the claimed project root.
    """
    from pathlib import Path

    from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
    from frisket.project_identity import ProjectStorageKey

    projects_root = tmp_path / "projects"
    workspace = projects_root / "23"
    p = _project(workspace, "news")
    sid = SourceStore(p).add_source(
        name="Podcast",
        kind="rss",
        url="https://example.com/podcast.xml",
        schedule="@hourly",
        config={"download_enclosures": "queued"},
    )
    p.close()

    q = SqliteJobQueue(tmp_path / "queue.db", hosted=True)
    key = ProjectStorageKey(storage_org_id=23, project_slug="news")
    opened: list[tuple[ProjectStorageKey, Path]] = []

    def opener(storage_key, path):
        opened.append((storage_key, Path(path)))
        return Project(path)

    reg = default_registry()
    register_source_poll_handler(
        reg,
        workspace_root=projects_root,
        queue=q,
        fetch=lambda url: _feed(
            [
                {
                    "guid": "ep1",
                    "title": "Episode One",
                    "link": "https://x/ep1",
                    "enclosure": {
                        "url": "https://media.example/ep1.mp3",
                        "type": "audio/mpeg",
                        "length": 123,
                    },
                }
            ],
            feed_title="Podcast",
        ),
        project_opener=opener,
    )
    handler = reg.get(SOURCE_POLL_KIND)
    assert handler is not None

    result = handler(
        {
            CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
            "project_id": "payload-attacker",
            "storage_org_id": 23,
            "workspace_root": str(tmp_path / "payload-root"),
            "source_id": sid,
        },
        JobHandlerContext.without_job_row(),
    )

    assert opened == [(key, workspace / "news.frisket")]
    assert result["project_id"] == "news"
    assert result["new_rows"] == 1
    assert result["enclosure_jobs"] == 1
    enclosure_jobs = [
        job for job in q.list_jobs(status="queued") if job.kind != SOURCE_POLL_KIND
    ]
    assert [job.payload["workspace_root"] for job in enclosure_jobs] == [str(workspace)]

    reopened = Project(workspace / "news.frisket")
    runs = [dict(r) for r in SourceStore(reopened).source_runs(sid)]
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    reopened.close()
    q.close()


def test_scheduler_keeps_workspace_roots_distinct_for_hosted_slugs(tmp_path):
    data = tmp_path / "data"
    root1 = data / "projects" / "1"
    root2 = data / "projects" / "2"
    root1.mkdir(parents=True)
    root2.mkdir(parents=True)
    p1 = Project.create(root1 / "news.frisket", name="news")
    sid1 = SourceStore(p1).add_source(
        name="Org 1",
        kind="rss",
        url="https://example.com/1.xml",
        schedule="@hourly",
    )
    p1.close()
    p2 = Project.create(root2 / "news.frisket", name="news")
    sid2 = SourceStore(p2).add_source(
        name="Org 2",
        kind="rss",
        url="https://example.com/2.xml",
        schedule="@hourly",
    )
    p2.close()
    q = SqliteJobQueue(data / "queue" / ".queue.db")

    jobs = []
    jobs.extend(
        enqueue_due_source_polls(
            workspace_root=root1,
            queue=q,
            now=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
        )
    )
    jobs.extend(
        enqueue_due_source_polls(
            workspace_root=root2,
            queue=q,
            now=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
        )
    )

    assert len(jobs) == 2
    assert {job["source_id"] for job in jobs} == {sid1, sid2}
    roots = {job.payload["workspace_root"] for job in q.list_jobs(status="queued")}
    assert roots == {str(root1), str(root2)}
    q.close()
