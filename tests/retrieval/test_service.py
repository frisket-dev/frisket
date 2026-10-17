from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from threading import Event

import pytest

from frisket.engine.store import Project
from frisket.retrieval import native
from frisket.retrieval.contracts import (
    RankingPolicy,
    RetrievalScope,
    SearchRequest,
    SourceAnchor,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TextRange,
    WeightedSemanticQuery,
    WeightedTerm,
)
from frisket.retrieval.encoding import EmbeddingGemma2Space, EncodingError
from frisket.retrieval.query import RetrievalQueryError, read_pdf_indexing_progress
from frisket.retrieval.service import search_with_encoding
from frisket.retrieval.sources import input_hash
from frisket.retrieval.store import (
    BindingDependency,
    CachedRepresentation,
    LexicalInput,
    PartRepresentation,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
    StoredPart,
)
from tests.retrieval.test_native import built_native_root as built_native_root


def _pdf_cursor(source, **changes):
    from frisket.retrieval.preparation import PDF_PAGE_RECIPE_ID

    return json.dumps(
        {
            "kind": "pdf_pages",
            "source_id": source.source_id,
            "recipe_id": PDF_PAGE_RECIPE_ID,
            "next_page": 3,
            "page_count": 10,
            **changes,
        }
    )


@pytest.fixture
def pdf_indexing_sources(tmp_path):
    from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE, PDF_PAGE_RECIPE_ID
    from frisket.retrieval.sources import blob_source_revision
    from frisket.search import drain_index

    project = Project.create(tmp_path / "pdf-progress.frisket")
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "file", type="file")
    sources = []
    for number in range(10):
        digest = project.add_blob(
            f"%PDF-fixture-{number}".encode(),
            filename=f"{number}-" + "document" * 30 + ".pdf",
            mime="application/pdf",
        )
        row = project.add_rows(sheet, [{"file": {"blob": digest}}], {"file": column})[0]
        sources.append((blob_source_revision(blob_hash=digest), row))
    drain_index(project)
    with RetrievalStore(project) as store:
        for source, _row in sources:
            store.begin_preparation(
                source,
                recipe_id=PDF_PAGE_RECIPE_ID,
                channel="pdf_page",
                space_id=DEFAULT_TEXT_SPACE.identity,
            )
            assert store.advance_preparation(
                source,
                recipe_id=PDF_PAGE_RECIPE_ID,
                channel="pdf_page",
                space_id=DEFAULT_TEXT_SPACE.identity,
                cursor_json=_pdf_cursor(source),
            )
    yield project, sheet, column, sources
    project.close()


def test_pdf_indexing_progress_is_bounded_current_metadata_only(
    pdf_indexing_sources, monkeypatch
):
    from frisket.retrieval import query as query_module
    from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE, PDF_PAGE_RECIPE_ID

    project, _sheet, _column, sources = pdf_indexing_sources
    first, second = sorted(
        (source for source, _row in sources), key=lambda s: s.source_id
    )[:2]
    with RetrievalStore(project) as store:
        assert store.advance_preparation(
            second,
            recipe_id=PDF_PAGE_RECIPE_ID,
            channel="pdf_page",
            space_id=DEFAULT_TEXT_SPACE.identity,
            cursor_json=_pdf_cursor(second, next_page=1897, page_count=24909),
        )

    original_open = query_module.PinnedRetrievalReader.open

    def open_metadata_only(self):
        result = original_open(self)

        def authorize(action, table, column, *_):
            forbidden = table in {"parts", "representations", "cells", "results"}
            forbidden = forbidden or (table == "blobs" and column == "metadata")
            forbidden = forbidden or (table == "current_cells" and column == "value")
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and forbidden
                else sqlite3.SQLITE_OK
            )

        self.db.set_authorizer(authorize)
        return result

    monkeypatch.setattr(query_module.PinnedRetrievalReader, "open", open_metadata_only)

    def no_vec1(*_args):
        raise AssertionError("metadata progress must not load Vec1")

    monkeypatch.setattr(query_module, "load_vec1", no_vec1)
    files, truncated = read_pdf_indexing_progress(
        project,
        space_id=DEFAULT_TEXT_SPACE.identity,
        recipe_id=PDF_PAGE_RECIPE_ID,
        limit=2,
    )
    assert truncated
    assert [(file.source_id, file.completed, file.total) for file in files] == [
        (first.source_id, 2, 10),
        (second.source_id, 1896, 24909),
    ]
    assert all(len(file.filename) == 200 for file in files)


