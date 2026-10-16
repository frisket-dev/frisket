"""Wire models for the browser project-search read."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from frisket.contracts.http.models import WireModel


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
    start: int
    end: int


class ProjectSearchPageRange(WireModel):
    start: int
    end: int


class ProjectSearchTimeRange(WireModel):
    start_seconds: float
    end_seconds: float


class ProjectSearchLocation(WireModel):
    kind: str
    precision: Literal["exact", "source_unit", "approximate"]
    text: ProjectSearchTextRange | None
    pages: ProjectSearchPageRange | None
    time: ProjectSearchTimeRange | None
    source_span_ids: list[str]


class ProjectSearchContribution(WireModel):
    method: str
    channel: str
    rank: int
    representation_id: str | None
    space_id: str | None
    score: float | None


class ProjectSearchChannelCoverage(WireModel):
    channel: str
    state: Literal["ready", "pending", "failed", "unavailable"]
    recipe_id: str | None
    eligible_count: int | None
    prepared_count: int | None
    failed_count: int | None
    reason: str | None


class ProjectSearchCoverage(WireModel):
    requested_mode: Literal["lexical", "semantic", "hybrid"]
    effective_modes: list[Literal["lexical", "semantic"]]
    state: Literal["complete", "partial", "preparing", "unavailable", "failed"]
    searched_channels: list[str]
    is_top_k: bool
    truncated: bool
    channels: list[ProjectSearchChannelCoverage]


class ProjectSearchHit(WireModel):
    """One ranked, authorization-checked and source-located result."""

    # Keep producer extension fields accepted during the generated-client
    # migration; the service itself now emits the explicit fields below.
    model_config = ConfigDict(extra="allow", strict=True)

    sheet_id: int
    row_id: int
    column_id: int | None
    column_name: str | None
    ai_generated: bool
    snip: str
    score: float
    semantic: bool
    source: ProjectSearchSource
    part: ProjectSearchPart
    location: ProjectSearchLocation
    contributions: list[ProjectSearchContribution]
    additional_binding_count: int
    rerank_score: float = None  # type: ignore[assignment]


class ProjectSearchPage(WireModel):
    """Ranked located hits with maintained preparation coverage."""

    hits: list[ProjectSearchHit]
    indexing: bool
    coverage: ProjectSearchCoverage
