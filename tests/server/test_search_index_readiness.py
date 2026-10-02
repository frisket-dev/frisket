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


def test_manual_watch_readiness_is_http_conflict_and_schedules_index(tmp_path):
    from contextlib import closing
    from unittest.mock import Mock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from frisket.engine.store import Project
    from frisket.server.routes.watches import register_watch_routes
    from frisket.server.services.watches import WatchService

    with closing(Project.create(tmp_path / "docs.frisket")) as project:
        watch_id = project.add_watch(
            "Needle", scope="project", query={"kind": "fts", "q": "needle"}
        )
        schedule = Mock()
        project._frisket_schedule_search_index = schedule
        app = FastAPI()
        register_watch_routes(
            app, service=WatchService(SimpleNamespace(get=lambda _id: project))
        )
        response = TestClient(app).post(f"/api/projects/docs/watches/{watch_id}/run")
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "search_index_not_ready"
        schedule.assert_called_once_with()
        assert project.watch_runs_total(watch_id) == 0


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


def test_local_mcp_search_prepares_fresh_index_without_worker(tmp_path, monkeypatch):
    from frisket.server.mcp.backends import LocalBackend

    monkeypatch.setenv("FRISKET_DISABLE_RERANK", "1")
    backend = LocalBackend(tmp_path / "workspace")
    project_id = backend.ws.create("Local search")["id"]
    project = backend.ws.get(project_id)
    sheet = project.add_sheet("notes")
    column = project.add_column(sheet, "text")
    rows = project.add_rows(sheet, [{"text": "needle"}], {"text": column})
    try:
        assert not (project.path / "project.search.db").exists()
        hits = backend.search(project_id, "needle")
        assert [hit["row_id"] for hit in hits] == rows
    finally:
        project.close()


def test_semantic_cache_does_not_wait_for_index_writer(tmp_path):
    import sqlite3
    import time
    from frisket.engine.store import Project
    from frisket.search import drain_index
    from frisket.semantic import semantic_search

    project = Project.create(tmp_path / "search.frisket", name="Search")
    try:
        sheet = project.add_sheet("notes")
        column = project.add_column(sheet, "text")
        project.add_rows(sheet, [{"text": "searchable word"}], {"text": column})
        drain_index(project)
        writer = sqlite3.connect(project.path / "project.search.db")
        try:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            hits = semantic_search(
                project,
                "word",
                embed=lambda texts: [[1.0] for _ in texts],
                embed_id="test/cache-contention",
                rerank="off",
            )
            assert hits and hits[0]["semantic"] is True
            assert time.monotonic() - started < 2
            assert writer.execute("SELECT COUNT(*) FROM cell_vec").fetchone()[0] == 0
        finally:
            writer.rollback()
            writer.close()
        semantic_search(
            project,
            "word",
            embed=lambda texts: [[1.0] for _ in texts],
            embed_id="test/cache-contention",
            rerank="off",
        )
        with sqlite3.connect(project.path / "project.search.db") as cache:
            assert cache.execute("SELECT COUNT(*) FROM cell_vec").fetchone()[0] == 1
    finally:
        project.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_standalone_vectors_cache_without_keyword_index(tmp_path, asynchronous):
    import asyncio
    import sqlite3
    from frisket.engine.store import Project
    from frisket.semantic import _doc_vectors, _doc_vectors_async

    project = Project.create(tmp_path / "standalone.frisket", name="Standalone")
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0] for _ in texts]

    try:
        corpus = [{"content": "standalone document"}]
        for _ in range(2):
            if asynchronous:
                vectors = asyncio.run(
                    _doc_vectors_async(project, corpus, embed, "test/v1")
                )
            else:
                vectors = _doc_vectors(project, corpus, embed, "test/v1")
            assert vectors == [[1.0]]
        assert calls == [["standalone document"]]
        with sqlite3.connect(project.path / "project.search.db") as cache:
            assert cache.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall() == [("cell_vec",)]
    finally:
        project.close()
