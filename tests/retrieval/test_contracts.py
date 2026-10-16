from __future__ import annotations

import math

import pytest

from frisket.retrieval.contracts import (
    ChannelCoverage,
    IndexedPart,
    LocatedHit,
    PageRange,
    RankingPolicy,
    ResultGroup,
    RetrievalContribution,
    RetrievalScope,
    ScopedBinding,
    SearchCoverage,
    SearchRequest,
    SearchResponse,
    SemanticExclusion,
    SheetScope,
    SourceAnchor,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TextRange,
    TimeRange,
    WeightedSemanticQuery,
    WeightedTerm,
)


def source() -> SourceRevision:
    return SourceRevision(
        source_id="surface:report-body",
        source_kind="prepared_text",
        version_kind="prepared_page_version_id",
        version_value=42,
        content_hash="sha256:current-source",
    )


def locator() -> SourceLocator:
    return SourceLocator(
        kind="transcript_passage",
        text=TextRange(
            text_surface_id="surface:transcript",
            content_hash="sha256:transcript",
            start=3,
            end=19,
        ),
        pages=PageRange(start=2, end=2),
        time=TimeRange(start_seconds=1.25, end_seconds=4.5),
        source_span_id="source_span:segment-2",
    )


def binding() -> ScopedBinding:
    return ScopedBinding(
        binding_id="binding:7:11:13",
        sheet_id=7,
        row_id=11,
        column_id=13,
    )


def part() -> IndexedPart:
    return IndexedPart(
        part_id="part:transcript:2",
        source=source(),
        producer_recipe_id="recipe:transcript-passages:v1",
        part_kind="transcript_passage",
        locator=locator(),
    )


def test_locator_reuses_content_addressed_text_and_original_media_time() -> None:
    located = locator()

    assert located.text is not None
    assert located.text.offset_unit == "unicode_codepoint"
    assert located.text.content_hash == "sha256:transcript"
    assert located.pages == PageRange(start=2, end=2)
    assert located.time == TimeRange(start_seconds=1.25, end_seconds=4.5)
    assert located.source_span_id == "source_span:segment-2"


def test_source_revision_separates_version_from_content_identity() -> None:
    revision = source()

    assert revision.version_kind == "prepared_page_version_id"
    assert revision.version_value == 42
    assert revision.content_hash == "sha256:current-source"

    with pytest.raises(ValueError, match="version_value"):
        SourceRevision(
            source_id="cell:7:11:13",
            source_kind="cell",
            version_kind="retrieval_revision",
            version_value=0,
        )


def test_frame_time_may_be_an_instant_but_must_be_finite() -> None:
    assert TimeRange(start_seconds=12.5, end_seconds=12.5).duration_seconds == 0.0

    with pytest.raises(ValueError, match="finite"):
        TimeRange(start_seconds=0.0, end_seconds=math.inf)
    with pytest.raises(ValueError, match="before"):
        TimeRange(start_seconds=4.0, end_seconds=3.0)


def test_text_offsets_require_an_ordered_content_addressed_surface() -> None:
    with pytest.raises(ValueError, match="content_hash"):
        TextRange(
            text_surface_id="surface:1",
            content_hash=" ",
            start=0,
            end=1,
        )
    with pytest.raises(ValueError, match="start < end"):
        TextRange(
            text_surface_id="surface:1",
            content_hash="sha256:text",
            start=2,
            end=2,
        )


def test_scope_distinguishes_all_from_an_explicit_empty_selection() -> None:
    all_project = RetrievalScope()
    no_project_sources = RetrievalScope(sheets=())
    no_rows = RetrievalScope(sheets=(SheetScope(sheet_id=7, row_ids=()),))
    no_columns = RetrievalScope(sheets=(SheetScope(sheet_id=7, column_ids=()),))

    assert all_project.sheets is None
    assert not all_project.is_empty
    assert no_project_sources.is_empty
    assert no_rows.is_empty
    assert no_columns.is_empty


