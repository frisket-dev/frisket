"""Browser semantic search uses only the workspace-owned local runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

import frisket.server.services.project_search as search_service
from frisket.contracts.http.project_search import (
    ProjectSearchProjectScope,
    ProjectSearchRanking,
    ProjectSearchRequest,
    ProjectSearchTextQuery,
)
from frisket.retrieval.contracts import (
    ChannelCoverage,
    IndexedPart,
    LocatedHit,
    ResultGroup,
    RetrievalContribution,
    ScopedBinding,
    SearchCoverage,
    SearchResponse,
    SourceLocator,
    SourceRevision,
    TextQuery,
)
from frisket.server.services.project_search import ProjectSearchService
from frisket.server.workspace import Workspace


def _project_with_text(tmp_path: Path):
    workspace = Workspace(tmp_path / "ws")
    info = workspace.create("search egress")
    project = workspace.get(info["id"])
    sheet_id = project.add_sheet("notes")
    columns = {"body": project.add_column(sheet_id, "body", type="text")}
    [row_id] = project.add_rows(
        sheet_id,
        [{"body": "the mayor approved the stadium bond"}],
        columns,
    )
    return workspace, info["id"], sheet_id, row_id, columns["body"]


def _lexical_fallback(sheet_id: int, row_id: int, column_id: int) -> SearchResponse:
    hit = LocatedHit(
        part=IndexedPart(
            part_id="part:text",
            source=SourceRevision(
                source_id="source:text",
                source_kind="text",
                version_kind="retrieval_revision",
                version_value=1,
            ),
            producer_recipe_id="recipe:text-v1",
            part_kind="text",
            locator=SourceLocator(kind="text"),
        ),
        binding=ScopedBinding(
            binding_id="binding:text",
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
        ),
        contributions=(
            RetrievalContribution(method="lexical", channel="text", rank=1, score=1.0),
        ),
        rank_score=1.0,
        precision="source_unit",
        snippet="the mayor approved the stadium bond",
    )
    return SearchResponse(
        results=(ResultGroup(group_key="source:text", primary=hit),),
        coverage=SearchCoverage(
            requested_mode="semantic",
            effective_modes=("lexical",),
            state="partial",
            searched_channels=("text",),
            is_top_k=True,
            truncated=False,
            channels=(
                ChannelCoverage(
                    channel="semantic",
                    state="unavailable",
                    reason="Install the local search model to use semantic search.",
                ),
            ),
        ),
    )


def test_semantic_search_uses_workspace_runtime_and_reports_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, sheet_id, row_id, column_id = _project_with_text(tmp_path)
    runtime = object()
    calls = []

    def search_with_encoding(project, request, **kwargs):
        calls.append((project, request, kwargs))
        return _lexical_fallback(sheet_id, row_id, column_id)

    monkeypatch.setattr(search_service, "search_with_encoding", search_with_encoding)
    monkeypatch.setattr(
        Workspace, "embedding_runtime", property(lambda _self: runtime), raising=False
    )
    page = ProjectSearchService(workspace).search(
        project_id,
        request=ProjectSearchRequest(
            scope=ProjectSearchProjectScope(kind="project"),
            query=ProjectSearchTextQuery(kind="text", text="stadium bond"),
            limit=10,
            mode="semantic",
            ranking=ProjectSearchRanking(rerank="auto"),
        ),
    )

    assert len(calls) == 1
    _project, request, kwargs = calls[0]
    assert isinstance(request.query, TextQuery)
    assert request.mode == "semantic"
    assert kwargs == {"runtime": runtime}
    assert page["coverage"]["state"] == "partial"
    assert page["coverage"]["effective_modes"] == ["lexical"]
    assert {
        contribution["method"]
        for contribution in page["groups"][0]["primary"]["contributions"]
    } == {"lexical"}


def test_search_reports_unprepared_media_in_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "ws")
    project_id = workspace.create("media gaps")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("media")
    image_column = project.add_column(sheet_id, "image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"image": image_column},
    )
    monkeypatch.setattr(
        search_service,
        "search_with_encoding",
        lambda *args, **kwargs: SearchResponse(
            results=(),
            coverage=SearchCoverage(
                requested_mode="lexical",
                effective_modes=("lexical",),
                state="complete",
                searched_channels=("text",),
                is_top_k=True,
                truncated=False,
                channels=(ChannelCoverage(channel="text", state="ready"),),
            ),
        ),
    )

    page = ProjectSearchService(workspace).search(
        project_id,
        request=ProjectSearchRequest(
            scope=ProjectSearchProjectScope(kind="project"),
            query=ProjectSearchTextQuery(kind="text", text="invoice"),
            limit=10,
            mode="lexical",
            ranking=ProjectSearchRanking(rerank="auto"),
        ),
    )

    assert page["coverage"]["state"] == "partial"
    gap = next(
        channel
        for channel in page["coverage"]["channels"]
        if channel["channel"] == "search_preparation"
    )
    assert gap == {
        "channel": "search_preparation",
        "state": "unavailable",
        "recipe_id": None,
        "eligible_count": 1,
        "prepared_count": 0,
        "failed_count": 0,
        "reason": "media_needs_preparation",
        "setup": None,
    }
