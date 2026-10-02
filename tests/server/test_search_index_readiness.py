"""Consumers distinguish incomplete indexing from a complete empty result."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from frisket.search import SearchIndexNotReady
from frisket.server.services import project_search
from frisket.server.services.project_qa_tools import ProjectQATools
from frisket.preview import query as query_preview


def test_keyword_page_exposes_partial_hits(monkeypatch):
    monkeypatch.setattr(
        project_search,
        "search_project_page",
        lambda *a, **kw: {
            "hits": [{"row_id": 7}],
            "complete": False,
        },
    )
    service = project_search.ProjectSearchService(
        SimpleNamespace(get=lambda _id: object())
    )
    assert service.search("p", q="word", limit=5, mode="keyword", rerank="off") == {
        "hits": [{"row_id": 7}],
        "indexing": True,
    }


class SnapshotProject:
    def __init__(self):
        self.opened = []
        self.closed = []

    @contextmanager
    def read_snapshot(self):
        snapshot = object()
        self.opened.append(snapshot)
        try:
            yield snapshot
        finally:
            self.closed.append(snapshot)


def not_ready(*args, **kwargs):
    raise SearchIndexNotReady()


def test_semantic_indexing_does_not_load_embedder_or_fall_back(monkeypatch):
    project = SnapshotProject()
    monkeypatch.setattr(project_search, "fresh_sidecar", not_ready)

    def unexpected(*args, **kwargs):
        pytest.fail("an incomplete semantic request must not embed or fall back")

    monkeypatch.setattr(project_search, "resolve_embedder", unexpected)
    monkeypatch.setattr(project_search, "semantic_search", unexpected)
    service = project_search.ProjectSearchService(
        SimpleNamespace(get=lambda _id: project)
    )
    assert service.search("p", q="word", limit=5, mode="semantic", rerank="off") == {
        "hits": [],
        "indexing": True,
    }
    assert project.closed == project.opened


@pytest.mark.parametrize("mode", ["keyword", "semantic"])
def test_agent_search_never_claims_complete_coverage_while_indexing(monkeypatch, mode):
    tools = object.__new__(ProjectQATools)
    tools.project = object()
    monkeypatch.setattr(tools, "_ready_search_hits", not_ready)
    hits, coverage = tools._search_hits("word", 1, 5, mode, None, set())
    assert hits == []
    assert coverage["complete"] is False
    assert coverage["reason"] == "indexing"


@pytest.mark.parametrize("remains_unready", [False, True])
def test_preview_retries_whole_evaluation_with_new_snapshot(
    monkeypatch, remains_unready
):
    project = SnapshotProject()
    evaluated = []
    marker = object()

    def evaluate(snapshot, *args, **kwargs):
        evaluated.append(snapshot)
        if len(evaluated) == 1 or remains_unready:
            raise SearchIndexNotReady()
        return marker

    monkeypatch.setattr(query_preview, "_resolve_query_preview_in_snapshot", evaluate)
    if remains_unready:
        with pytest.raises(query_preview.QueryPreviewError) as error:
            query_preview.resolve_query_preview_resolution(project, {})
        assert error.value.code == "search_index_not_ready"
    else:
        assert query_preview.resolve_query_preview_resolution(project, {}) is marker
    assert len(evaluated) == 2
    assert evaluated[0] is not evaluated[1]
    assert project.closed == evaluated


def test_hybrid_checks_index_before_embedding(monkeypatch):
    from frisket.ai.embeddings import hybrid

    monkeypatch.setattr(hybrid, "search_sheet", not_ready)

    def unexpected(*args, **kwargs):
        pytest.fail("indexing must not trigger a paid query embedding")

    monkeypatch.setattr(hybrid, "resolve_embedding_similarity", unexpected)
    with pytest.raises(SearchIndexNotReady):
        hybrid.resolve_embedding_hybrid(
            SimpleNamespace(row_count=lambda _sheet: 1),
            {"embedding_index_id": "index", "sheet_id": 1, "text": "word"},
        )


def test_watch_refuses_partial_search(monkeypatch):
    import frisket.search
    from frisket.features.watchlists.service import (
        WatchBindingError,
        _evaluate_fts_watch,
    )

    monkeypatch.setattr(frisket.search, "search_project", not_ready)
    with pytest.raises(WatchBindingError) as error:
        _evaluate_fts_watch(object(), {"query": {"q": "word"}})
    assert error.value.code == "search_index_not_ready"


@pytest.mark.parametrize("indexing", [False, True])
def test_hosted_mcp_preserves_readiness(indexing):
    import asyncio
    import httpx
    from frisket.server.mcp.backends import HostedBackend

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"hits": [{"row_id": 1}], "indexing": indexing},
                )
            ),
        ) as client:
            backend = HostedBackend(client=client)
            if indexing:
                with pytest.raises(SearchIndexNotReady):
                    await backend.search("p", "word")
            else:
                assert await backend.search("p", "word") == [{"row_id": 1}]

    asyncio.run(run())
