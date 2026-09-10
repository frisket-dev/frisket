from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


def test_source_store_preserves_source_run_and_dedupe_lifecycle(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "sources.frisket", name="Sources")
    store = SourceStore(project)

    source_id = store.add_source(
        "Feed",
        kind="rss",
        url="https://example.test/feed.xml",
        config={"title_field": "title"},
        schedule="hourly",
    )
    source = store.get_source(source_id)
    assert source is not None
    assert source["last_status"] == "never"
    assert json.loads(source["config"]) == {"title_field": "title"}

    store.update_source(source_id, enabled=False, config={"title_field": "headline"})
    source = store.get_source(source_id)
    assert source is not None
    assert source["enabled"] == 0
    assert json.loads(source["config"]) == {"title_field": "headline"}
    with pytest.raises(ValueError, match="unknown source field"):
        store.update_source(source_id, unknown=True)

    source_run_id = store.start_source_run(source_id, cursor_before="cursor-1")
    store.upsert_source_item(
        source_id=source_id,
        source_item_id="item-1",
        dedupe_key="dedupe-1",
        item_hash="sha256:first",
        row_id=None,
        source_run_id=source_run_id,
        revision=1,
        raw_ref={"url": "https://example.test/a"},
    )
    store.upsert_source_item(
        source_id=source_id,
        source_item_id="item-1b",
        dedupe_key="dedupe-1",
        item_hash="sha256:second",
        row_id=None,
        source_run_id=source_run_id,
        revision=2,
        raw_ref={"url": "https://example.test/b"},
    )
    item = store.source_item_for_dedupe(source_id, "dedupe-1")
    assert item is not None
    assert item["source_item_id"] == "item-1b"
    assert item["revision"] == 2
    assert json.loads(item["raw_ref_json"]) == {"url": "https://example.test/b"}

    store.finish_source_run(
        source_run_id,
        status="ok",
        new_rows=1,
        revisions=1,
        cursor_after="cursor-2",
        summary={"seen": 2},
    )
    runs = store.source_runs(source_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert json.loads(runs[0]["summary_json"]) == {"seen": 2}
    assert store.source_runs_total(source_id) == 1
    source = store.get_source(source_id)
    assert source is not None
    assert source["last_status"] == "ok"
    assert source["new_rows_total"] == 2
    assert source["cursor"] == "cursor-2"


def test_upsert_source_item_is_authoritative_without_a_prior_read(
    tmp_path: Path,
) -> None:
    """A second write for one key must collapse even inside one open txn.

    The store is what makes a duplicate unrepresentable: the conflict is
    resolved by the UNIQUE(source_id, dedupe_key) constraint, not by a SELECT
    that ran first. So this exercises the uncommitted case (commit=False, one
    connection) and asserts first_seen_run_id survives the update arm while
    an omitted revision leaves the ledger's count alone.
    """
    project = Project.create(tmp_path / "upsert.frisket", name="Upsert")
    store = SourceStore(project)
    source_id = store.add_source("Feed", kind="rss", url="https://example.test/f.xml")
    first_run = store.start_source_run(source_id)
    second_run = store.start_source_run(source_id)

    store.upsert_source_item(
        source_id=source_id,
        source_item_id="item-1",
        dedupe_key="dup",
        item_hash="sha256:first",
        row_id=None,
        source_run_id=first_run,
        revision=3,
        commit=False,
    )
    store.upsert_source_item(
        source_id=source_id,
        source_item_id="item-2",
        dedupe_key="dup",
        item_hash="sha256:second",
        row_id=None,
        source_run_id=second_run,
        commit=False,
    )
    project.db.commit()

    rows = project.db.execute(
        "SELECT * FROM source_items WHERE source_id=? AND dedupe_key=?",
        (source_id, "dup"),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source_item_id"] == "item-2"
    assert rows[0]["item_hash"] == "sha256:second"
    assert rows[0]["first_seen_run_id"] == first_run
    assert rows[0]["last_seen_run_id"] == second_run
    # revision omitted on the second call -> the ledger keeps its count.
    assert rows[0]["revision"] == 3
    project.close()