def test_pdf_indexing_progress_omits_invalid_stale_hidden_and_terminal_work(
    pdf_indexing_sources,
):
    from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE, PDF_PAGE_RECIPE_ID

    project, sheet, column, sources = pdf_indexing_sources
    with RetrievalStore(project) as store:
        for (source, _row), changes in zip(
            sources,
            (
                {"kind": "other"},
                {"source_id": "wrong-source"},
                {"recipe_id": "wrong-recipe"},
                {"next_page": True},
                {"next_page": 11},
            ),
        ):
            store.db.execute(
                "UPDATE preparation SET cursor_json=? WHERE source_id=? AND channel='pdf_page'",
                (_pdf_cursor(source, **changes), source.source_id),
            )
        store.db.execute(
            "UPDATE preparation SET status='ready' WHERE source_id=?",
            (sources[5][0].source_id,),
        )
        store.db.execute(
            "UPDATE preparation SET status='pending' WHERE source_id=?",
            (sources[6][0].source_id,),
        )
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (sources[7][1],))
    project.db.execute(
        "UPDATE current_cells SET retrieval_revision=retrieval_revision+1 WHERE row_id=? AND column_id=?",
        (sources[8][1], column),
    )
    project.db.commit()
    files, truncated = read_pdf_indexing_progress(
        project,
        space_id=DEFAULT_TEXT_SPACE.identity,
        recipe_id=PDF_PAGE_RECIPE_ID,
        limit=2,
    )
    assert not truncated
    assert [file.source_id for file in files] == [sources[9][0].source_id]
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet,))
    project.db.commit()
    assert read_pdf_indexing_progress(
        project,
        space_id=DEFAULT_TEXT_SPACE.identity,
        recipe_id=PDF_PAGE_RECIPE_ID,
        limit=2,
    ) == ((), False)


class Encoder:
    def __init__(self, *, unavailable=False):
        self.calls = 0
        self.unavailable = unavailable

    def encode(self, inputs, **_kwargs):
        self.calls += 1
        if self.unavailable:
            raise EncodingError("model_unavailable", "Install the model")
        return [(1.0, *([0.0] * 767)) for _ in inputs]


@pytest.fixture
def indexed_project(tmp_path, monkeypatch, built_native_root):
    monkeypatch.setattr(native, "_NATIVE_ROOT", built_native_root)
    project = Project.create(tmp_path / "search.frisket", name="Search")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "body")
    text = "The council approved funding."
    row = project.add_rows(sheet, [{"body": text}], {"body": column})[0]
    revision = project.db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row, column),
    ).fetchone()[0]
    source = SourceRevision(
        f"cell:{row}:{column}", "text", "retrieval_revision", revision, input_hash(text)
    )
    location = SourceLocator(
        "text", text=TextRange(source.source_id, input_hash(text), 0, len(text))
    )
    space = EmbeddingGemma2Space()
    with RetrievalStore(project) as store:
        store.begin_preparation(source, recipe_id="text-v1", channel="text")
        assert store.publish_source(
            SourcePublication(
                source,
                "text-v1",
                "text",
                parts=(StoredPart("part:1", "text_passage", location),),
                bindings=(
                    StoredBinding(
                        "binding:1",
                        sheet,
                        row,
                        column,
                        (BindingDependency(row, column, revision),),
                    ),
                ),
                representations=(
                    CachedRepresentation(
                        "vector:1",
                        space.identity,
                        input_hash(text),
                        native.pack_vector([1.0, *([0.0] * 767)]),
                        768,
                    ),
                ),
                attachments=(PartRepresentation("part:1", "vector:1", "text"),),
                lexical=LexicalInput(text, location),
            )
        )
    yield project, row
    project.close()


def request():
    return SearchRequest(
        RetrievalScope(),
        TextQuery("council"),
        mode="semantic",
        ranking=RankingPolicy(rerank="off"),
    )


def test_semantic_query_uses_shared_cache_and_returns_current_location(indexed_project):
    project, row = indexed_project
    encoder = Encoder()
    first = search_with_encoding(project, request(), runtime=encoder)
    second = search_with_encoding(project, request(), runtime=encoder)
    assert encoder.calls == 1
    assert first == second
    hit = first.results[0].primary
    assert hit.binding.row_id == row
    assert hit.binding.dependencies
    assert hit.part.locator.text.start == 0
    assert first.coverage.effective_modes == ("semantic",)


def test_missing_model_gives_explicit_lexical_fallback(indexed_project):
    project, row = indexed_project
    result = search_with_encoding(project, request(), runtime=Encoder(unavailable=True))
    assert result.results[0].primary.binding.row_id == row
    assert result.coverage.requested_mode == "semantic"
    assert result.coverage.effective_modes == ("lexical",)
    assert result.coverage.state == "partial"
    assert result.coverage.channels[-1].state == "unavailable"


