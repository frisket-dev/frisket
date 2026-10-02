"""Bounded maintenance, crash replay, and honest latest-only search."""

import pytest

from frisket.engine.store import Project
from frisket.search import (
    SearchIndexNotReady,
    drain_index,
    fresh_sidecar,
    index_batch,
    search_project,
    search_project_page,
)
import frisket.search_index as maintenance


@pytest.fixture
def documents(tmp_path):
    project = Project.create(tmp_path / "search.frisket", name="search")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "body")
    rows = project.add_rows(
        sheet, [{"body": f"needle document {i}"} for i in range(7)], {"body": column}
    )
    try:
        yield project, sheet, column, rows
    finally:
        project.close()


def test_readers_do_not_rebuild_and_batches_are_bounded(documents):
    project, *_ = documents
    with pytest.raises(SearchIndexNotReady):
        search_project(project, "needle", rerank="off")
    assert search_project_page(project, "needle", rerank="off") == {
        "hits": [],
        "complete": False,
    }
    assert not (project.path / "project.search.db").exists()
    for _ in range(100):
        progress = index_batch(project, batch_size=2)
        assert progress.processed <= 2
        if progress.complete:
            break
    else:
        pytest.fail("bounded indexing did not finish")
    assert len(search_project(project, "needle", rerank="off")) == 7


def test_partial_results_hide_changed_and_deleted_text(documents):
    project, sheet, column, rows = documents
    drain_index(project)
    project.apply_edits(
        [{"row_id": rows[0], "column_id": column, "value": "replacement"}]
    )
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[1],))
    project.db.commit()
    page = search_project_page(project, "needle", rerank="off")
    assert page["complete"] is False
    assert {hit["row_id"] for hit in page["hits"]} == set(rows[2:])
    drain_index(project, batch_size=2)
    assert search_project(project, "replacement", rerank="off")[0]["row_id"] == rows[0]
    assert len(search_project(project, "needle", rerank="off")) == 5


def test_crash_after_index_commit_before_ack_replays(documents, monkeypatch):
    project, *_ = documents
    original = maintenance.advance_dirty_scope

    def crash(*args, **kwargs):
        raise RuntimeError("crash before checkpoint")

    monkeypatch.setattr(maintenance, "advance_dirty_scope", crash)
    with pytest.raises(RuntimeError, match="crash before checkpoint"):
        index_batch(project, batch_size=2)
    monkeypatch.setattr(maintenance, "advance_dirty_scope", original)
    assert drain_index(project, batch_size=2) == 7
    assert len(search_project(project, "needle", rerank="off")) == 7


def test_changed_cell_behind_backfill_cursor_is_reconciled(documents):
    project, _, column, rows = documents
    index_batch(project, batch_size=2)
    project.apply_edits(
        [{"row_id": rows[0], "column_id": column, "value": "newneedle"}]
    )
    drain_index(project, batch_size=2)
    assert search_project(project, "newneedle", rerank="off")[0]["row_id"] == rows[0]


def test_old_source_snapshot_never_rebuilds_shared_index(documents):
    project, sheet, column, _ = documents
    drain_index(project)
    with project.read_snapshot() as old:
        db = fresh_sidecar(old)
        db.close()
        project.add_rows(sheet, [{"body": "laterneedle"}], {"body": column})
        drain_index(project)
        with pytest.raises(SearchIndexNotReady):
            fresh_sidecar(old)
    assert search_project(project, "laterneedle", rerank="off")


def test_partial_mutation_revokes_old_complete_checkpoint(documents):
    project, sheet, column, _ = documents
    drain_index(project)
    with project.read_snapshot() as old:
        db = fresh_sidecar(old)
        db.close()
        project.add_rows(
            sheet, [{"body": "laterneedle"}, {"body": "another"}], {"body": column}
        )
        assert not index_batch(project, batch_size=1).complete
        with pytest.raises(SearchIndexNotReady):
            fresh_sidecar(old)


def test_missing_sidecar_recovers_even_after_work_acknowledged(documents):
    project, *_ = documents
    drain_index(project)
    (project.path / "project.search.db").unlink()
    assert drain_index(project, batch_size=2) == 7


def test_pinned_index_remains_stable_during_later_update(documents):
    project, sheet, column, _ = documents
    drain_index(project)
    db = fresh_sidecar(project)
    try:
        project.add_rows(sheet, [{"body": "laterneedle"}], {"body": column})
        drain_index(project)
        assert db.execute("SELECT COUNT(*) FROM cell_fts").fetchone()[0] == 7
    finally:
        db.close()
    assert len(search_project(project, "laterneedle", rerank="off")) == 1


@pytest.mark.parametrize("change", ["rename", "hide", "delete"])
def test_column_lifecycle_reconciles_indexed_identities(documents, change):
    project, _, column, _ = documents
    drain_index(project)
    if change == "rename":
        project.db.execute("UPDATE columns SET name='renamed' WHERE id=?", (column,))
    elif change == "hide":
        project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column,))
    else:
        project.db.execute("DELETE FROM columns WHERE id=?", (column,))
    project.db.commit()
    assert search_project_page(project, "needle", rerank="off")["hits"] == []
    drain_index(project, batch_size=2)
    hits = search_project(project, "needle", rerank="off")
    if change == "rename":
        assert len(hits) == 7
        assert {hit["column_name"] for hit in hits} == {"renamed"}
    else:
        assert hits == []


