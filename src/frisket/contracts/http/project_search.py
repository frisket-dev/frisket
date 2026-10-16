"""Strict wire models for project-wide located retrieval."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from frisket.contracts.http.models import WireModel


class ProjectSearchProjectScope(WireModel):
    kind: Literal["project"]


class ProjectSearchSheetScope(WireModel):
    sheet_id: int = Field(gt=0)
    parent_row_id: int | None = Field(default=None, gt=0)
    row_ids: list[int] | None = None
    column_ids: list[int] | None = None
    filter_json: str | None = None


class ProjectSearchSheetsScope(WireModel):
    kind: Literal["sheets"]
    sheets: list[ProjectSearchSheetScope] = Field(min_length=1)


ProjectSearchScope = Annotated[
    ProjectSearchProjectScope | ProjectSearchSheetsScope,
    Field(discriminator="kind"),
]


class ProjectSearchSource(WireModel):
    source_id: str
    source_kind: str
    version_kind: str
    version_value: int | str
    content_hash: str | None


class ProjectSearchPart(WireModel):
    part_id: str
    part_kind: str
    producer_recipe_id: str


class ProjectSearchTextRange(WireModel):
    text_surface_id: str
    content_hash: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> ProjectSearchTextRange:
        if self.end <= self.start:
            raise ValueError("text range end must be after its start")
        return self


class ProjectSearchPageRange(WireModel):
    start: int = Field(gt=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> ProjectSearchPageRange:
        if self.end < self.start:
            raise ValueError("page range end must not precede its start")
        return self


class ProjectSearchTimeRange(WireModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> ProjectSearchTimeRange:
        if self.end_seconds < self.start_seconds:
            raise ValueError("time range end must not precede its start")
        return self


class ProjectSearchLocation(WireModel):
    kind: str
    precision: Literal["exact", "source_unit", "approximate"]
    text: ProjectSearchTextRange | None
    pages: ProjectSearchPageRange | None
    time: ProjectSearchTimeRange | None
    source_span_ids: list[str]


class ProjectSearchBindingRevision(WireModel):
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)
    retrieval_revision: int = Field(ge=0)


class ProjectSearchBinding(WireModel):
    binding_id: str
    sheet_id: int = Field(gt=0)
    row_id: int = Field(gt=0)
    column_id: int | None = Field(default=None, gt=0)
    dependencies: list[ProjectSearchBindingRevision]


class ProjectSearchLocatedAnchor(WireModel):
    kind: Literal["located"]
    source: ProjectSearchSource
    location: ProjectSearchLocation
    binding: ProjectSearchBinding | None = None
    exclude_exact: bool = True


class ProjectSearchCellAnchor(WireModel):
    kind: Literal["cell"]
    sheet_id: int = Field(gt=0)
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)
    exclude_exact: bool = True
    location: ProjectSearchLocation | None = None


ProjectSearchAnchor = Annotated[
    ProjectSearchLocatedAnchor | ProjectSearchCellAnchor,
    Field(discriminator="kind"),
]


class ProjectSearchTextQuery(WireModel):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=500)
    lexical_syntax: Literal["fts", "plain"] = "fts"


class ProjectSearchSimilarQuery(WireModel):
    kind: Literal["similar"]
    anchor: ProjectSearchAnchor


ProjectSearchQuery = Annotated[
    ProjectSearchTextQuery | ProjectSearchSimilarQuery,
    Field(discriminator="kind"),
]


class ProjectSearchRanking(WireModel):
    rerank: Literal["off", "auto", "on"] = "auto"


class ProjectSearchRequest(WireModel):
    scope: ProjectSearchScope
    query: ProjectSearchQuery
    mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    group_by: Literal["part", "source", "row"] = "source"
    limit: int = Field(default=50, ge=1, le=500)
    named_index_id: str | None = None
    ranking: ProjectSearchRanking = Field(default_factory=ProjectSearchRanking)

    @model_validator(mode="after")
    def _query_matches_mode(self) -> ProjectSearchRequest:
        if (
            isinstance(self.query, ProjectSearchSimilarQuery)
            and self.mode != "semantic"
        ):
            raise ValueError("similar searches require semantic mode")
        return self


class ProjectSearchContribution(WireModel):
    method: str
    channel: str
    rank: int = Field(gt=0)
    representation_id: str | None
    space_id: str | None
    score: float | None


class ProjectSearchSetup(WireModel):
    setup_ref: str
    scope: Literal["workspace", "organization"]
    display_name: str
    download_bytes: int | None = Field(default=None, ge=0)
    additional_runtime_download: bool
    license: str | None = None
    source_url: str | None = None


class ProjectSearchChannelCoverage(WireModel):
    channel: str
    state: Literal["ready", "pending", "failed", "unavailable"]
    recipe_id: str | None
    eligible_count: int | None = Field(default=None, ge=0)
    prepared_count: int | None = Field(default=None, ge=0)
    failed_count: int | None = Field(default=None, ge=0)
    reason: str | None
    gap_sheet_ids: list[int] | None = None
    setup: ProjectSearchSetup | None = None


class ProjectSearchCoverage(WireModel):
    requested_mode: Literal["lexical", "semantic", "hybrid"]
    effective_modes: list[Literal["lexical", "semantic"]]
    state: Literal["complete", "partial", "preparing", "unavailable", "failed"]
    searched_channels: list[str]
    is_top_k: bool
    truncated: bool
    channels: list[ProjectSearchChannelCoverage]


class ProjectSearchEvidenceNavigation(WireModel):
    kind: Literal["evidence"]
    evidence_link_id: str
    span_id: str


class ProjectSearchDocumentNavigation(WireModel):
    kind: Literal["document"]
    sheet_id: int = Field(gt=0)
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)
    page: int | None = Field(default=None, gt=0)
    time_seconds: float | None = Field(default=None, ge=0)


class ProjectSearchCellNavigation(WireModel):
    kind: Literal["cell"]
    sheet_id: int = Field(gt=0)
    row_id: int = Field(gt=0)
    column_id: int | None = Field(default=None, gt=0)
    text_range: ProjectSearchTextRange | None = None


ProjectSearchNavigation = Annotated[
    ProjectSearchEvidenceNavigation
    | ProjectSearchDocumentNavigation
    | ProjectSearchCellNavigation,
    Field(discriminator="kind"),
]


class ProjectSearchHit(WireModel):
    """One ranked, authorization-checked and source-located result."""

    column_name: str | None
    ai_generated: bool
    snippet: str
    score: float
    rerank_score: float | None = None
    source: ProjectSearchSource
    part: ProjectSearchPart
    binding: ProjectSearchBinding
    location: ProjectSearchLocation
    navigation: ProjectSearchNavigation
    contributions: list[ProjectSearchContribution]
    additional_binding_count: int = Field(ge=0)


class ProjectSearchGroup(WireModel):
    group_key: str
    primary: ProjectSearchHit
    supporting: list[ProjectSearchHit]
    additional_count: int = Field(ge=0)


class ProjectSearchResponse(WireModel):
    """Grouped located results with maintained preparation coverage."""

    groups: list[ProjectSearchGroup]
    coverage: ProjectSearchCoverage
    response_anchor: ProjectSearchLocatedAnchor | None = None


__all__ = [
    "ProjectSearchRequest",
    "ProjectSearchResponse",
]
