"""Bounded unified retrieval maintenance and freshness."""

from __future__ import annotations

import pytest

from frisket.engine.jobs.search_index import process_index_batches
from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.retrieval.store import RETRIEVAL_DB_NAME, RetrievalStore
from frisket.search import SearchIndexNotReady, drain_index, index_batch, search_project
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


def test_readers_do_not_prepare_and_maintenance_batches_are_bounded(documents):
    project, *_ = documents
    with pytest.raises(SearchIndexNotReady):
        search_project(project, "needle", rerank="off")
    assert not (project.path / RETRIEVAL_DB_NAME).exists()

    for _ in range(100):
        progress = index_batch(project, batch_size=2)
        assert progress.processed <= 2
        if progress.complete:
            break
    else:
        pytest.fail("bounded indexing did not finish")

    assert len(search_project(project, "needle", rerank="off")) == 7


def test_completed_dirty_pass_analyzes_the_retrieval_sidecar(documents):
    project, *_ = documents

    class Encoder:
        def available(self):
            return True

        def encode(self, inputs, *, purpose, space, cancelled=None):
            return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]

    for _ in range(20):
        _processed, complete = process_index_batches(
            project, embedding_encoder=Encoder()
        )
        if complete:
            break
    else:
        pytest.fail("retrieval maintenance never completed")

    with RetrievalStore(project).open() as store:
        stats = {
            str(row[0]) for row in store.db.execute("SELECT tbl FROM sqlite_stat1")
        }
    assert {
        "bindings",
        "lexical_sources",
        "part_representations",
        "parts",
        "representations",
        "sources",
    } <= stats


def test_dirty_edit_is_not_reported_as_fresh_until_maintenance_finishes(documents):
    project, _sheet, column, rows = documents
    drain_index(project)
    project.apply_edits(
        [{"row_id": rows[0], "column_id": column, "value": "replacementneedle"}]
    )

    assert {
        hit["row_id"] for hit in search_project(project, "needle", rerank="off")
    } == set(rows[1:])
    assert search_project(project, "replacementneedle", rerank="off") == []

    drain_index(project, batch_size=2)
    assert (
        search_project(project, "replacementneedle", rerank="off")[0]["row_id"]
        == rows[0]
    )
    assert {
        hit["row_id"] for hit in search_project(project, "needle", rerank="off")
    } == set(rows[1:])


def test_crash_before_dirty_ack_replays_committed_publication(documents, monkeypatch):
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


def test_deleted_unified_sidecar_is_rebuilt_from_authoritative_cells(documents):
    project, *_ = documents
    drain_index(project)
    (project.path / RETRIEVAL_DB_NAME).unlink()

    assert drain_index(project, batch_size=2) == 7
    assert len(search_project(project, "needle", rerank="off")) == 7


def test_byte_budget_loads_only_the_admitted_cell_page(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "bytes.frisket", name="bytes")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet,
            [{"body": "x" * (1536 * 1024) + f" tailneedle{i}"} for i in range(4)],
            {"body": column},
        )
        loaded = []
        get_values = ProjectReadSnapshot.get_values

        def observe(snapshot, sheet_id, column_id, row_ids=None, **kwargs):
            loaded.append(list(row_ids or []))
            return get_values(snapshot, sheet_id, column_id, row_ids=row_ids, **kwargs)

        monkeypatch.setattr(ProjectReadSnapshot, "get_values", observe)
        progress = index_batch(project)

        assert not progress.complete
        assert 3 * 1024 * 1024 < progress.processed_bytes < 4 * 1024 * 1024
        assert loaded == [project.visible_row_ids(sheet)[:2]]
        drain_index(project)
        assert search_project(project, "tailneedle3", rerank="off")
    finally:
        project.close()


def test_concurrent_edit_during_publication_remains_dirty_and_is_reconciled(
    documents, monkeypatch
):
    project, _sheet, column, rows = documents
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
                [
                    {
                        "row_id": rows[0],
                        "column_id": column,
                        "value": "concurrentneedle",
                    }
                ]
            )

    monkeypatch.setattr(maintenance, "_replace_cell", replace_then_change)
    assert not index_batch(project).complete
    drain_index(project)
    assert (
        search_project(project, "concurrentneedle", rerank="off")[0]["row_id"]
        == rows[0]
    )


def test_removing_last_content_still_schedules_existing_index_cleanup(documents):
    from frisket.engine.jobs.search_index import index_work_marker

    project, *_ = documents
    drain_index(project)
    assert search_project(project, "needle", rerank="off")
    project.undo()
    assert index_work_marker(project) is not None
    drain_index(project)
    assert search_project(project, "needle", rerank="off") == []
    assert index_work_marker(project, include_preparation=False) is None
