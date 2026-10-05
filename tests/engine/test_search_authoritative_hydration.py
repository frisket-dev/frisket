"""Public search hydrates bounded source text without a persistent content copy."""

from __future__ import annotations

import sqlite3

import frisket.search as search_mod
import pytest
from frisket.engine.store import Project
from frisket.search import (
    drain_index,
    search_cells_scoped,
    search_project,
    search_project_page,
)


def test_contentless_search_uses_native_snippets_and_removes_old_tokens(tmp_path):
    project = Project.create(tmp_path / "contentless.frisket", name="contentless")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        text = ("filler " * 500) + "raw <b>tag</b> actualneedle at the end"
        [row] = project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)

        path = project.path / "project.search.db"
        raw = sqlite3.connect(path)
        try:
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='cell_fts'"
            ).fetchone()[0]
            assert "contentless_delete=1" in schema.replace(" ", "")
            assert raw.execute("SELECT content FROM cell_fts").fetchone()[0] is None
            assert (
                raw.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='search_content'"
                ).fetchone()
                is None
            )
        finally:
            raw.close()

        [hit] = search_project(project, "actualneedle", rerank="off")
        assert hit["row_id"] == row
        assert "<b>actualneedle</b>" in hit["snip"]

        [scoped] = search_cells_scoped(project, sheet, "actualneedle", [row], limit=10)
        assert scoped["fts_anchor"] == "actualneedle"
        assert "raw <b>tag</b>" in scoped["snip"]

        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "replacementneedle"}]
        )
        drain_index(project)
        assert search_project(project, "actualneedle", rerank="off") == []
        assert (
            search_project(project, "replacementneedle", rerank="off")[0]["row_id"]
            == row
        )
    finally:
        project.close()


def test_malformed_unicode_cell_is_repaired_and_fully_searchable(tmp_path):
    project = Project.create(tmp_path / "unicode.frisket", name="unicode")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        [row] = project.add_rows(
            sheet,
            [{"body": "beforeneedle \ud800 afterneedle 🚀"}],
            {"body": column},
        )

        assert project.get_values(sheet, column)[row] == (
            "beforeneedle \ufffd afterneedle 🚀"
        )
        drain_index(project)

        assert search_project(project, "beforeneedle", rerank="off")[0]["row_id"] == row
        [hit] = search_project(project, "afterneedle", rerank="off")
        assert hit["row_id"] == row
        assert "\ufffd" in hit["snip"]
        assert "🚀" in hit["snip"]
    finally:
        project.close()


@pytest.mark.parametrize(("rerank", "expected_pool"), [("off", 3), ("auto", 50)])
def test_temp_fts_indexes_only_the_bounded_result_pool(
    tmp_path, monkeypatch, rerank, expected_pool
):
    project = Project.create(tmp_path / "bounded.frisket", name="bounded")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet,
            [{"body": f"commonneedle document {index}"} for index in range(60)],
            {"body": column},
        )
        drain_index(project)

        ranked_limits: list[int] = []
        hydrated: list[int] = []
        snippet_pools: list[int] = []
        ranked_candidates = search_mod._ranked_candidates
        hydrate_candidates = search_mod.hydrate_candidates
        native_snippets = search_mod.native_snippets

        def record_ranked(db, query, limit):
            ranked_limits.append(limit)
            return ranked_candidates(db, query, limit)

        def record_hydrated(snapshot, candidates):
            candidates = list(candidates)
            hydrated.append(len(candidates))
            return hydrate_candidates(snapshot, candidates)

        def record_pool(db, query, candidates, **kwargs):
            snippet_pools.append(len(candidates))
            return native_snippets(db, query, candidates, **kwargs)

        monkeypatch.setattr(search_mod, "_ranked_candidates", record_ranked)
        monkeypatch.setattr(search_mod, "hydrate_candidates", record_hydrated)
        monkeypatch.setattr(search_mod, "native_snippets", record_pool)
        monkeypatch.setattr(
            search_mod,
            "local_reranker",
            lambda: lambda _query, documents: list(range(len(documents))),
        )
        page = search_project_page(project, "commonneedle", limit=3, rerank=rerank)

        assert len(page["hits"]) == 3
        assert ranked_limits == [expected_pool]
        assert hydrated == [expected_pool]
        assert snippet_pools == [expected_pool]
    finally:
        project.close()
