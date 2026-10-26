"""Strict transport contracts for temporary generic action comparison."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.models import WireModel


class ActionDraftScope(WireModel):
    kind: Literal["sheet_rows"]
    sheet_id: int = Field(gt=0)
    row_ids: list[Annotated[int, Field(gt=0)]] | None = None


class ActionDraft(WireModel):
    """Reusable settings and scope with no invocation or consent authority."""

    action_id: str = Field(min_length=1)
    scope: ActionDraftScope
    params: dict[str, JsonValue]
    output_names: dict[str, str]
    sheet_name: str | None = None


class ActionCompareDraftSource(WireModel):
    kind: Literal["draft"]
    draft: ActionDraft


class ActionCompareRunSource(WireModel):
    kind: Literal["run"]
    run_id: int = Field(gt=0)
    column_id: int = Field(gt=0)


ActionCompareBasis = Annotated[
    ActionCompareDraftSource | ActionCompareRunSource, Field(discriminator="kind")
]


class ActionCompareContextRequest(WireModel):
    source: ActionCompareBasis
    sample_row_ids: list[Annotated[int, Field(gt=0)]] = Field(
        min_length=1, max_length=20
    )


class ActionCompareReason(WireModel):
    code: str
    message: str


class ActionCompareLimits(WireModel):
    max_sample_rows: Literal[20]
    max_challengers: Literal[3]


class ActionCompareSheet(WireModel):
    id: int = Field(gt=0)
    name: str


class ActionCompareSourceColumn(WireModel):
    param: str
    column_id: int = Field(gt=0)
    name: str
    column_type: str
    format: str | None


class ActionCompareOutputColumn(WireModel):
    logical_key: str
    column_id: int | None = Field(default=None, gt=0)
    current_name: str
    column_type: str
    semantic_type: str | None = None
    format: str | None
    hidden: bool
    schema_: dict[str, JsonValue] = Field(alias="schema")


class ActionCompareReplacementTarget(WireModel):
    logical_key: str
    current_name: str
    column_id: int = Field(gt=0)


class ActionCompareCurrentBoundedValue(WireModel):
    kind: Literal["current_cell"]
    preview: str = Field(max_length=2_048)
    value_kind: Literal["text", "json", "bigint", "legacy_invalid"]
    total_characters: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    retrieval_revision: int = Field(gt=0)
    truncated: Literal[True]
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)


class ActionCompareCellOrigin(WireModel):
    kind: Literal["source_cell", "run_result", "manual_edit", "missing"]
    run_id: int | None = Field(default=None, gt=0)
    op_id: int | None = Field(default=None, gt=0)
    review_decision: Literal["reject_clear", "edit"] | None = None


class ActionCompareCell(WireModel):
    state: Literal["value", "null", "missing", "error"]
    value: JsonValue | None = None
    bounded_value: ActionCompareCurrentBoundedValue | None = None
    origin: ActionCompareCellOrigin
    error: str | None = None


class ActionCompareOriginSummary(WireModel):
    mixed_runs: bool
    includes_manual_edits: bool
    includes_reject_clear: bool


class ActionCompareContextResponse(WireModel):
    schema_version: Literal["frisket.action_compare_context.v1"]
    available: bool
    reason: ActionCompareReason | None
    limits: ActionCompareLimits
    baseline_mode: Literal["preview", "current_values"]
    comparison_basis: ActionCompareBasis
    draft: ActionDraft
    sheet: ActionCompareSheet
    sample_row_ids: list[int]
    sample_fingerprint: str
    locked_params: list[str]
    source_columns: list[ActionCompareSourceColumn]
    output_columns: list[ActionCompareOutputColumn]
    expected_output_signature: str
    replacement_targets: list[ActionCompareReplacementTarget]
    source_rows: dict[str, dict[str, ActionCompareCell]]
    baseline_rows: dict[str, dict[str, ActionCompareCell]] | None
    origin_summary: ActionCompareOriginSummary


class ActionComparePreviewAdmission(WireModel):
    basis: ActionCompareBasis
    expected_output_signature: str = Field(min_length=1)


class ActionComparePreviewRequest(WireModel):
    action: dict[str, JsonValue]
    comparison: ActionComparePreviewAdmission


__all__ = [
    "ActionCompareBasis",
    "ActionCompareContextRequest",
    "ActionCompareContextResponse",
    "ActionComparePreviewRequest",
    "ActionDraft",
    "ActionDraftScope",
]
