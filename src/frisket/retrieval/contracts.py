"""Small shared runtime contract for located retrieval.

The values here contain no SQL, database handles, providers, or viewer state.
Services compile ``SheetScope`` through the existing Frisket scope compiler and
return one authorization-checked ``ScopedBinding`` for each located hit.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


QUERY_SCHEMA_VERSION = "frisket.retrieval.query.v1"
MAX_SEARCH_LIMIT = 100
MAX_SEMANTIC_TERMS = 16
DEFAULT_RRF_K = 60

SearchMode: TypeAlias = Literal["lexical", "semantic", "hybrid"]
GroupBy: TypeAlias = Literal["part", "source", "row"]
RerankMode: TypeAlias = Literal["off", "auto", "on"]
LocationPrecision: TypeAlias = Literal["exact", "source_unit", "approximate"]
CoverageState: TypeAlias = Literal[
    "complete", "partial", "preparing", "unavailable", "failed"
]
ChannelState: TypeAlias = Literal["ready", "pending", "failed", "unavailable"]


def _nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _finite(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite number")


def _selection(values: tuple[int, ...] | None, name: str) -> None:
    if values is None:
        return
    if not isinstance(values, tuple) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        raise ValueError(f"{name} must contain positive integer IDs")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """Stable source identity plus its authoritative SQL-join version."""

    source_id: str
    source_kind: str
    version_kind: str
    version_value: int | str
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _nonblank(self.source_id, "source_id")
        _nonblank(self.source_kind, "source_kind")
        _nonblank(self.version_kind, "version_kind")
        if isinstance(self.version_value, int) and not isinstance(
            self.version_value, bool
        ):
            _positive(self.version_value, "version_value")
        elif isinstance(self.version_value, str):
            _nonblank(self.version_value, "version_value")
        else:
            raise ValueError("version_value must be a positive integer or string")
        if self.content_hash is not None:
            _nonblank(self.content_hash, "content_hash")


@dataclass(frozen=True, slots=True)
class TextRange:
    """Half-open Unicode-code-point offsets in a content-addressed surface."""

    text_surface_id: str
    content_hash: str
    start: int
    end: int
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"

    def __post_init__(self) -> None:
        _nonblank(self.text_surface_id, "text_surface_id")
        _nonblank(self.content_hash, "content_hash")
        if (
            isinstance(self.start, bool)
            or isinstance(self.end, bool)
            or not isinstance(self.start, int)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end <= self.start
        ):
            raise ValueError("text range must satisfy 0 <= start < end")
        if self.offset_unit != "unicode_codepoint":
            raise ValueError("retrieval text offsets must use unicode_codepoint")


@dataclass(frozen=True, slots=True)
class PageRange:
    """Inclusive one-based pages in the original document."""

    start: int
    end: int

    def __post_init__(self) -> None:
        _positive(self.start, "page start")
        _positive(self.end, "page end")
        if self.end < self.start:
            raise ValueError("page start must not be after page end")


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Finite seconds on the original timeline; equality is an instant."""

    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        _finite(self.start_seconds, "time range start")
        _finite(self.end_seconds, "time range end")
        if self.start_seconds < 0:
            raise ValueError("time range start must be nonnegative")
        if self.end_seconds < self.start_seconds:
            raise ValueError("time range end must not be before its start")

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True, slots=True)
class SourceLocator:
    """Independent source coordinates and ordered supporting source spans."""

    kind: str
    text: TextRange | None = None
    pages: PageRange | None = None
    time: TimeRange | None = None
    source_span_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopedBinding:
    """One visible binding selected by the request's compiled scope."""

    binding_id: str
    sheet_id: int
    row_id: int
    column_id: int | None = None


@dataclass(frozen=True, slots=True)
class IndexedPart:
    part_id: str
    source: SourceRevision
    producer_recipe_id: str
    part_kind: str
    locator: SourceLocator


@dataclass(frozen=True, slots=True)
class SheetScope:
    """Inputs for existing scope compilation; empty tuples mean no results."""

    sheet_id: int
    parent_row_id: int | None = None
    row_ids: tuple[int, ...] | None = None
    column_ids: tuple[int, ...] | None = None
    filter_json: str | None = None

    def __post_init__(self) -> None:
        _positive(self.sheet_id, "sheet_id")
        if self.parent_row_id is not None:
            _positive(self.parent_row_id, "parent_row_id")
        _selection(self.row_ids, "row_ids")
        _selection(self.column_ids, "column_ids")
        if self.filter_json is not None:
            _nonblank(self.filter_json, "filter_json")

    @property
    def is_empty(self) -> bool:
        return self.row_ids == () or self.column_ids == ()


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """All project sources (None) or an explicit union of sheet scopes."""

    sheets: tuple[SheetScope, ...] | None = None

    def __post_init__(self) -> None:
        if self.sheets is not None:
            sheet_ids = [scope.sheet_id for scope in self.sheets]
            if len(set(sheet_ids)) != len(sheet_ids):
                raise ValueError("sheets must not contain duplicate sheet_id values")

    @property
    def is_empty(self) -> bool:
        return self.sheets == () or (
            self.sheets is not None and all(scope.is_empty for scope in self.sheets)
        )


