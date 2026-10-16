"""Public lexical search over the unified retrieval store."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.retrieval.store import RETRIEVAL_DB_NAME
from frisket.search import (
    SearchIndexNotReady,
    drain_index,
    search_project,
    search_sheet,
)


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


def test_reader_is_side_effect_free_and_relative_project_searches(
    tmp_path, monkeypatch
):
    seeded = Project.create(tmp_path / "relative.frisket", name="relative")
    sheet = seeded.add_sheet("Documents")
    column = seeded.add_column(sheet, "body")
    [row] = seeded.add_rows(sheet, [{"body": "relativepathneedle"}], {"body": column})
    seeded.close()

    monkeypatch.chdir(tmp_path)
    project = Project(Path("relative.frisket"))
    try:
        with pytest.raises(SearchIndexNotReady):
            search_project(project, "relativepathneedle", rerank="off")
        assert not (project.path / RETRIEVAL_DB_NAME).exists()

        drain_index(project)

        [hit] = search_project(project, "relativepathneedle", rerank="off")
        assert hit["row_id"] == row
        assert hit["location"]["kind"] == "whole_source"
    finally:
        project.close()


def test_streaming_import_pages_and_removal_refresh_public_results(tmp_path):
    project = Project.create(tmp_path / "pages.frisket", name="pages")
    try:
        writer = _import_writer(project)
        writer.append_page([{"body": "firstneedle"}], expected_cursor=0, next_cursor=1)
        drain_index(project)
        assert search_project(project, "firstneedle", rerank="off")

        writer.append_page([{"body": "secondneedle"}], expected_cursor=1, next_cursor=2)
        assert search_project(project, "secondneedle", rerank="off") == []
        drain_index(project)
        assert search_project(project, "secondneedle", rerank="off")

        writer.cancel(expected_cursor=2)
        writer.remove_session(expected_cursor=2)
        drain_index(project)
        assert search_project(project, "firstneedle", rerank="off") == []
        assert search_project(project, "secondneedle", rerank="off") == []
    finally:
        project.close()


def test_complete_search_reaches_a_match_after_fifty_thousand_characters(tmp_path):
    project = Project.create(tmp_path / "large.frisket", name="large")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        late_document = ("ordinary " * 7_000) + "lateboundaryneedle"
        [row] = project.add_rows(sheet, [{"body": late_document}], {"body": column})

        drain_index(project)

        [hit] = search_project(project, "lateboundaryneedle", rerank="off")
        assert hit["row_id"] == row
        assert "lateboundaryneedle" in hit["snip"]
        assert search_sheet(project, sheet, "lateboundaryneedle") == [row]
    finally:
        project.close()


def test_malformed_unicode_is_repaired_and_searchable(tmp_path):
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


def test_public_reranking_widens_the_pool_and_auto_falls_back(tmp_path, monkeypatch):
    from frisket.ai.embeddings import backends

    project = Project.create(tmp_path / "rerank.frisket", name="rerank")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        decoy, relevant = project.add_rows(
            sheet,
            [
                {"body": "battery battery battery battery sale flyer"},
                {"body": "the car battery was dead so the engine would not start"},
            ],
            {"body": column},
        )
        drain_index(project)
        baseline = search_project(project, "battery", rerank="off")
        assert baseline[0]["row_id"] == decoy

        monkeypatch.setattr(
            backends,
            "local_reranker",
            lambda: (
                lambda _query, documents: [
                    9.0 if "engine" in document else -9.0 for document in documents
                ]
            ),
        )
        assert (
            search_project(project, "battery", limit=1, rerank="on")[0]["row_id"]
            == relevant
        )

        def broken(_query, _documents):
            raise RuntimeError("reranker failed")

        monkeypatch.setattr(backends, "local_reranker", lambda: broken)
        assert [hit["row_id"] for hit in search_project(project, "battery")] == [
            hit["row_id"] for hit in baseline
        ]
    finally:
        project.close()
