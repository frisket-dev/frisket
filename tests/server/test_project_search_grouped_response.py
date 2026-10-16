"""Public project-search grouping, coverage, and viewer navigation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from frisket.contracts.http.project_search import ProjectSearchRequest
from frisket.retrieval.contracts import (
    BindingRevision,
    CellAnchor,
    ChannelCoverage,
    IndexedPart,
    LocatedHit,
    PageRange,
    ResultGroup,
    RetrievalContribution,
    ScopedBinding,
    SearchCoverage,
    SearchResponse,
    SourceLocator,
    SourceRevision,
    TextRange,
    TimeRange,
)
from frisket.server.services import project_search
from frisket.server.services.project_search import ProjectSearchService


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE source_artifacts(
            id INTEGER PRIMARY KEY,
            stable_id TEXT,
            source_sheet_id INTEGER,
            source_row_id INTEGER,
            source_column_id INTEGER,
            filename TEXT
        );
        CREATE TABLE source_spans(
            id INTEGER PRIMARY KEY,
            stable_id TEXT,
            artifact_id INTEGER,
            page_start INTEGER,
            page_end INTEGER,
            start_ms INTEGER,
            end_ms INTEGER,
            quote TEXT,
            snippet TEXT
        );
        CREATE TABLE evidence_links(
            id INTEGER PRIMARY KEY,
            stable_id TEXT,
            sheet_id INTEGER,
            row_id INTEGER,
            column_id INTEGER,
            status TEXT
        );
        CREATE TABLE evidence_link_spans(
            link_id INTEGER,
            span_id INTEGER,
            rank INTEGER
        );
        """
    )
    return db


@dataclass
class _Project:
    db: sqlite3.Connection

    def get_values(self, sheet_id, column_id, *, row_ids):
        return {row_id: {"filename": "clip.mp4"} for row_id in row_ids}

    def sheets(self) -> list[dict[str, object]]:
        return [{"id": 1}]

    def columns(self, sheet_id: int) -> list[dict[str, object]]:
        assert sheet_id == 1
        return [
            {"id": 3, "name": "source", "type": "video", "ai_generated": False},
            {"id": 4, "name": "notes", "type": "text", "ai_generated": True},
        ]


class _Workspace:
    def __init__(self, project: _Project) -> None:
        self._project = project
        self.embedding_runtime = object()

    def get(self, project_id: str) -> _Project:
        assert project_id == "project"
        return self._project


def _source(name: str) -> SourceRevision:
    return SourceRevision(
        source_id=f"source:{name}",
        source_kind="video" if name == "video" else "text",
        version_kind="retrieval_revision",
        version_value=1,
    )


def _hit(
    name: str,
    *,
    column_id: int | None,
    locator: SourceLocator,
    score: float,
    snippet: str | None = None,
) -> LocatedHit:
    dependencies = (
        (BindingRevision(row_id=2, column_id=column_id, retrieval_revision=1),)
        if column_id is not None
        else (
            BindingRevision(row_id=2, column_id=3, retrieval_revision=1),
            BindingRevision(row_id=2, column_id=4, retrieval_revision=1),
        )
    )
    return LocatedHit(
        part=IndexedPart(
            part_id=f"part:{name}",
            source=_source(name),
            producer_recipe_id="recipe:v1",
            part_kind=name,
            locator=locator,
        ),
        binding=ScopedBinding(
            binding_id=f"binding:{name}",
            sheet_id=1,
            row_id=2,
            column_id=column_id,
            dependencies=dependencies,
        ),
        contributions=(
            RetrievalContribution(method="semantic", channel=name, rank=1, score=score),
        ),
        rank_score=score,
        precision="exact",
        snippet=snippet or f"{name} result",
    )


def _request() -> ProjectSearchRequest:
    return ProjectSearchRequest.model_validate(
        {
            "scope": {"kind": "project"},
            "query": {"kind": "text", "text": "launch"},
            "mode": "semantic",
            "group_by": "source",
            "limit": 10,
            "ranking": {"rerank": "off"},
        }
    )


