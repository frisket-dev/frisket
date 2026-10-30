"""Project-local expected answers, binary judgments, and saved comparison contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from frisket.contracts.http.action_compare import ActionCompareBasis
from frisket.contracts.http.models import NonNullQueryWireModel, WireModel

Rule = Literal["exact", "ignore_case", "number", "date", "set", "hand"]
Verdict = Literal["match", "miss", "unchecked", "unscored", "not_run"]
PositiveId = Annotated[int, Field(gt=0)]


class EvaluationOutput(WireModel):
    logical_key: str = Field(min_length=1)
    column_id: PositiveId | None
    current_name: str


class ExpectedKey(WireModel):
    expected_column_id: PositiveId
    target_column_id: PositiveId | None


class ExpectedField(WireModel):
    expected_column_id: PositiveId
    output_column_id: PositiveId
    logical_key: str = Field(min_length=1)
    rule: Rule = "exact"
    hidden: bool = False
    source: Literal["import", "disagreements"] = "import"


class ExpectedLinkInput(WireModel):
    target_sheet_id: PositiveId
    expected_sheet_id: PositiveId
    action_id: str = Field(min_length=1)
    keys: list[ExpectedKey] = Field(min_length=1, max_length=8)
    fields: list[ExpectedField] = Field(min_length=1, max_length=100)
    hidden: bool = False

    @model_validator(mode="after")
    def unique_mapping(self):
        for values in (
            [pair.expected_column_id for pair in self.keys],
            [pair.target_column_id for pair in self.keys],
            [field.expected_column_id for field in self.fields],
            [field.output_column_id for field in self.fields],
            [field.logical_key for field in self.fields],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Expected answer mappings must be unique")
        if self.target_sheet_id == self.expected_sheet_id:
            raise ValueError("Choose two different sheets")
        if {pair.expected_column_id for pair in self.keys} & {
            field.expected_column_id for field in self.fields
        }:
            raise ValueError("Key columns cannot also be expected fields")
        return self


class ExpectedLinkPreviewInput(ExpectedLinkInput):
    fields: list[ExpectedField] = Field(default_factory=list, max_length=100)


class ExpectedLink(ExpectedLinkInput):
    id: PositiveId
    expected_sheet_name: str
    target_sheet_name: str


class ExpectedLinks(WireModel):
    links: list[ExpectedLink]


class ExpectedJoinPreview(WireModel):
    matched: int
    not_found: int
    ambiguous_expected: int
    ambiguous_target: int
    unmatched_expected_row_ids: list[int]
    ambiguous_expected_row_ids: list[int]
    ambiguous_target_row_ids: list[int]


class ExpectedProjectionRequest(WireModel):
    sheet_id: PositiveId
    row_ids: list[PositiveId] = Field(max_length=500)


class ExpectedProjectionColumn(WireModel):
    id: str
    link_id: PositiveId
    expected_column_id: PositiveId
    output_column_id: PositiveId
    logical_key: str
    name: str
    rule: Rule
    hidden: bool
    source: Literal["import", "disagreements"]


class ExpectedProjectedCell(WireModel):
    value: JsonValue
    source_row_id: PositiveId | None
    source_column_id: PositiveId
    checked: bool


class ExpectedProjection(WireModel):
    links: list[ExpectedLink]
    columns: list[ExpectedProjectionColumn]
    rows: dict[str, dict[str, ExpectedProjectedCell]]


class ExpectedRuleChange(WireModel):
    rule: Rule


class ExpectedRuleSetting(ExpectedRuleChange):
    expected_column_id: PositiveId


class CurrentVariantRef(WireModel):
    kind: Literal["current"]


class PreviewVariantRef(WireModel):
    kind: Literal["preview"]
    preview_ids: list[str] = Field(min_length=1, max_length=100)


class SavedVariantRef(WireModel):
    kind: Literal["saved"]
    saved_id: PositiveId


VariantRef = Annotated[
    CurrentVariantRef | PreviewVariantRef | SavedVariantRef, Field(discriminator="kind")
]


class EvaluationVariantInput(WireModel):
    key: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=300)
    source: VariantRef


class EvaluationRequest(WireModel):
    basis: ActionCompareBasis
    row_ids: list[PositiveId] = Field(min_length=1, max_length=500)
    variants: list[EvaluationVariantInput] = Field(min_length=1, max_length=32)
    link_id: PositiveId | None = None

    @model_validator(mode="after")
    def unique_scope(self):
        if len(set(self.row_ids)) != len(self.row_ids):
            raise ValueError("Comparison rows must be unique")
        if len({variant.key for variant in self.variants}) != len(self.variants):
            raise ValueError("Variant keys must be unique")
        return self


class EvaluationBoundedValue(WireModel):
    preview: str = Field(max_length=2048)
    total_characters: int
    total_bytes: int
    truncated: Literal[True] = True


class EvaluationCell(WireModel):
    state: Literal["value", "null", "missing", "error", "pending", "cancelled"]
    value: JsonValue = None
    error: str | None = None
    bounded_value: EvaluationBoundedValue | None = None
    value_hash: str | None = None


class EvaluationVariantSnapshot(WireModel):
    sheet_id: PositiveId
    action_id: str
    output_columns: list[EvaluationOutput]
    settings: JsonValue
    rows: dict[str, dict[str, EvaluationCell]]
    source_fingerprints: dict[str, str]


class EvaluationAggregate(WireModel):
    checked: int
    attempted: int
    matches: int
    misses: int
    failed: int
    scored: int
    remaining: int
    accuracy: float | None
    coverage: float


class EvaluationVariant(WireModel):
    key: str
    label: str
    source: VariantRef
    settings: JsonValue
    rows: dict[str, dict[str, EvaluationCell]]
    source_fingerprints: dict[str, str]
    verdicts: dict[str, dict[str, Verdict]]
    scores: dict[str, EvaluationAggregate]


class EvaluationResponse(WireModel):
    sheet_id: PositiveId
    action_id: str
    output_columns: list[EvaluationOutput]
    row_ids: list[int]
    link: ExpectedLink | None
    expected: dict[str, dict[str, JsonValue]]
    expected_bounded: dict[str, dict[str, EvaluationBoundedValue]] = Field(
        default_factory=dict
    )
    variants: list[EvaluationVariant]
    best: dict[str, list[str]]
    rule_previews: dict[str, dict[str, dict[str, EvaluationAggregate]]]


class EvaluationJudgmentRequest(WireModel):
    comparison: EvaluationRequest
    variant_key: str
    row_id: PositiveId
    logical_key: str
    verdict: Literal["match", "miss"] | None


class ExpectedAnswerRequest(WireModel):
    basis: ActionCompareBasis
    link_id: PositiveId | None = None
    row_id: PositiveId
    logical_key: str
    value: JsonValue
    rule: Rule = "exact"


class ShelfSaveRequest(WireModel):
    comparison: EvaluationRequest
    variant_keys: list[str] = Field(min_length=1, max_length=32)


class ComparisonCaptureRequest(ShelfSaveRequest):
    target_id: PositiveId | None = None


class EvaluationCellRequest(WireModel):
    comparison: EvaluationRequest
    variant_key: str
    row_id: PositiveId
    logical_key: str


class ShelfRename(WireModel):
    label: str = Field(min_length=1, max_length=300)


class SavedComparisonSummary(WireModel):
    id: PositiveId
    label: str
    sheet_id: PositiveId
    action_id: str
    output_columns: list[EvaluationOutput]
    row_count: int
    saved_at: str
    kept: bool


class SavedComparison(SavedComparisonSummary):
    settings: JsonValue
    rows: dict[str, dict[str, EvaluationCell]]
    source_fingerprints: dict[str, str]


class SavedComparisons(WireModel):
    items: list[SavedComparisonSummary]


class EvaluationLinksQuery(NonNullQueryWireModel):
    sheet_id: PositiveId


class EvaluationShelfQuery(EvaluationLinksQuery):
    action_id: str


class EvaluationReviewQuery(NonNullQueryWireModel):
    run_id: PositiveId


class ComparisonVariantSettings(WireModel):
    key: str
    label: str
    settings: JsonValue


class ComparisonSettings(WireModel):
    schema_version: Literal["frisket.comparison_settings.v1"]
    sheet_id: PositiveId
    action_id: str
    row_ids: list[int]
    fields: list[ExpectedField]
    variants: list[ComparisonVariantSettings]
