"""Complete-cell FTS storage and bounded search consumers."""

from __future__ import annotations

import sqlite3

import pytest

import frisket.search as search_mod
from frisket.engine.store import Project
from frisket.search import (
    _sidecar,
    fts_indexed_at_op,
    rebuild_index,
    search_cells_scoped,
    search_project,
    search_sheet,
)
from frisket.semantic import (
    SEMANTIC_CELL_PREFIX_CHARS,
    SEMANTIC_COVERAGE,
    semantic_search,
)


NEEDLE = "nebulaquartz"


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
    project, sheet, column, late_row = _project_with_large_match(tmp_path)
    db = _sidecar(project)
    try:
        db.execute("DELETE FROM cell_fts")
        db.execute(
            "INSERT INTO cell_fts "
            "(content, sheet_id, row_id, column_id, column_name) VALUES (?,?,?,?,?)",
            ("ordinary filler " * 100, sheet, late_row, column, "body"),
        )
        db.execute(
            "INSERT INTO fts_state (key, value) VALUES ('indexed_at_op', ?)",
            (str(project.op_cursor),),
        )
        db.commit()
    finally:
        db.close()

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

    def fail_values(*_args, **_kwargs):
        raise RuntimeError("source read failed")

    monkeypatch.setattr(project, "get_values", fail_values)
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

    hits = semantic_search(project, "query", embed=embed, embed_id="stub/fulltext-v1")
    corpus_inputs = [text for text in embedded if text != "query"]
    assert corpus_inputs
    assert all(len(text) <= SEMANTIC_CELL_PREFIX_CHARS for text in corpus_inputs)
    assert all(NEEDLE not in text for text in corpus_inputs if len(text) > 1_000)
    assert all(hit["semantic_coverage"] == SEMANTIC_COVERAGE for hit in hits)
    project.close()
