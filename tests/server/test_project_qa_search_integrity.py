from __future__ import annotations

from typing import Literal

import frisket.retrieval.service as retrieval_service
from frisket.contracts.http.project_qa import AskCitation
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.retrieval.contracts import (
    BindingRevision,
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
    TextQuery,
    TimeRange,
)
from frisket.server.services.project_qa_citations import resolve_citation
from frisket.server.services.project_qa_tools import ProjectQATools


def _response(
    sheet_id: int,
    row_id: int,
    column_id: int,
    revision: int,
    *,
    locator: SourceLocator | None = None,
    dependencies: tuple[BindingRevision, ...] | None = None,
    mode: Literal["lexical", "semantic"] = "lexical",
) -> SearchResponse:
    hit = LocatedHit(
        part=IndexedPart(
            part_id="part:oldneedle",
            source=SourceRevision(
                source_id=f"cell:{row_id}:{column_id}",
                source_kind="text",
                version_kind="retrieval_revision",
                version_value=revision,
            ),
            producer_recipe_id="recipe:text-v1",
            part_kind="text",
            locator=locator or SourceLocator(kind="text"),
        ),
        binding=ScopedBinding(
            binding_id="binding:oldneedle",
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            dependencies=dependencies
            or (
                BindingRevision(
                    row_id=row_id,
                    column_id=column_id,
                    retrieval_revision=revision,
                ),
            ),
        ),
        contributions=(
            RetrievalContribution(method=mode, channel="text", rank=1, score=1.0),
        ),
        rank_score=1.0,
        precision="source_unit",
        snippet="oldneedle",
    )
    return SearchResponse(
        results=(ResultGroup(group_key="source:text", primary=hit),),
        coverage=SearchCoverage(
            requested_mode=mode,
            effective_modes=(mode,),
            state="complete",
            searched_channels=("text",),
            is_top_k=True,
            truncated=False,
        ),
    )


def _ask_project(tmp_path):
    project = Project.create(tmp_path / "race.frisket", name="Race")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "Text")
    [row] = project.add_rows(sheet, [{"Text": "oldneedle"}], {"Text": column})
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(thread["id"], request_id="one", question="Find it")
    revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row, column),
        ).fetchone()[0]
    )
    return project, store, turn, sheet, row, column, revision


