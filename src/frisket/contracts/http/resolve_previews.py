"""Compatibility contracts for the synchronous resolve preview routes.

The preview services deliberately own validation of object payloads so their
typed bare action errors stay aligned with their commit-action counterparts.
These models therefore preserve raw values and ignored object extensions while
leaving non-object JSON at FastAPI's ordinary 422 boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, JsonValue

from frisket.contracts.http.models import CoerciveRequest, WireModel


class _CompatibleRequest(CoerciveRequest):
    pass


class ClusterPreviewRequest(_CompatibleRequest):
    sheet_id: Any = None
    input_column: Any = None
    method: Any = "fingerprint"
    min_size: Any = 2
    threshold: Any = None
    ngram_size: Any = None
    key_template: Any = None


class ColumnValuesPreviewRequest(_CompatibleRequest):
    sheet_id: Any = None
    input_column: Any = None
    search: Any = None
    limit: Any = None
    offset: Any = None


class ReplaceRulesPreviewRequest(_CompatibleRequest):
    sheet_id: Any = None
    input_column: Any = None
    rules: Any = None
    unmatched: Any = "keep"
    test_value: Any = None


class _OpenPreviewResponse(WireModel):
    """Strict stable preview core with lossless producer-owned JSON additions."""

    model_config = ConfigDict(extra="allow", strict=True)

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class ClusterPreviewValue(_OpenPreviewResponse):
    value: str
    count: int


class ClusterPreviewGroup(_OpenPreviewResponse):
    key: str
    canonical: str
    size: int
    values: list[ClusterPreviewValue]
    row_ids: list[int]


class ClusterPreviewResponse(_OpenPreviewResponse):
    """Required cluster review core, plus method-owned additive JSON fields."""

    schema_version: Literal["frisket.cluster_preview.v1"]
    sheet_id: int
    sheet_name: str
    column_id: int
    column: str
    column_type: str
    method: Literal["fingerprint", "ngram_fingerprint", "semantic"]
    min_size: int
    row_count: int
    value_hash: str
    count: int
    clusters: list[ClusterPreviewGroup]
    semantic: bool


class ColumnValueCount(_OpenPreviewResponse):
    value: str
    count: int


class ListFacetScalarSelector(_OpenPreviewResponse):
    """An exact JSON scalar selection for a generic JSON-list facet."""

    kind: Literal["scalar"]
    # The producer only emits strings, booleans, and finite JavaScript-safe
    # numbers. Keeping the response type narrow makes the server-authored
    # selector safe to replay without treating arbitrary JSON as a predicate.
    value: str | bool | int | float


class ListFacetEntitySelector(_OpenPreviewResponse):
    """An exact entity mention selection for a marked entity list."""

    kind: Literal["entity"]
    type: str
    text: str


class ListFacetChoice(_OpenPreviewResponse):
    key: str
    label: str
    count: int
    selector: ListFacetScalarSelector | ListFacetEntitySelector


class ListFacetPreview(_OpenPreviewResponse):
    """Additive element-level facet facts for a JSON array column."""

    distinct: int
    offset: int
    limit: int
    truncated: bool
    search: str | None
    choices: list[ListFacetChoice]


class NumberColumnDistributionBin(_OpenPreviewResponse):
    start: float
    end: float
    count: int


class NumberColumnDistribution(_OpenPreviewResponse):
    kind: Literal["number"]
    min: float
    max: float
    bins: list[NumberColumnDistributionBin]


class IntegerColumnDistributionBin(_OpenPreviewResponse):
    start: str
    end: str
    count: int


class IntegerColumnDistribution(_OpenPreviewResponse):
    kind: Literal["integer"]
    min: str
    max: str
    bins: list[IntegerColumnDistributionBin]


class DateColumnDistributionBin(_OpenPreviewResponse):
    start: str
    end: str
    count: int


class DateColumnDistribution(_OpenPreviewResponse):
    kind: Literal["date"]
    min: str
    max: str
    bins: list[DateColumnDistributionBin]


class ColumnValuesPreviewResponse(_OpenPreviewResponse):
    """Required distinct-values preview core with additive producer metadata."""

    schema_version: Literal["frisket.column_values_preview.v1"]
    sheet_id: int
    column_id: int
    input_column: str
    total_rows: int
    distinct: int
    missing: int
    values: list[ColumnValueCount]
    offset: int
    limit: int
    truncated: bool
    value_hash: str
    search: str | None
    distribution: (
        NumberColumnDistribution
        | IntegerColumnDistribution
        | DateColumnDistribution
        | None
    )
    list_facet: ListFacetPreview | None = None


class ReplaceRuleMatchCount(_OpenPreviewResponse):
    index: int
    matched_rows: int
    matched_values: int


class ReplaceRulesTestResult(_OpenPreviewResponse):
    matched_rule_index: int | None
    output: str | None


class ReplaceRulesPreviewResponse(_OpenPreviewResponse):
    """Required rules-preview core with additive producer metadata."""

    schema_version: Literal["frisket.replace_rules_preview.v1"]
    sheet_id: int
    column_id: int
    total_rows: int
    rule_counts: list[ReplaceRuleMatchCount]
    unmatched_rows: int
    test_result: ReplaceRulesTestResult | None
    value_hash: str


__all__ = [
    "ClusterPreviewRequest",
    "ClusterPreviewResponse",
    "ColumnValuesPreviewRequest",
    "ColumnValuesPreviewResponse",
    "ReplaceRulesPreviewRequest",
    "ReplaceRulesPreviewResponse",
]
