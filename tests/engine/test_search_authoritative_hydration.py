"""Public search hydrates bounded source text without a persistent content copy."""

from __future__ import annotations

import sqlite3

import frisket.search as search_mod
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


def test_temp_fts_indexes_only_the_bounded_result_pool(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "bounded.frisket", name="bounded")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet,
            [{"body": f"commonneedle document {index}"} for index in range(40)],
            {"body": column},
        )
        drain_index(project)

        observed: list[int] = []
        native_snippets = search_mod.native_snippets

        def record_pool(db, query, candidates, **kwargs):
            observed.append(len(candidates))
            return native_snippets(db, query, candidates, **kwargs)

        monkeypatch.setattr(search_mod, "native_snippets", record_pool)
        page = search_project_page(project, "commonneedle", limit=3, rerank="off")

        assert len(page["hits"]) == 3
        assert observed == [3]
    finally:
        project.close()