def test_keyword_search_does_not_pair_stale_excerpt_with_new_value_ref(
    tmp_path, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(tmp_path)

    def search_then_edit(_project, request, **_kwargs):
        assert isinstance(request.query, TextQuery)
        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "replacement"}]
        )
        return _response(sheet, row, column, revision)

    monkeypatch.setattr(retrieval_service, "search_with_encoding", search_then_edit)
    try:
        result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
        assert result["hits"] == []
        assert result["partial"] is True
        assert result["coverage"]["reason"] == "source_changed"
        assert (
            project.db.execute(
                "SELECT count(*) FROM project_qa_citations WHERE turn_id=?",
                (turn["id"],),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_keyword_search_mints_citation_after_dependency_recheck(tmp_path, monkeypatch):
    project, store, turn, sheet, row, column, revision = _ask_project(tmp_path)
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(sheet, row, column, revision),
    )
    try:
        result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
        [hit] = result["hits"]
        citation = store.get_citation(hit["citation_id"])
        assert citation["source_kind"] == "cell"
        assert citation["locator"]["sheet_id"] == sheet
        assert citation["locator"]["row_id"] == row
        assert citation["locator"]["column_id"] == column
        assert citation["locator"]["retrieval_source"] == {
            "source_id": f"cell:{row}:{column}",
            "source_kind": "text",
            "version_kind": "retrieval_revision",
            "version_value": revision,
        }

        def whole_source_read(*_args, **_kwargs):
            raise AssertionError("citation resolution loaded the whole source")

        monkeypatch.setattr(project, "get_values_with_refs", whole_source_read)
        assert (
            resolve_citation(project, turn["thread_id"], hit["citation_id"])["status"]
            == "current"
        )
        assert result["partial"] is False
    finally:
        project.close()


def test_semantic_search_uses_shared_runtime_and_mints_a_citation(
    tmp_path, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(tmp_path)
    runtime = object()
    observed = []

    def semantic_search(_project, request, **kwargs):
        observed.append((request.mode, kwargs["runtime"]))
        return _response(sheet, row, column, revision, mode="semantic")

    monkeypatch.setattr(retrieval_service, "search_with_encoding", semantic_search)
    try:
        result = ProjectQATools(
            project,
            turn,
            store,
            embedding_runtime=runtime,
            setup_scope="workspace",
        ).search_cells("oldneedle", sheet, mode="semantic")

        assert observed == [("semantic", runtime)]
        [hit] = result["hits"]
        assert store.get_citation(hit["citation_id"])["source_kind"] == "cell"
        assert result["coverage"]["requested_mode"] == "semantic"
        assert result["partial"] is False
    finally:
        project.close()


def test_located_media_hit_mints_navigable_evidence_citation(tmp_path, monkeypatch):
    from frisket.engine.store.evidence import (
        record_evidence_link,
        record_source_artifact,
        record_source_span,
    )

    project, store, turn, sheet, row, column, _revision = _ask_project(tmp_path)
    _, refs = project.get_values_with_refs(sheet, column, [row])
    artifact = record_source_artifact(
        project,
        artifact_kind="video",
        media_type="video/mp4",
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=column,
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="video_clip",
        page_start=2,
        page_end=2,
        start_ms=4250,
        end_ms=9500,
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=refs[row],
        spans=[{"span_id": span["id"]}],
        sheet_id=sheet,
        row_id=row,
        column_id=column,
    )
    revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row, column),
        ).fetchone()[0]
    )
    located = SourceLocator(
        kind="video_clip",
        pages=PageRange(2, 2),
        time=TimeRange(4.25, 9.5),
        source_span_ids=(span["stable_id"],),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(
            sheet, row, column, revision, locator=located
        ),
    )
    try:
        result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
        citation = store.get_citation(result["hits"][0]["citation_id"])
        assert citation["source_kind"] == "evidence"
        assert citation["locator"]["evidence_link_id"] == link["stable_id"]
        assert citation["locator"]["artifact_id"] == artifact["stable_id"]
        assert citation["locator"]["span_id"] == span["stable_id"]
        assert citation["locator"]["location"]["time"] == {
            "start_seconds": 4.25,
            "end_seconds": 9.5,
        }
    finally:
        project.close()


def test_page_time_hit_without_evidence_keeps_document_navigation(
    tmp_path, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(tmp_path)
    located = SourceLocator(
        kind="video_clip",
        pages=PageRange(3, 3),
        time=TimeRange(12.5, 18.0),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(
            sheet, row, column, revision, locator=located
        ),
    )
    try:
        [hit] = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)[
            "hits"
        ]
        citation = store.get_citation(hit["citation_id"])
        assert citation["source_kind"] == "cell"

        current = resolve_citation(project, turn["thread_id"], hit["citation_id"])
        assert current["status"] == "current"
        typed = AskCitation.model_validate(current)
        assert typed.target is not None
        assert current["target"]["navigation"] == {
            "kind": "document",
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
            "page": 3,
            "time_seconds": 12.5,
        }

        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "replacement"}]
        )
        changed = resolve_citation(project, turn["thread_id"], hit["citation_id"])
        assert changed["status"] == "changed"
        assert "navigation" not in changed["target"]
    finally:
        project.close()


def test_citation_becomes_stale_when_any_binding_dependency_changes(
    tmp_path, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(tmp_path)
    context_column = project.add_column(sheet, "Context")
    project.apply_edits(
        [{"row_id": row, "column_id": context_column, "value": "supporting"}]
    )
    context_revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row, context_column),
        ).fetchone()[0]
    )
    dependencies = (
        BindingRevision(row, column, revision),
        BindingRevision(row, context_column, context_revision),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(
            sheet,
            row,
            column,
            revision,
            locator=SourceLocator(kind="document", pages=PageRange(1, 1)),
            dependencies=dependencies,
        ),
    )
    try:
        [hit] = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)[
            "hits"
        ]
        assert (
            resolve_citation(project, turn["thread_id"], hit["citation_id"])["status"]
            == "current"
        )

        project.apply_edits(
            [{"row_id": row, "column_id": context_column, "value": "changed"}]
        )
        changed = resolve_citation(project, turn["thread_id"], hit["citation_id"])
        assert changed["status"] == "changed"
        assert "navigation" not in changed["target"]
    finally:
        project.close()