def test_scope_keeps_compiler_inputs_without_accepting_sql() -> None:
    scope = SheetScope(
        sheet_id=7,
        parent_row_id=3,
        row_ids=(11, 12),
        column_ids=(13,),
        filter_json='{"Status":{"eq":"published"}}',
    )

    assert scope.row_ids == (11, 12)
    assert scope.filter_json == '{"Status":{"eq":"published"}}'

    with pytest.raises(ValueError, match="row_ids"):
        SheetScope(sheet_id=7, row_ids=(11, 11))


def test_search_request_keeps_ranking_policy_small_and_mode_compatible() -> None:
    request = SearchRequest(
        scope=RetrievalScope(sheets=(SheetScope(sheet_id=7),)),
        query=TextQuery("climate NEAR/4 budget"),
        mode="hybrid",
        group_by="source",
        ranking=RankingPolicy(k_rrf=60, rerank="off"),
    )

    assert request.schema_version == "frisket.retrieval.query.v1"
    assert request.ranking == RankingPolicy(k_rrf=60, rerank="off")

    weighted = WeightedSemanticQuery(
        terms=(WeightedTerm("public transit", 1.0), WeightedTerm("cars", -0.25)),
        exclusions=(SemanticExclusion("sports", threshold=0.7),),
        minimum_similarity=0.2,
    )
    with pytest.raises(ValueError, match="requires a text query"):
        SearchRequest(
            scope=RetrievalScope(),
            query=weighted,
            mode="hybrid",
        )


@pytest.mark.parametrize("bad_weight", [math.inf, -math.inf, math.nan])
def test_semantic_terms_reject_nonfinite_weights(bad_weight: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        WeightedTerm("term", bad_weight)


def test_source_anchor_retains_the_exact_selected_location() -> None:
    anchor = SourceAnchor(
        source=source(),
        locator=SourceLocator(
            kind="video_frame",
            time=TimeRange(start_seconds=12.5, end_seconds=12.5),
        ),
        binding=binding(),
        exclude_exact=True,
    )
    request = SearchRequest(
        scope=RetrievalScope(sheets=(SheetScope(sheet_id=7, row_ids=(11,)),)),
        query=anchor,
        mode="semantic",
        group_by="part",
        space_id="space:multimodal-v1",
    )

    assert isinstance(request.query, SourceAnchor)
    assert request.query.locator.time == TimeRange(12.5, 12.5)


def test_result_keeps_selected_binding_location_and_method_scores() -> None:
    hit = LocatedHit(
        part=part(),
        binding=binding(),
        snippet="…capital budget…",
        precision="exact",
        contributions=(
            RetrievalContribution(
                method="semantic",
                channel="transcript_text",
                representation_id="representation:44",
                space_id="space:text-v1",
                rank=1,
                score=0.83,
            ),
            RetrievalContribution(
                method="lexical",
                channel="transcript_text",
                rank=3,
            ),
        ),
        rank_score=0.032,
        additional_binding_count=2,
    )
    response = SearchResponse(
        results=(
            ResultGroup(
                group_key="source:surface:report-body",
                primary=hit,
                additional_count=4,
            ),
        ),
        coverage=SearchCoverage(
            requested_mode="hybrid",
            effective_modes=("lexical", "semantic"),
            state="complete",
            searched_channels=("transcript_text",),
            is_top_k=True,
            truncated=False,
            channels=(
                ChannelCoverage(
                    channel="transcript_text",
                    state="ready",
                    recipe_id="recipe:transcript-passages:v1",
                ),
            ),
        ),
    )

    primary = response.results[0].primary
    assert primary.part.locator.time == TimeRange(1.25, 4.5)
    assert primary.binding == binding()
    assert primary.contributions[0].score == 0.83
    assert response.coverage.channels[0].eligible_count is None


def test_coverage_accepts_only_cheap_optional_maintenance_counts() -> None:
    coverage = ChannelCoverage(
        channel="video_frames",
        state="pending",
        recipe_id="recipe:frames:periodic-v1",
        eligible_count=10,
        prepared_count=4,
        failed_count=1,
        reason="six sources still need preparation",
    )

    assert coverage.prepared_count == 4
    with pytest.raises(ValueError, match="nonnegative"):
        ChannelCoverage(channel="video_frames", state="pending", prepared_count=-1)
