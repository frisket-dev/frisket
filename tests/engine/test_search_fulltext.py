"""Complete-cell FTS storage and bounded search consumers."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

import pytest

import frisket.search as search_mod
from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.search import (
    SearchIndexNotReady,
    _sidecar,
    drain_index,
    fts_indexed_at_op,
    rebuild_index,
    search_cells_scoped,
    search_project,
    search_sheet,
)
from frisket.search_index import index_needs_work
from frisket.search_storage import reclaim_is_pending
from frisket.semantic import (
    SEMANTIC_CELL_PREFIX_CHARS,
    SEMANTIC_COVERAGE,
    semantic_search,
)


NEEDLE = "nebulaquartz"


def test_relative_project_path_indexes_and_searches(tmp_path, monkeypatch):
    seeded = Project.create(tmp_path / "relative.frisket", name="relative")
    sheet = seeded.add_sheet("Documents")
    column = seeded.add_column(sheet, "body")
    [row] = seeded.add_rows(sheet, [{"body": "relativepathneedle"}], {"body": column})
    seeded.close()

    monkeypatch.chdir(tmp_path)
    project = Project(Path("relative.frisket"))
    try:
        drain_index(project)

        assert not index_needs_work(project)
        assert not reclaim_is_pending(project.path / "project.search.db")
        assert (
            search_project(project, "relativepathneedle", rerank="off")[0]["row_id"]
            == row
        )
    finally:
        project.close()


def _import_writer(project: Project) -> StreamingSheetWriter:
    return StreamingSheetWriter.start_session(
        project,
        session_id="import:search",
        writer_authority="claim:search",
        sheet_name="Documents",
        columns=[{"name": "body", "type": "text"}],
        project_id="search-project",
        action_kind="import.files",
        idempotency_key="search:one",
        params_hash="sha256:search",
        action_id="act:search",
        receipt_id="receipt:search",
        source_ref={"kind": "file_inventory", "inventory_id": "inventory:search"},
    )


def test_search_tracks_import_pages_and_removal_under_one_op(tmp_path):
    project = Project.create(tmp_path / "pages.frisket", name="pages")
    try:
        writer = _import_writer(project)
        op_cursor = project.op_cursor
        writer.append_page([{"body": "firstneedle"}], expected_cursor=0, next_cursor=1)
        drain_index(project)
        assert search_project(project, "firstneedle", rerank="off")
        writer.append_page([{"body": "secondneedle"}], expected_cursor=1, next_cursor=2)
        assert project.op_cursor == op_cursor
        with pytest.raises(SearchIndexNotReady):
            search_project(project, "secondneedle", rerank="off")
        drain_index(project)
        assert search_project(project, "secondneedle", rerank="off")
        writer.cancel(expected_cursor=2)
        assert search_project(project, "firstneedle", rerank="off")
        writer.remove_session(expected_cursor=2)
        assert project.op_cursor == op_cursor
        drain_index(project)
        assert search_project(project, "firstneedle", rerank="off") == []
        assert search_project(project, "secondneedle", rerank="off") == []
    finally:
        project.close()


def test_missing_revision_requires_explicit_maintenance(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "unstamped.frisket", name="unstamped")
    try:
        writer = _import_writer(project)
        writer.append_page([{"body": NEEDLE}], expected_cursor=0, next_cursor=1)
        rebuild_index(project)
        db = _sidecar(project)
        db.execute("DELETE FROM fts_state WHERE key='complete_revision'")
        db.commit()
        db.close()
        rebuilds = []
        original = search_mod.rebuild_index

        def rebuild(*args, **kwargs):
            rebuilds.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(search_mod, "rebuild_index", rebuild)
        with pytest.raises(SearchIndexNotReady):
            search_project(project, NEEDLE, rerank="off")
        drain_index(project)
        assert search_project(project, NEEDLE, rerank="off")
        writer.pause(expected_cursor=1)
        assert search_project(project, NEEDLE, rerank="off")
        assert rebuilds == []
    finally:
        project.close()


def test_explicit_rebuild_catches_import_pages_arriving_during_maintenance(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "import-snapshot.frisket", name="snapshot")
    try:
        writer = _import_writer(project)
        writer.append_page([{"body": "original"}], expected_cursor=0, next_cursor=1)
        checks = 0

        def append_during_scan(_cancel_event):
            nonlocal checks
            checks += 1
            if checks == 2:
                writer.append_page([{"body": NEEDLE}], expected_cursor=1, next_cursor=2)

        monkeypatch.setattr(search_mod, "_raise_if_cancelled", append_during_scan)
        assert rebuild_index(project) == 2
        assert search_project(project, NEEDLE, rerank="off")
    finally:
        project.close()


def _project_with_large_match(tmp_path: object) -> tuple[Project, int, int, int]:
    project = Project.create(tmp_path / "large.frisket", name="large")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body")
    # This is deliberately multi-megabyte: an FTS match must not depend on the
    # first 50k characters of a live cell.
    late_document = ("ordinary filler " * 180_000) + f"{NEEDLE} at the end"
    project.add_rows(
        sheet,
        [{"body": late_document}, {"body": f"{NEEDLE} short comparison"}],
        {"body": column},
    )
    late_row = next(
        row_id
        for row_id, value in project.get_values(sheet, column).items()
        if value == late_document
    )
    return project, sheet, column, late_row


def test_complete_fts_finds_late_multi_megabyte_cell_in_project_sheet_and_scope(
    tmp_path, monkeypatch
):
    project, sheet, column, late_row = _project_with_large_match(tmp_path)
    seen_rerank_inputs: list[str] = []

    def score(_query: str, texts: list[str]) -> list[float]:
        seen_rerank_inputs.extend(texts)
        return [float(index) for index in range(len(texts))]

    monkeypatch.setattr(search_mod, "local_reranker", lambda: score)

    drain_index(project)
    unreranked = search_project(project, NEEDLE, rerank="off")
    reranked = search_project(project, NEEDLE, rerank="on")
    assert late_row in {int(hit["row_id"]) for hit in unreranked}
    assert late_row in {int(hit["row_id"]) for hit in reranked}
    assert any(NEEDLE in hit["snip"] for hit in unreranked)

    assert late_row in search_sheet(project, sheet, NEEDLE)
    # This is the lexical boundary used by Ask: its row and exact-cell scopes
    # must still reach a match late in an authorized cell.
    assert [
        int(hit["row_id"])
        for hit in search_cells_scoped(project, sheet, NEEDLE, [late_row], limit=10)
    ] == [late_row]
    assert [
        int(hit["row_id"])
        for hit in search_cells_scoped(
            project, sheet, NEEDLE, [], {(late_row, column)}, limit=10
        )
    ] == [late_row]

    assert seen_rerank_inputs
    assert all(NEEDLE in text for text in seen_rerank_inputs)
    assert all(len(text.split()) <= 64 for text in seen_rerank_inputs)
    assert all("<b>" not in text for text in seen_rerank_inputs)
    project.close()


def test_same_op_truncated_sidecar_rebuilds_for_content_version(tmp_path):
    project, _sheet, _column, late_row = _project_with_large_match(tmp_path)
    db = _sidecar(project)
    try:
        db.execute("DELETE FROM cell_fts")
        db.execute(
            "INSERT INTO cell_fts(content) VALUES (?)",
            ("ordinary filler " * 100,),
        )
        db.execute(
            "INSERT INTO fts_state (key, value) VALUES ('indexed_at_op', ?)",
            (str(project.op_cursor),),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(SearchIndexNotReady):
        search_project(project, NEEDLE, rerank="off")
    drain_index(project)
    hits = search_project(project, NEEDLE, rerank="off")
    assert late_row in {int(hit["row_id"]) for hit in hits}
    db = _sidecar(project)
    try:
        assert fts_indexed_at_op(db) == project.op_cursor
        assert (
            db.execute(
                "SELECT value FROM fts_state WHERE key='index_content_version'"
            ).fetchone()["value"]
            == search_mod.FTS_INDEX_CONTENT_VERSION
        )
    finally:
        db.close()
    project.close()


def test_failed_rebuild_keeps_prior_committed_index(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "atomic.frisket", name="atomic")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "committedneedle"}], {"body": column})
    rebuild_index(project)
    original_op = project.op_cursor

    checks = 0

    def fail_during_read(_cancel_event):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("source read failed")

    monkeypatch.setattr(search_mod, "_raise_if_cancelled", fail_during_read)
    with pytest.raises(RuntimeError, match="source read failed"):
        rebuild_index(project)

    db = sqlite3.connect(project.path / "project.search.db")
    try:
        assert (
            db.execute(
                "SELECT count(*) FROM cell_fts WHERE cell_fts MATCH 'committedneedle'"
            ).fetchone()[0]
            == 1
        )
        assert db.execute(
            "SELECT value FROM fts_state WHERE key='indexed_at_op'"
        ).fetchone()[0] == str(original_op)
    finally:
        db.close()
    project.close()


def test_semantic_keeps_the_existing_prefix_input_and_cache_identity(tmp_path):
    project, _sheet, _column, _late_row = _project_with_large_match(tmp_path)
    embedded: list[str] = []

    def embed(texts: list[str]) -> list[list[float]]:
        embedded.extend(texts)
        return [[1.0, 0.0] for _text in texts]

    drain_index(project)
    hits = semantic_search(project, "query", embed=embed, embed_id="stub/fulltext-v1")
    corpus_inputs = [text for text in embedded if text != "query"]
    assert corpus_inputs
    assert all(len(text) <= SEMANTIC_CELL_PREFIX_CHARS for text in corpus_inputs)
    assert all(NEEDLE not in text for text in corpus_inputs if len(text) > 1_000)
    assert all(hit["semantic_coverage"] == SEMANTIC_COVERAGE for hit in hits)
    project.close()


def test_explicit_rebuild_catches_writes_arriving_during_maintenance(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "snapshot.frisket", name="snapshot")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "original"}], {"body": column})
    original_op = project.op_cursor
    checks = 0

    def read_then_write(_cancel_event):
        nonlocal checks
        checks += 1
        if checks > 1 and project.op_cursor == original_op:
            project.add_rows(sheet, [{"body": "concurrentneedle"}], {"body": column})

    monkeypatch.setattr(search_mod, "_raise_if_cancelled", read_then_write)
    rebuild_index(project)
    db = _sidecar(project)
    try:
        assert project.op_cursor > original_op
        assert fts_indexed_at_op(db) == project.op_cursor
        assert search_project(project, "concurrentneedle", rerank="off")
    finally:
        db.close()
        project.close()


def test_cancelled_rebuild_keeps_prior_index(tmp_path):
    project = Project.create(tmp_path / "cancel.frisket", name="cancel")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "committedneedle"}], {"body": column})
    rebuild_index(project)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(InterruptedError):
        rebuild_index(project, cancel_event=cancel)

    assert search_project(project, "committedneedle", rerank="off")
    project.close()


def test_rebuild_streams_snapshot_rows_without_materializing_columns(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "stream.frisket", name="stream")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "streamneedle"}], {"body": column})

    original_get_values = ProjectReadSnapshot.get_values

    def bounded_read(snapshot, *args, **kwargs):
        if kwargs.get("row_ids") is None:
            pytest.fail("rebuild materialized a complete column")
        return original_get_values(snapshot, *args, **kwargs)

    monkeypatch.setattr(ProjectReadSnapshot, "get_values", bounded_read)
    assert rebuild_index(project) == 1
    assert search_project(project, "streamneedle", rerank="off")
    project.close()
