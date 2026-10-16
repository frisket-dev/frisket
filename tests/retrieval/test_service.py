from __future__ import annotations

from dataclasses import replace
from threading import Event

import pytest

from frisket.engine.store import Project
from frisket.retrieval import native
from frisket.retrieval.contracts import (
    RankingPolicy,
    RetrievalScope,
    SearchRequest,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TextRange,
    WeightedSemanticQuery,
    WeightedTerm,
)
from frisket.retrieval.encoding import EmbeddingGemma2Space, EncodingError
from frisket.retrieval.query import RetrievalQueryError
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
        result = search_with_encoding(project, request(), runtime=Encoder(unavailable=True))
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
def test_unprepared_project_reports_readiness_without_starting_work(tmp_path, mode):
    project = Project.create(tmp_path / "empty.frisket", name="Empty")
    encoder = Encoder()
    try:
        result = search_with_encoding(
            project, replace(request(), mode=mode), runtime=encoder
        )
        assert not result.results
        assert result.coverage.state == "preparing"
        assert encoder.calls == 0
        assert not (project.path / "project.retrieval.db").exists()
    finally:
        project.close()
