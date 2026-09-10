from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.executor.actions import run_action_spec
from frisket.server.sources.rss import (
    RssPoller,
    register_rss_poller,
)
from frisket.server.sources.runtime import (
    register_source_poller,
)
from frisket.engine.store import Project


PROJECT_ID = "project-rss-source-poll-convergence"


def _source_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "rss_source_poll@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source_id is not None:
        params["source"] = source_id
    if source is not None:
        params["source"] = source
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _feed(items: list[dict[str, Any]], feed_title: str = "Policy Feed") -> str:
    entries: list[str] = []
    for item in items:
        parts: list[str] = []
        if "title" in item:
            parts.append(f"<title>{item['title']}</title>")
        if "link" in item:
            parts.append(f"<link>{item['link']}</link>")
        if "guid" in item:
            parts.append(f"<guid isPermaLink='false'>{item['guid']}</guid>")
        if "pubDate" in item:
            parts.append(f"<pubDate>{item['pubDate']}</pubDate>")
        if "description" in item:
            parts.append(f"<description>{item['description']}</description>")
        if "enclosure" in item:
            enclosure = item["enclosure"]
            attrs = f"url='{enclosure['url']}'"
            if "length" in enclosure:
                attrs += f" length='{enclosure['length']}'"
            if "type" in enclosure:
                attrs += f" type='{enclosure['type']}'"
            parts.append(f"<enclosure {attrs} />")
        entries.append("<item>" + "".join(parts) + "</item>")
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        f"<title>{feed_title}</title>" + "".join(entries) + "</channel></rss>"
    )


def _new_source(url: str = "https://feeds.example/policy.xml") -> dict[str, Any]:
    return {
        "kind": "rss",
        "name": "Policy Feed",
        "url": url,
        "config": {"merge_strategy": "append-new"},
    }


def test_source_poll_rss_uses_source_items_and_generic_enclosure_refs(
    tmp_path: Path,
) -> None:
    feed = _feed(
        [
            {
                "title": "Episode",
                "link": "https://example.test/episode",
                "guid": "episode-1",
                "pubDate": "Tue, 24 Jun 2026 10:00:00 GMT",
                "description": "Audio episode",
                "enclosure": {
                    "url": "https://media.example.test/episode.mp3",
                    "type": "audio/mpeg",
                    "length": "12345",
                },
            }
        ]
    )
    register_source_poller(RssPoller(fetch=lambda _url: feed), replace=True)
    project = Project.create(tmp_path / "rss-source-poll.frisket", name="RSS poll")
    try:
        first = run_action_spec(
            project,
            _source_poll_action(source=_new_source()),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors
        assert first.receipt_id is not None
        assert first.op_ids
        source_ref = next(
            output.ref for output in first.outputs if output.kind == "source"
        )
        source_id = int(source_ref["source_id"])

        receipt = _receipt(project, first.receipt_id)
        assert receipt["action_kind"] == "source.poll"
        refs = [
            item["ref"]
            for item in receipt["inputs"] + receipt["outputs"] + receipt["evidence"]
        ]
        ref_kinds = {ref["kind"] for ref in refs}
        assert {
            "source_poll_run",
            "source_poll_rows",
            "source_poll_cursor",
        } <= ref_kinds
        assert "rss_source_run" not in ref_kinds
        assert "rss_feed_rows" not in ref_kinds
        enclosure_ref = next(
            ref for ref in refs if ref["kind"] == "source_poll_enclosure_pointers"
        )
        assert enclosure_ref["may_feed"] == ["media.enclosure_materialize"]
        assert enclosure_ref["row_ids"] == first.outputs[-1].row_ids

        assert (
            project.db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        )
        row = project.db.execute("SELECT * FROM source_items").fetchone()
        assert row is not None
        assert row["dedupe_key"] == "episode-1"

        second = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="rss_source_poll@sha256:second",
            ),
            project_id=PROJECT_ID,
        )
        assert second.status == "completed", second.errors
        assert second.op_ids == []
        source_run = next(
            output.ref for output in second.outputs if output.kind == "source_run"
        )
        assert source_run["skipped_rows"] == 1
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        )
    finally:
        project.close()
        register_rss_poller(replace=True)


