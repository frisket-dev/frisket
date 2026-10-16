"""Public project-search grouping, coverage, and viewer navigation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

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
            source_column_id INTEGER
        );
        CREATE TABLE source_spans(
            id INTEGER PRIMARY KEY,
            stable_id TEXT,
            artifact_id INTEGER
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
        snippet=f"{name} result",
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


def test_grouped_response_preserves_supporting_hits_and_real_navigation(
    monkeypatch,
) -> None:
    db = _db()
    db.executescript(
        """
        INSERT INTO source_artifacts VALUES (1, 'artifact-1', 1, 2, 3);
        INSERT INTO source_spans VALUES (1, 'span-1', 1);
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
