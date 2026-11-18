from __future__ import annotations

import json

import pytest

from frisket.engine.store.feeds import FeedStore


def test_feed_store_preserves_feed_check_lifecycle(
    make_project,
) -> None:
    project = make_project("Sources")
    store = FeedStore(project)

    source_id = store.add_feed(
        "Feed",
        kind="rss",
        url="https://example.test/feed.xml",
        config={"title_field": "title"},
        schedule="hourly",
    )
    source = store.get_feed(source_id)
    assert source is not None
    assert source["last_status"] == "never"
    assert json.loads(source["config"]) == {"title_field": "title"}

    store.update_feed(source_id, enabled=False, config={"title_field": "headline"})
    source = store.get_feed(source_id)
    assert source is not None
    assert source["enabled"] == 0
    assert json.loads(source["config"]) == {"title_field": "headline"}
    with pytest.raises(ValueError, match="unknown feed field"):
        store.update_feed(source_id, unknown=True)

    source_run_id = store.start_feed_check(source_id, cursor_before="cursor-1")
    store.finish_feed_check(
        source_run_id,
        status="ok",
        new_rows=1,
        revisions=1,
        cursor_after="cursor-2",
        summary={"seen": 2},
    )
    runs = store.feed_checks_page(source_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert json.loads(runs[0]["summary_json"]) == {"seen": 2}
    assert store.feed_checks_total(source_id) == 1
    source = store.get_feed(source_id)
    assert source is not None
    assert source["last_status"] == "ok"
    assert source["new_rows_total"] == 2
    assert source["cursor"] == "cursor-2"
