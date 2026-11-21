"""Manual feed imports return a destination before provider listing begins."""

from types import SimpleNamespace

import pytest

from frisket.engine.jobs import SqliteJobQueue, Worker
from frisket.engine.store.feeds import FeedStore
from frisket.server.feed_enqueue import queue_feed_poll
from frisket.feeds.progress import listing_progress_options
from frisket.feeds.rss import ensure_rss_feed_checker
from test_feed_scheduler import _feed, _project, _register
from frisket.engine.store.transactions import write_transaction


def test_manual_poll_returns_empty_sheet_then_worker_populates(tmp_path):
    workspace = tmp_path / "ws"
    project = _project(workspace)
    ensure_rss_feed_checker()
    with write_transaction(project.db):
        source_id = FeedStore(project).add_feed(
            name="My feed", kind="rss", url="https://example.com/feed"
        )
    queue = SqliteJobQueue(workspace / ".queue.db")
    ctx = SimpleNamespace(queue=queue, queue_payload_extra={}, workspace_root=workspace)
    body = {
        "action_id": "feed.poll",
        "scope": {"kind": "project"},
        "params": {"feed": source_id},
        "idempotency_key": "manual-feed-1",
    }
    result = queue_feed_poll(project, "news", body, ctx)
    assert result.status == "queued"
    sheet_id = result.outputs[0].sheet_id
    assert dict(
        project.db.execute("SELECT name FROM sheets WHERE id=?", (sheet_id,)).fetchone()
    ) == {"name": "My feed"}
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        == 0
    )
    assert queue_feed_poll(project, "news", body, ctx).job_id == result.job_id
    project.close()

    def fetch(url):
        running = queue.get(result.job_id)
        assert running.status == "running"
        assert running.result["completed_items"] == 0
        return _feed([{"guid": "a", "title": "Alpha", "link": "https://example.com/a"}])

    worker = Worker(queue, _register(workspace, fetch, queue=queue))
    assert worker.run_once()
    completed = queue.get(result.job_id)
    assert completed.status == "done", completed.error
    assert completed.result["sheet_id"] == sheet_id
    assert completed.result["new_rows"] == 1
    # An obsolete progress callback cannot overwrite a terminal result.
    assert not queue.report_progress(
        result.job_id, completed.attempts, {"completed_items": 99}
    )
    assert queue.get(result.job_id).result["new_rows"] == 1
    queue.close()


def test_metadata_progress_counts_unique_entries_and_honors_cancellation():
    updates = []
    cancelled = False
    options = listing_progress_options(
        lambda count, total: updates.append((count, total)), lambda: cancelled, 20
    )
    observe = options["match_filter"]
    observe({"id": "playlist"}, incomplete=True)
    observe({"id": "first", "playlist_index": 1}, incomplete=True)
    observe({"id": "first", "playlist_index": 1})
    observe({"id": "second", "playlist_index": 2})
    assert updates == [(1, 20), (2, 20)]
    cancelled = True
    with pytest.raises(RuntimeError, match="cancelled"):
        observe({"id": "third", "playlist_index": 3})