def test_weighted_query_is_not_silently_changed_to_keywords(indexed_project):
    project, _ = indexed_project
    result = search_with_encoding(
        project,
        replace(request(), query=WeightedSemanticQuery((WeightedTerm("council"),))),
        runtime=Encoder(unavailable=True),
    )
    assert not result.results
    assert result.coverage.state == "unavailable"
    assert result.coverage.effective_modes == ()


def test_missing_model_keeps_empty_lexical_search_available(
    tmp_path, monkeypatch, built_native_root
):
    monkeypatch.setattr(native, "_NATIVE_ROOT", built_native_root)
    project = Project.create(tmp_path / "empty-index.frisket", name="Empty")
    try:
        with RetrievalStore(project):
            pass
        result = search_with_encoding(
            project, request(), runtime=Encoder(unavailable=True)
        )
        assert not result.results
        assert result.coverage.state == "partial"
        assert result.coverage.effective_modes == ("lexical",)
        assert result.coverage.searched_channels == ("lexical",)
    finally:
        project.close()


def test_cancelled_or_wrong_space_does_not_call_model(indexed_project):
    project, _ = indexed_project
    encoder = Encoder()
    stopped = Event()
    stopped.set()
    with pytest.raises(InterruptedError):
        search_with_encoding(project, request(), runtime=encoder, cancel_event=stopped)
    with pytest.raises(RetrievalQueryError, match="different embedding model"):
        search_with_encoding(
            project, replace(request(), space_id="paid-space"), runtime=encoder
        )
    assert encoder.calls == 0


@pytest.mark.parametrize("mode", ["lexical", "semantic", "hybrid"])
def test_empty_project_reports_complete_without_starting_work(tmp_path, mode):
    project = Project.create(tmp_path / "empty.frisket", name="Empty")
    encoder = Encoder()
    try:
        result = search_with_encoding(
            project, replace(request(), mode=mode), runtime=encoder
        )
        assert not result.results
        assert result.coverage.state == "complete"
        assert encoder.calls == 0
        assert not (project.path / "project.retrieval.db").exists()
    finally:
        project.close()


def test_find_similar_echoes_exact_resolved_source(indexed_project):
    project, _ = indexed_project
    encoder = Encoder()
    hit = search_with_encoding(project, request(), runtime=encoder).results[0].primary
    anchor = SourceAnchor(hit.part.source, hit.part.locator, hit.binding)
    response = search_with_encoding(
        project, replace(request(), query=anchor), runtime=encoder
    )
    assert response.response_anchor == anchor
    assert encoder.calls == 1  # Reuse the exact indexed document vector.
    assert not response.results


def test_find_similar_encodes_exact_unprepared_text_location(indexed_project):
    project, _ = indexed_project
    encoder = Encoder()
    hit = search_with_encoding(project, request(), runtime=encoder).results[0].primary
    selected_text = replace(hit.part.locator.text, start=4, end=11)
    anchor = SourceAnchor(
        hit.part.source, replace(hit.part.locator, text=selected_text), hit.binding
    )
    response = search_with_encoding(
        project, replace(request(), query=anchor), runtime=encoder
    )
    assert response.response_anchor == anchor
    assert encoder.calls == 2
    assert response.results[0].primary.part.part_id == hit.part.part_id


def test_unprepared_anchor_refuses_stale_binding_before_encoding(indexed_project):
    project, _ = indexed_project
    encoder = Encoder()
    hit = search_with_encoding(project, request(), runtime=encoder).results[0].primary
    anchor = SourceAnchor(
        hit.part.source,
        replace(hit.part.locator, text=replace(hit.part.locator.text, start=4, end=11)),
        replace(
            hit.binding,
            dependencies=(
                replace(hit.binding.dependencies[0], retrieval_revision=999),
            ),
        ),
    )
    with pytest.raises(RetrievalQueryError, match="changed"):
        search_with_encoding(project, replace(request(), query=anchor), runtime=encoder)
    assert encoder.calls == 1


@pytest.mark.parametrize("mode", ["lexical", "semantic", "hybrid"])
def test_keyword_search_survives_missing_vec1(
    indexed_project, monkeypatch, tmp_path, mode
):
    project, row = indexed_project
    monkeypatch.setattr(native, "_NATIVE_ROOT", tmp_path / "no-native-library")
    response = search_with_encoding(
        project, replace(request(), mode=mode), runtime=Encoder()
    )
    assert response.results[0].primary.binding.row_id == row
    assert response.coverage.effective_modes == ("lexical",)
    if mode != "lexical":
        assert response.coverage.channels[-1].state == "unavailable"