def _semantic_setup_requirement() -> SimpleNamespace:
    return SimpleNamespace(
        setup_ref="engine-setup:embeddinggemma-2.local@1",
        display_name="EmbeddingGemma 2",
        download_bytes_if_missing=1_525_784_180,
        runtime_installable=True,
        runtime_ready=False,
        license="Gemma",
        source_url="https://example.test/embeddinggemma",
    )


def test_grouped_response_preserves_supporting_hits_and_real_navigation(
    monkeypatch,
) -> None:
    db = _db()
    db.executescript(
        """
        INSERT INTO source_artifacts VALUES (1, 'artifact-1', 1, 2, 3, 'harbor.pdf');
        INSERT INTO source_spans(id,stable_id,artifact_id)
        VALUES (1, 'span-1', 1);
        INSERT INTO evidence_links VALUES (1, 'link-1', 1, 2, 3, 'active');
        INSERT INTO evidence_link_spans VALUES (1, 1, 0);
        """
    )
    project = _Project(db)
    primary = _hit(
        "video",
        column_id=3,
        locator=SourceLocator(
            kind="video",
            time=TimeRange(12.5, 15.0),
            source_span_ids=("span-1",),
        ),
        score=0.9,
    )
    supporting = _hit(
        "text",
        column_id=4,
        locator=SourceLocator(
            kind="text",
            text=TextRange("surface-1", "hash-1", 4, 10),
        ),
        score=0.7,
    )
    response = SearchResponse(
        results=(
            ResultGroup(
                group_key="source:video",
                primary=primary,
                supporting=(supporting,),
                additional_count=2,
            ),
        ),
        coverage=SearchCoverage(
            requested_mode="semantic",
            effective_modes=("semantic",),
            state="complete",
            searched_channels=("video", "text"),
            is_top_k=True,
            truncated=False,
            channels=(ChannelCoverage(channel="semantic", state="ready"),),
        ),
    )
    calls: list[object] = []

    def search(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        return response

    monkeypatch.setattr(project_search, "search_with_encoding", search)

    payload = ProjectSearchService(_Workspace(project)).search(
        "project", request=_request()
    )

    assert len(calls) == 1
    assert payload["groups"][0]["additional_count"] == 2
    assert payload["groups"][0]["primary"]["navigation"] == {
        "kind": "evidence",
        "evidence_link_id": "link-1",
        "span_id": "span-1",
    }
    assert payload["groups"][0]["supporting"][0]["navigation"] == {
        "kind": "cell",
        "sheet_id": 1,
        "row_id": 2,
        "column_id": 4,
        "text_range": {
            "text_surface_id": "surface-1",
            "content_hash": "hash-1",
            "start": 4,
            "end": 10,
        },
    }


def test_evidence_navigation_selects_span_for_located_page(monkeypatch) -> None:
    db = _db()
    db.executescript(
        """
        INSERT INTO source_artifacts VALUES (1, 'artifact-1', 1, 2, 3, 'harbor.pdf');
        INSERT INTO source_spans(
            id,stable_id,artifact_id,page_start,page_end,quote
        ) VALUES
            (1, 'page-1-span', 1, 1, 1, 'Harbor'),
            (2, 'page-2-first-span', 1, 2, 2, 'Repair'),
            (3, 'page-2-match-span', 1, 2, 2, 'bluewharf');
        INSERT INTO evidence_links VALUES (1, 'link-1', 1, 2, 3, 'active');
        INSERT INTO evidence_link_spans VALUES (1, 1, 0);
        INSERT INTO evidence_link_spans VALUES (1, 2, 1);
        INSERT INTO evidence_link_spans VALUES (1, 3, 2);
        """
    )
    project = _Project(db)
    primary = _hit(
        "video",
        column_id=3,
        locator=SourceLocator(
            kind="whole_source",
            pages=PageRange(2, 2),
            source_span_ids=(
                "page-1-span",
                "page-2-first-span",
                "page-2-match-span",
            ),
        ),
        score=0.9,
        snippet="The council approved the <b>bluewharf</b> repair.",
    )
    response = SearchResponse(
        results=(ResultGroup(group_key="source:video", primary=primary),),
        coverage=SearchCoverage(
            requested_mode="lexical",
            effective_modes=("lexical",),
            state="complete",
            searched_channels=("lexical",),
            is_top_k=True,
            truncated=False,
        ),
    )
    monkeypatch.setattr(
        project_search, "search_with_encoding", lambda *a, **k: response
    )

    payload = ProjectSearchService(_Workspace(project)).search(
        "project", request=_request()
    )

    assert payload["groups"][0]["primary"]["navigation"] == {
        "kind": "evidence",
        "evidence_link_id": "link-1",
        "span_id": "page-2-match-span",
    }


def test_unlinked_media_uses_document_time_and_unavailable_model_has_setup(
    monkeypatch,
) -> None:
    project = _Project(_db())
    primary = _hit(
        "video",
        column_id=3,
        locator=SourceLocator(
            kind="video",
            pages=PageRange(2, 3),
            time=TimeRange(7.25, 9.0),
            source_span_ids=("missing-span",),
        ),
        score=0.8,
    )
    response = SearchResponse(
        results=(ResultGroup(group_key="source:video", primary=primary),),
        coverage=SearchCoverage(
            requested_mode="semantic",
            effective_modes=(),
            state="unavailable",
            searched_channels=(),
            is_top_k=True,
            truncated=False,
            channels=(
                ChannelCoverage(
                    channel="semantic",
                    state="unavailable",
                    reason="Install the local search model.",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        project_search, "search_with_encoding", lambda *a, **k: response
    )
    monkeypatch.setattr(
        project_search,
        "local_search_setup_requirements",
        lambda: (_semantic_setup_requirement(),),
    )

    payload = ProjectSearchService(_Workspace(project)).search(
        "project", request=_request()
    )

    assert payload["groups"][0]["primary"]["navigation"] == {
        "kind": "document",
        "sheet_id": 1,
        "row_id": 2,
        "column_id": 3,
        "page": 2,
        "time_seconds": 7.25,
    }
    assert payload["coverage"]["channels"][0]["setup"] == {
        "setup_ref": "engine-setup:embeddinggemma-2.local@1",
        "scope": "workspace",
        "display_name": "EmbeddingGemma 2",
        "download_bytes": 1_525_784_180,
        "additional_runtime_download": True,
        "license": "Gemma",
        "source_url": "https://example.test/embeddinggemma",
    }


@pytest.mark.parametrize(
    ("edition", "offer_setup", "expected_scope"),
    [
        ("team", True, "organization"),
        ("team", False, None),
        ("cloud", True, None),
    ],
)
def test_semantic_setup_offer_respects_edition_and_capability(
    monkeypatch, edition, offer_setup, expected_scope
) -> None:
    project = _Project(_db())
    response = SearchResponse(
        results=(),
        coverage=SearchCoverage(
            requested_mode="semantic",
            effective_modes=(),
            state="unavailable",
            searched_channels=(),
            is_top_k=True,
            truncated=False,
            channels=(ChannelCoverage(channel="semantic", state="unavailable"),),
        ),
    )
    monkeypatch.setattr(
        project_search, "search_with_encoding", lambda *a, **k: response
    )
    workspace = _Workspace(project)
    workspace.edition = edition
    requirement_calls = 0

    def setup_requirements():
        nonlocal requirement_calls
        requirement_calls += 1
        return (_semantic_setup_requirement(),)

    monkeypatch.setattr(
        project_search, "local_search_setup_requirements", setup_requirements
    )

    payload = ProjectSearchService(workspace).search(
        "project", request=_request(), offer_setup=offer_setup
    )

    setup = payload["coverage"]["channels"][0]["setup"]
    if expected_scope is None:
        assert setup is None
        assert requirement_calls == 0
    else:
        assert setup == {
            "setup_ref": "engine-setup:embeddinggemma-2.local@1",
            "scope": expected_scope,
            "display_name": "EmbeddingGemma 2",
            "download_bytes": 1_525_784_180,
            "additional_runtime_download": True,
            "license": "Gemma",
            "source_url": "https://example.test/embeddinggemma",
        }
        assert requirement_calls == 1


def test_installed_model_is_not_offered_again_for_unavailable_search_runtime(
    monkeypatch,
) -> None:
    requirement = _semantic_setup_requirement()
    requirement.runtime_ready = True
    requirement.weights_ready = True
    requirement.download_bytes_if_missing = 0
    monkeypatch.setattr(
        project_search, "local_search_setup_requirements", lambda: (requirement,)
    )
    coverage = SearchCoverage(
        requested_mode="semantic",
        effective_modes=("lexical",),
        state="partial",
        searched_channels=("lexical",),
        is_top_k=True,
        truncated=False,
        channels=(
            ChannelCoverage(
                channel="semantic",
                state="unavailable",
                reason="Semantic search is unavailable in this SQLite runtime.",
            ),
        ),
    )
    result = project_search._coverage_payload(coverage, setup_scope="workspace")
    assert result["channels"][0]["setup"] is None
    assert result["channels"][0]["reason"] == coverage.channels[0].reason


def test_navigation_normalizes_sqlite_column_rows(monkeypatch) -> None:
    class SqliteColumnProject(_Project):
        def columns(self, sheet_id: int) -> list[sqlite3.Row]:
            assert sheet_id == 1
            return list(
                self.db.execute(
                    "SELECT 3 AS id,'source' AS name,'video' AS type,0 AS ai_generated"
                )
            )

    project = SqliteColumnProject(_db())
    primary = _hit(
        "video",
        column_id=3,
        locator=SourceLocator(kind="video"),
        score=0.8,
    )
    response = SearchResponse(
        results=(ResultGroup(group_key="source:video", primary=primary),),
        coverage=SearchCoverage(
            requested_mode="semantic",
            effective_modes=("semantic",),
            state="complete",
            searched_channels=("video",),
            is_top_k=True,
            truncated=False,
        ),
    )
    monkeypatch.setattr(
        project_search, "search_with_encoding", lambda *a, **k: response
    )

    payload = ProjectSearchService(_Workspace(project)).search(
        "project", request=_request()
    )

    assert payload["groups"][0]["primary"]["navigation"] == {
        "kind": "document",
        "sheet_id": 1,
        "row_id": 2,
        "column_id": 3,
        "page": None,
        "time_seconds": None,
    }


def test_cell_similarity_is_passed_to_the_shared_service_with_named_index(
    monkeypatch,
) -> None:
    project = _Project(_db())
    response = SearchResponse(
        results=(),
        coverage=SearchCoverage(
            requested_mode="semantic",
            effective_modes=("semantic",),
            state="complete",
            searched_channels=("semantic",),
            is_top_k=True,
            truncated=False,
        ),
    )
    calls: list[object] = []

    def search(project_arg, request, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((project_arg, request, kwargs))
        return response

    monkeypatch.setattr(project_search, "search_with_encoding", search)
    request = ProjectSearchRequest.model_validate(
        {
            "scope": {"kind": "project"},
            "query": {
                "kind": "similar",
                "anchor": {
                    "kind": "cell",
                    "sheet_id": 1,
                    "row_id": 2,
                    "column_id": 3,
                    "location": {
                        "kind": "video",
                        "precision": "exact",
                        "text": None,
                        "pages": None,
                        "time": {"start_seconds": 12.5, "end_seconds": 12.5},
                        "source_span_ids": [],
                    },
                },
            },
            "mode": "semantic",
            "group_by": "row",
            "limit": 5,
            "named_index_id": "index-1",
            "ranking": {"rerank": "off"},
        }
    )

    ProjectSearchService(_Workspace(project)).search("project", request=request)

    _, retrieval_request, _ = calls[0]
    assert retrieval_request.named_index_id == "index-1"
    assert retrieval_request.group_by == "row"
    assert retrieval_request.query == CellAnchor(
        1,
        2,
        3,
        locator=SourceLocator(kind="video", time=TimeRange(12.5, 12.5)),
    )
