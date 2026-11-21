from __future__ import annotations

import json
from typing import Any

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.feeds.rss import (
    RssFeedChecker,
    register_rss_feed_checker,
)
from frisket.feeds.runtime import (
    register_feed_checker,
)
from frisket.engine.store import Project


PROJECT_ID = "project-rss-source-poll-convergence"


def _feed_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "rss_source_poll@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source_id is not None:
        params["feed"] = source_id
    if source is not None:
        params["feed"] = source
    return {
        "action_id": "feed.poll",
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


def test_source_poll_rss_uses_keyed_observations_and_generic_enclosure_refs(
    make_project,
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
    register_feed_checker(RssFeedChecker(fetch=lambda _url: feed), replace=True)
    project = make_project("RSS poll")
    try:
        first = run_action_spec(
            project,
            _feed_poll_action(source=_new_source()),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors
        assert first.receipt_id is not None
        assert first.op_ids
        source_ref = next(
            output.ref for output in first.outputs if output.kind == "feed"
        )
        source_id = int(source_ref["feed_id"])

        receipt = _receipt(project, first.receipt_id)
        assert receipt["action_kind"] == "feed.poll"
        refs = [
            item["ref"]
            for item in receipt["inputs"] + receipt["outputs"] + receipt["evidence"]
        ]
        ref_kinds = {ref["kind"] for ref in refs}
        assert {
            "feed_poll_check",
            "feed_poll_rows",
            "feed_poll_cursor",
        } <= ref_kinds
        assert "rss_source_run" not in ref_kinds
        assert "rss_feed_rows" not in ref_kinds
        enclosure_ref = next(
            ref for ref in refs if ref["kind"] == "feed_poll_enclosure_pointers"
        )
        assert enclosure_ref["may_feed"] == ["media.enclosure_materialize"]
        assert enclosure_ref["row_ids"] == first.outputs[-1].row_ids

        assert (
            project.db.execute("SELECT COUNT(*) FROM keyed_observations").fetchone()[0]
            == 1
        )
        row = project.db.execute("SELECT * FROM keyed_observations").fetchone()
        assert row is not None
        assert json.loads(row["key"])[1] == ["text", "episode-1"]

        second = run_action_spec(
            project,
            _feed_poll_action(
                source_id=source_id,
                idempotency_key="rss_source_poll@sha256:second",
            ),
            project_id=PROJECT_ID,
        )
        assert second.status == "completed", second.errors
        assert second.op_ids == []
        source_run = next(
            output.ref for output in second.outputs if output.kind == "feed_check"
        )
        assert source_run["skipped_rows"] == 1
        assert (
            project.db.execute("SELECT COUNT(*) FROM keyed_observations").fetchone()[0]
            == 1
        )
    finally:
        project.close()
        register_rss_feed_checker(replace=True)


def _duplicate_key_feed(*, conflicting: bool = True) -> str:
    """Two entries share one guid, with either identical or conflicting content.

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
                "title": "Same guid, second copy"
                if conflicting
                else "Same guid, first copy",
                "link": "https://example.test/dup",
                "guid": "dup-1",
                "description": "Second body" if conflicting else "First body",
            },
        ]
    )


def _sheet_row_count(project: Project) -> int:
    return int(
        project.db.execute("SELECT COUNT(*) AS count FROM rows").fetchone()["count"]
    )


@pytest.mark.parametrize("conflicting", [False, True])
def test_repeated_polls_do_not_grow_rows_when_a_batch_repeats_a_dedupe_key(
    make_project,
    conflicting: bool,
) -> None:
    """A same-batch duplicate must not make every later poll append a row.

    Before the fix the two entries were invisible to each other (each item's
    existence check ran against the pre-batch table), so the first poll wrote
    two rows for one dedupe_key and every subsequent poll flip-flopped the
    stored item_hash between the two bodies, appending one revision row per
    poll with zero new items: 2, 3, 4, ... 9 over eight polls.
    """
    feed = _duplicate_key_feed(conflicting=conflicting)
    register_feed_checker(RssFeedChecker(fetch=lambda _url: feed), replace=True)
    project = make_project("RSS dup")
    try:
        first = run_action_spec(
            project,
            _feed_poll_action(
                source=_new_source(url="https://feeds.example/dup.xml"),
                idempotency_key="rss_dup_poll@sha256:0",
            ),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors
        source_ref = next(
            output.ref for output in first.outputs if output.kind == "feed"
        )
        source_id = int(source_ref["feed_id"])

        # Identical keys collapse; conflicting groups are skipped as ambiguous.
        assert project.db.execute("SELECT COUNT(*) FROM keyed_observations").fetchone()[
            0
        ] == (0 if conflicting else 1)
        after_first = _sheet_row_count(project)
        assert after_first == (0 if conflicting else 1)

        counts = [after_first]
        for poll in range(1, 9):
            result = run_action_spec(
                project,
                _feed_poll_action(
                    source_id=source_id,
                    idempotency_key=f"rss_dup_poll@sha256:{poll}",
                ),
                project_id=PROJECT_ID,
            )
            assert result.status == "completed", result.errors
            counts.append(_sheet_row_count(project))

        assert counts == [after_first] * 9, counts
        assert project.db.execute("SELECT COUNT(*) FROM keyed_observations").fetchone()[
            0
        ] == (0 if conflicting else 1)
    finally:
        project.close()
        register_rss_feed_checker(replace=True)


def test_same_batch_duplicate_dedupe_key_is_collapsed_not_appended(
    make_project,
) -> None:
    """The fence: a duplicate inside one batch is collapsed, and counted as such.

    This is the wire that must go red if the batch-level collapse is removed:
    two entries in, one row out, and the poll reports the drop rather than
    silently double-writing.
    """
    feed = _duplicate_key_feed(conflicting=False)
    register_feed_checker(RssFeedChecker(fetch=lambda _url: feed), replace=True)
    project = make_project("RSS dup fence")
    try:
        result = run_action_spec(
            project,
            _feed_poll_action(
                source=_new_source(url="https://feeds.example/dup-fence.xml"),
                idempotency_key="rss_dup_fence@sha256:first",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        source_run = next(
            output.ref for output in result.outputs if output.kind == "feed_check"
        )
        sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        assert source_run["new_rows"] == 1
        assert _sheet_row_count(project) == 1
        items = project.db.execute("SELECT * FROM keyed_observations").fetchall()
        assert len(items) == 1
        assert json.loads(items[0]["key"])[1] == ["text", "dup-1"]
        # Equal records collapse; conflicting records are rejected as a group.
        row_id = int(items[0]["row_id"])
        title_column_id = next(
            int(column["id"])
            for column in project.columns(sheet_id, include_hidden=True)
            if column["name"] == "title"
        )
        assert (
            project.get_values(sheet_id, title_column_id, row_ids=[row_id])[row_id]
            == "Same guid, first copy"
        )
    finally:
        project.close()
        register_rss_feed_checker(replace=True)


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])