def test_foreground_read_does_not_wait_for_index_writer(documents):
    project, *_ = documents
    drain_index(project)
    from frisket.search import _sidecar

    writer = _sidecar(project)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert search_project_page(project, "needle", rerank="off")["complete"]
        reader = fresh_sidecar(project)
        reader.close()
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("size", [10, 2000])
def test_nonsearchable_columns_do_not_scan_cell_corpus(tmp_path, size):
    project = Project.create(tmp_path / "nontext.frisket", name="nontext")
    try:
        sheet = project.add_sheet("Documents")
        columns = {
            "amount": project.add_column(sheet, "amount", type="number"),
            "date": project.add_column(sheet, "date", type="date"),
            "file": project.add_column(sheet, "file", type="file"),
            "hidden": project.add_column(sheet, "hidden", hidden=True),
        }
        records = [
            {
                "amount": i,
                "date": "2026-10-02",
                "file": {"name": "a.pdf"},
                "hidden": "secret",
            }
            for i in range(size)
        ]
        project.add_rows(sheet, records, columns)
        progress = index_batch(project, batch_size=5)
        assert progress.complete and progress.processed <= 1
        # Explicit column-range invalidations from a subsequent append must
        # also skip live values, not only the initial whole-project walk.
        project.add_rows(sheet, records, columns)
        processed = 0
        for _ in range(10):
            progress = index_batch(project, batch_size=5)
            processed += progress.processed
            if progress.complete:
                break
        assert progress.complete and processed <= len(columns)
    finally:
        project.close()


def test_former_text_column_purges_only_previously_indexed_cells(documents):
    project, sheet, column, rows = documents
    drain_index(project)
    project.db.execute("UPDATE columns SET type='number' WHERE id=?", (column,))
    project.db.commit()
    project.add_rows(sheet, [{"body": i} for i in range(2000)], {"body": column})
    processed = 0
    for _ in range(10):
        progress = index_batch(project, batch_size=5)
        processed += progress.processed
        if progress.complete:
            break
    assert progress.complete and processed <= len(rows) + 3
    assert search_project(project, "needle", rerank="off") == []


def test_one_quantum_drains_many_tiny_scopes(documents):
    project, sheet, column, _ = documents
    drain_index(project)
    for i in range(30):
        project.add_rows(sheet, [{"body": f"newneedle {i}"}], {"body": column})
    progress = index_batch(project, batch_size=100)
    assert progress.complete and progress.processed <= 100
    assert len(search_project(project, "newneedle", rerank="off")) == 30


def test_byte_budget_loads_only_cells_it_can_process(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "bytes.frisket", name="bytes")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet,
            [{"body": "x" * (1536 * 1024) + f" tailneedle{i}"} for i in range(3)],
            {"body": column},
        )
        loaded = []
        read = maintenance._read_cell

        def observe(*args):
            loaded.append(args[-1])
            return read(*args)

        monkeypatch.setattr(maintenance, "_read_cell", observe)
        progress = index_batch(project)
        assert not progress.complete
        assert 3 * 1024 * 1024 < progress.processed_bytes < 4 * 1024 * 1024
        assert len(loaded) == 2
        drain_index(project)
        assert len(loaded) == 3
        assert search_project(project, "tailneedle2", rerank="off")
    finally:
        project.close()


def test_single_oversized_cell_keeps_full_searchable_content(tmp_path):
    project = Project.create(tmp_path / "oversized.frisket", name="oversized")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet,
            [
                {"body": "x" * (5 * 1024 * 1024) + " oversizedtail"},
                {"body": "nextcell"},
            ],
            {"body": column},
        )
        first = index_batch(project)
        assert first.processed == 1 and first.processed_bytes > 4 * 1024 * 1024
        assert not first.complete
        drain_index(project)
        assert search_project(project, "oversizedtail", rerank="off")
        assert search_project(project, "nextcell", rerank="off")
    finally:
        project.close()


def test_concurrent_scope_replacement_survives_deferred_ack(documents, monkeypatch):
    project, _, column, rows = documents
    drain_index(project)
    project.apply_edits([{"row_id": rows[0], "column_id": column, "value": "first"}])
    replace = maintenance._replace_cell
    changed = False

    def replace_then_change(*args):
        nonlocal changed
        replace(*args)
        if not changed:
            changed = True
            project.apply_edits(
                [{"row_id": rows[0], "column_id": column, "value": "concurrentneedle"}]
            )

    monkeypatch.setattr(maintenance, "_replace_cell", replace_then_change)
    assert not index_batch(project).complete
    drain_index(project)
    assert search_project(project, "concurrentneedle", rerank="off")