def _duplicate_key_feed() -> str:
    """Two entries share one guid and differ in content.

    Malformed-but-real feeds do this (a republished item keeping its guid, a
    generator that omits guid so `link` becomes the key). Both entries map to
    the same `dedupe_key`, which is exactly the case a per-item SELECT taken
    before any of the batch's own writes cannot see.
    """
    return _feed(
        [
            {
                "title": "Same guid, first copy",
                "link": "https://example.test/dup",
                "guid": "dup-1",
                "description": "First body",
            },
            {
                "title": "Same guid, second copy",
                "link": "https://example.test/dup",
                "guid": "dup-1",
                "description": "Second body",
            },
        ]
    )


def _sheet_row_count(project: Project) -> int:
    return int(
        project.db.execute("SELECT COUNT(*) AS count FROM rows").fetchone()["count"]
    )


def test_repeated_polls_do_not_grow_rows_when_a_batch_repeats_a_dedupe_key(
    tmp_path: Path,
) -> None:
    """A same-batch duplicate must not make every later poll append a row.

    Before the fix the two entries were invisible to each other (each item's
    existence check ran against the pre-batch table), so the first poll wrote
    two rows for one dedupe_key and every subsequent poll flip-flopped the
    stored item_hash between the two bodies, appending one revision row per
    poll with zero new items: 2, 3, 4, ... 9 over eight polls.
    """
    feed = _duplicate_key_feed()
    register_source_poller(RssPoller(fetch=lambda _url: feed), replace=True)
    project = Project.create(tmp_path / "rss-dup-key.frisket", name="RSS dup")
    try:
        first = run_action_spec(
            project,
            _source_poll_action(
                source=_new_source(url="https://feeds.example/dup.xml"),
                idempotency_key="rss_dup_poll@sha256:0",
            ),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors
        source_ref = next(
            output.ref for output in first.outputs if output.kind == "source"
        )
        source_id = int(source_ref["source_id"])

        # One dedupe_key -> one source_items row -> one materialized row.
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        )
        after_first = _sheet_row_count(project)
        assert after_first == 1

        counts = [after_first]
        for poll in range(1, 9):
            result = run_action_spec(
                project,
                _source_poll_action(
                    source_id=source_id,
                    idempotency_key=f"rss_dup_poll@sha256:{poll}",
                ),
                project_id=PROJECT_ID,
            )
            assert result.status == "completed", result.errors
            counts.append(_sheet_row_count(project))

        assert counts == [after_first] * 9, counts
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        )
    finally:
        project.close()
        register_rss_poller(replace=True)


def test_same_batch_duplicate_dedupe_key_is_collapsed_not_appended(
    tmp_path: Path,
) -> None:
    """The fence: a duplicate inside one batch is collapsed, and counted as such.

    This is the wire that must go red if the batch-level collapse is removed:
    two entries in, one row out, and the poll reports the drop rather than
    silently double-writing.
    """
    feed = _duplicate_key_feed()
    register_source_poller(RssPoller(fetch=lambda _url: feed), replace=True)
    project = Project.create(tmp_path / "rss-dup-fence.frisket", name="RSS dup fence")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=_new_source(url="https://feeds.example/dup-fence.xml"),
                idempotency_key="rss_dup_fence@sha256:first",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        source_run = next(
            output.ref for output in result.outputs if output.kind == "source_run"
        )
        assert source_run["new_rows"] == 1
        assert _sheet_row_count(project) == 1
        items = project.db.execute("SELECT * FROM source_items").fetchall()
        assert len(items) == 1
        assert items[0]["dedupe_key"] == "dup-1"
        # The surviving row is the last copy in feed order.
        row_id = int(items[0]["row_id"])
        title = project.db.execute(
            "SELECT c.value AS value FROM cells c JOIN columns col "
            "ON col.id=c.column_id WHERE c.row_id=? AND col.name='title'",
            (row_id,),
        ).fetchone()
        assert title is not None
        assert json.loads(title["value"]) == "Same guid, second copy"
    finally:
        project.close()
        register_rss_poller(replace=True)


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])
