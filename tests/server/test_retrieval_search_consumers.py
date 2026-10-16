from __future__ import annotations

from frisket.contracts.http.project_search import ProjectSearchPage
from frisket.retrieval.contracts import (
    ChannelCoverage,
    IndexedPart,
    LocatedHit,
    PageRange,
    RetrievalContribution,
    ScopedBinding,
    SearchCoverage,
    SourceLocator,
    SourceRevision,
    TextRange,
    TimeRange,
)
from frisket.server.services.project_qa_output_scope import OutputReadGrant
from frisket.server.services.project_qa_tools import _retrieval_scope
from frisket.server.services.project_search import (
    _coverage_payload,
    _search_hit_payload,
)


def _located_hit() -> LocatedHit:
    return LocatedHit(
        part=IndexedPart(
            part_id="part:clip-7",
            source=SourceRevision(
                source_id="source:video-3",
                source_kind="video",
                version_kind="cell_revision",
                version_value=41,
                content_hash="sha256:" + "a" * 64,
            ),
            producer_recipe_id="recipe:fixed-5s",
            part_kind="native_video_clip",
            locator=SourceLocator(
                kind="video_clip",
                text=TextRange(
                    text_surface_id="surface:transcript-3",
                    content_hash="sha256:" + "b" * 64,
                    start=120,
                    end=180,
                ),
                pages=PageRange(2, 3),
                time=TimeRange(4.25, 9.5),
                source_span_ids=("span:video-7", "span:transcript-9"),
            ),
        ),
        binding=ScopedBinding(
            binding_id="binding:9",
            sheet_id=5,
            row_id=7,
            column_id=11,
        ),
        contributions=(
            RetrievalContribution(
                method="semantic",
                channel="video",
                rank=1,
                representation_id="representation:7",
                space_id="space:embeddinggemma-2",
                score=0.82,
            ),
        ),
        rank_score=0.91,
        precision="exact",
        snippet="A rider crosses the finish line.",
        additional_binding_count=2,
    )


def test_browser_search_serializes_actual_source_and_independent_locations() -> None:
    hit = _search_hit_payload(
        _located_hit(), column={"name": "Recording", "ai_generated": False}
    )
    coverage = _coverage_payload(
        SearchCoverage(
            requested_mode="semantic",
            effective_modes=("semantic",),
            state="partial",
            searched_channels=("video",),
            is_top_k=True,
            truncated=False,
            channels=(
                ChannelCoverage(
                    channel="video",
                    state="ready",
                    recipe_id="recipe:fixed-5s",
                    eligible_count=3,
                    prepared_count=2,
                    failed_count=0,
                ),
            ),
        )
    )

    page = ProjectSearchPage.model_validate(
        {"hits": [hit], "indexing": True, "coverage": coverage}
    ).model_dump()

    assert page["hits"][0]["source"] == {
        "source_id": "source:video-3",
        "source_kind": "video",
        "version_kind": "cell_revision",
        "version_value": 41,
        "content_hash": "sha256:" + "a" * 64,
    }
    assert page["hits"][0]["location"] == {
        "kind": "video_clip",
        "precision": "exact",
        "text": {
            "text_surface_id": "surface:transcript-3",
            "content_hash": "sha256:" + "b" * 64,
            "start": 120,
            "end": 180,
        },
        "pages": {"start": 2, "end": 3},
        "time": {"start_seconds": 4.25, "end_seconds": 9.5},
        "source_span_ids": ["span:video-7", "span:transcript-9"],
    }
    assert page["coverage"]["channels"][0]["prepared_count"] == 2


def test_ask_scope_keeps_whole_rows_and_exact_cells_as_a_union() -> None:
    scope = _retrieval_scope(
        5,
        {7, 8},
        {(8, 11), (9, 12)},
        (
            OutputReadGrant(
                receipt_id="receipt:1",
                sheet_id=5,
                row_ids=frozenset({10, 11}),
                column_ids=frozenset({13}),
            ),
        ),
    )

    assert scope.sheets is not None
    assert [(item.row_ids, item.column_ids) for item in scope.sheets] == [
        ((7, 8), None),
        ((9,), (12,)),
        ((10, 11), (13,)),
    ]