@dataclass(frozen=True, slots=True)
class TextQuery:
    text: str
    lexical_syntax: Literal["fts", "plain"] = "fts"

    def __post_init__(self) -> None:
        _nonblank(self.text, "query text")


@dataclass(frozen=True, slots=True)
class WeightedTerm:
    text: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        _nonblank(self.text, "semantic term text")
        _finite(self.weight, "semantic term weight")


@dataclass(frozen=True, slots=True)
class SemanticExclusion:
    text: str
    threshold: float = 0.5

    def __post_init__(self) -> None:
        _nonblank(self.text, "semantic exclusion text")
        _finite(self.threshold, "semantic exclusion threshold")
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("semantic exclusion threshold must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class WeightedSemanticQuery:
    terms: tuple[WeightedTerm, ...]
    exclusions: tuple[SemanticExclusion, ...] = ()
    minimum_similarity: float | None = None

    def __post_init__(self) -> None:
        if not self.terms or len(self.terms) > MAX_SEMANTIC_TERMS:
            raise ValueError(f"semantic query requires 1-{MAX_SEMANTIC_TERMS} terms")
        if len(self.exclusions) > MAX_SEMANTIC_TERMS:
            raise ValueError(
                f"semantic query allows at most {MAX_SEMANTIC_TERMS} exclusions"
            )
        if self.minimum_similarity is not None:
            _finite(self.minimum_similarity, "minimum_similarity")
            if not -1.0 <= self.minimum_similarity <= 1.0:
                raise ValueError("minimum_similarity must be in [-1, 1]")


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    source: SourceRevision
    locator: SourceLocator
    binding: ScopedBinding | None = None
    exclude_exact: bool = True


SearchQuery: TypeAlias = TextQuery | WeightedSemanticQuery | SourceAnchor


@dataclass(frozen=True, slots=True)
class RankingPolicy:
    k_rrf: int = DEFAULT_RRF_K
    rerank: RerankMode = "auto"

    def __post_init__(self) -> None:
        _positive(self.k_rrf, "k_rrf")
        if self.rerank not in {"off", "auto", "on"}:
            raise ValueError("rerank must be off, auto, or on")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    scope: RetrievalScope
    query: SearchQuery
    schema_version: str = QUERY_SCHEMA_VERSION
    mode: SearchMode = "lexical"
    group_by: GroupBy = "source"
    limit: int = 50
    named_index_id: str | None = None
    space_id: str | None = None
    ranking: RankingPolicy = field(default_factory=RankingPolicy)

    def __post_init__(self) -> None:
        if self.schema_version != QUERY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported retrieval query schema: {self.schema_version}"
            )
        if self.mode not in {"lexical", "semantic", "hybrid"}:
            raise ValueError("mode must be lexical, semantic, or hybrid")
        if self.group_by not in {"part", "source", "row"}:
            raise ValueError("group_by must be part, source, or row")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_SEARCH_LIMIT
        ):
            raise ValueError(f"limit must be in [1, {MAX_SEARCH_LIMIT}]")
        if self.mode in {"lexical", "hybrid"} and not isinstance(self.query, TextQuery):
            raise ValueError(f"{self.mode} mode requires a text query")


@dataclass(frozen=True, slots=True)
class RetrievalContribution:
    """One method's rank and method-specific raw score."""

    method: str
    channel: str
    rank: int
    representation_id: str | None = None
    space_id: str | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        _positive(self.rank, "contribution rank")
        if self.score is not None:
            _finite(self.score, "contribution score")


@dataclass(frozen=True, slots=True)
class LocatedHit:
    part: IndexedPart
    binding: ScopedBinding
    contributions: tuple[RetrievalContribution, ...]
    rank_score: float
    precision: LocationPrecision
    snippet: str | None = None
    preview_ref: str | None = None
    additional_binding_count: int = 0

    def __post_init__(self) -> None:
        _finite(self.rank_score, "rank_score")
        if self.additional_binding_count < 0:
            raise ValueError("additional_binding_count must be nonnegative")


@dataclass(frozen=True, slots=True)
class ResultGroup:
    group_key: str
    primary: LocatedHit
    supporting: tuple[LocatedHit, ...] = ()
    additional_count: int = 0


@dataclass(frozen=True, slots=True)
class ChannelCoverage:
    """Maintained readiness; counts are optional and never demand a recount."""

    channel: str
    state: ChannelState
    recipe_id: str | None = None
    eligible_count: int | None = None
    prepared_count: int | None = None
    failed_count: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("eligible_count", "prepared_count", "failed_count"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be nonnegative")


@dataclass(frozen=True, slots=True)
class SearchCoverage:
    requested_mode: SearchMode
    effective_modes: tuple[Literal["lexical", "semantic"], ...]
    state: CoverageState
    searched_channels: tuple[str, ...]
    is_top_k: bool
    truncated: bool
    channels: tuple[ChannelCoverage, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchResponse:
    results: tuple[ResultGroup, ...]
    coverage: SearchCoverage
