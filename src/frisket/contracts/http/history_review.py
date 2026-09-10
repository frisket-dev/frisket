"""Strict HTTP response contracts for history and review read endpoints.

The routes keep their established payloads intact.  These models make the
existing public shapes explicit at the response boundary; they do not invent a
second summary or normalize the separate review-queue endpoint.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.models import RunRowErrorSummary, WireModel


class HistoryOutputColumn(WireModel):
    id: int
    name: str


class HistoryRun(WireModel):
    run_id: int
    status: str
    action_kind: str
    model: str | None
    params: dict[str, JsonValue]
    total_rows: int = Field(ge=0)
    completed_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    cost_estimate: float | None
    cost_actual: float | None
    started_at: str
    finished_at: str | None
    worker_version: str | None
    output_columns: list[HistoryOutputColumn]
    row_errors: RunRowErrorSummary | None


class HistoryOperation(WireModel):
    id: int
    index: int = Field(ge=0)
    kind: str
    label: str | None
    status: str
    barrier: bool
    at_cursor: bool
    created_at: str
    run: HistoryRun | None


class HistoryTarget(WireModel):
    id: int
    index: int = Field(ge=0)
    barrier: bool


class HistoryRevision(WireModel):
    total: int = Field(ge=0)
    max_op_id: int = Field(ge=0)
    op_cursor: int = Field(ge=0)


class HistoryPage(WireModel):
    schema_version: Literal["frisket.history_page.v1"]
    order: Literal["asc"]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more_before: bool
    has_more_after: bool
    prev_offset: int | None
    next_offset: int | None
    cursor_index: int
    cursor_op: HistoryOperation | None
    cursor_op_loaded: bool
    undo_target: HistoryTarget | None
    redo_target: HistoryTarget | None
    revision: HistoryRevision
    ops: list[HistoryOperation]


class ColumnRunColumn(WireModel):
    id: int
    name: str
    type: str
    ai_generated: bool
    current_run_id: int | None
    latest_run_id: int | None
    mixed_origins: bool


class ReviewScore(WireModel):
    passed: int = Field(ge=0)
    graded: int = Field(ge=0)


class JudgeReviewScore(ReviewScore):
    run_id: int
    model: str | None
    started_at: str
    verdict_column_id: int
    compared: int = Field(ge=0)
    disagreement_count: int | None = Field(default=None, ge=0)


class ColumnRun(WireModel):
    run_id: int
    action_kind: str
    action_name: str
    model: str | None
    status: str
    # The run spec is intentionally an open JSON object owned by each action.
    spec: dict[str, JsonValue]
    total_rows: int = Field(ge=0)
    completed_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    cost_actual: float | None
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    tokens_in: int | None
    tokens_out: int | None
    current: bool
    human_score: ReviewScore
    judge_scores: list[JudgeReviewScore]


class ColumnRunsPage(WireModel):
    column: ColumnRunColumn
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more: bool
    next_offset: int | None
    current_run: ColumnRun | None
    current_run_loaded: bool
    # Presentation/action-family context, never a managed value pointer.
    latest_run: ColumnRun | None
    latest_run_loaded: bool
    # Server order is newest first (run id descending).
    runs: list[ColumnRun]


class ReviewBundleItem(WireModel):
    run_id: int
    row_id: int
    column_id: int
    column_name: str
    column_type: str
    sheet_id: int
    value: JsonValue
    confidence: float | None
    justification: str | None
    error: str | None
    review_state: str | None
    review_decision: Literal["accept", "reject", "reject_clear", "edit"] | None
    review_note: str | None
    role: Literal["field", "evidence"]
    chore: bool


class ReviewBundle(WireModel):
    id: str
    run_id: int
    row_id: int
    sheet_id: int
    sheet_name: str
    action_kind: str
    action_name: str
    model: str | None
    confidence: float | None
    source: dict[str, JsonValue]
    fields: list[ReviewBundleItem]
    evidence: list[ReviewBundleItem]
    items: list[ReviewBundleItem]


class ReviewBundlesPage(WireModel):
    schema_version: Literal["frisket.review_bundles_page.v1"]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more: bool
    next_offset: int | None
    bundles: list[ReviewBundle]


class ReviewCount(WireModel):
    count: int = Field(ge=0)


__all__ = [
    "ColumnRun",
    "ColumnRunColumn",
    "ColumnRunsPage",
    "HistoryOperation",
    "HistoryOutputColumn",
    "HistoryPage",
    "HistoryRevision",
    "HistoryRun",
    "HistoryTarget",
    "JudgeReviewScore",
    "ReviewBundle",
    "ReviewBundleItem",
    "ReviewBundlesPage",
    "ReviewCount",
    "ReviewScore",
]
