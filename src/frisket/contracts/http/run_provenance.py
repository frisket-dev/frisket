"""Public HTTP response contracts for run trace and project provenance reads."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.models import WireModel


class RunTraceRow(WireModel):
    """One bounded, redacted model-call trace row.

    Trace records are deliberately JSON-shaped: prompt/data/calls/retries are
    captured evidence, not a second application schema. Optional fields stay
    omitted when an older producer did not write them.
    """

    row_id: int | None = None
    trace_id: str | None = None
    prompt: JsonValue | None = None
    raw_response: str | None = None
    data: JsonValue | None = None
    error: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    cached: bool | None = None
    latency_ms: int | None = None
    retries: list[JsonValue] | None = None
    calls: list[JsonValue] | None = None


class RunTraceRowTrace(WireModel):
    run_id: int
    trace_id: str | None
    action_kind: str
    action_name: str
    model: str | None
    created_at: str | None
    row: RunTraceRow | None
    record_count: int = Field(ge=0)


class RunTraceRowRun(WireModel):
    id: int
    sheet_id: int
    action_kind: str
    action_name: str
    status: str
    model: str | None
    started_at: str | None = None
    finished_at: str | None = None
    cost_actual: float | None = None


class RunTraceRowEvidence(WireModel):
    schema_version: Literal["frisket.actions.v1"]
    action: Literal["run_trace_row"]
    project_id: str
    run_id: int
    row_id: int
    column_id: int | None
    recorded: bool
    status: Literal["recorded", "not_recorded", "row_not_in_run", "missing"]
    state: Literal["recorded", "not_recorded", "row_not_in_run", "missing"]
    run: RunTraceRowRun
    trace: RunTraceRowTrace | None
    cell: dict[str, JsonValue] | None
    absence: dict[str, JsonValue] | None


class ProvenanceModelSummary(WireModel):
    model: str
    provider: str | None
    runs: int = Field(ge=0)
    rows: int = Field(ge=0)
    cost: float


class ProvenanceActionKindSummary(WireModel):
    action_kind: str
    action_name: str
    runs: int = Field(ge=0)
    rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    cost: float


class ProvenanceRunSummary(WireModel):
    run_id: int
    sheet_id: int
    action_kind: str
    action_name: str
    model: str | None
    provider: str | None
    status: str
    total_rows: int = Field(ge=0)
    completed_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    cost: float | None
    started_at: str | None
    finished_at: str | None


class ProvenanceReceiptSummary(WireModel):
    receipt_id: str
    action_kind: str
    status: str
    run_id: int | None
    created_at: str | None


class ProvenancePage(WireModel):
    schema_version: Literal[
        "frisket.provenance_runs_page.v1",
        "frisket.provenance_receipts_page.v1",
    ]
    order: Literal["desc"]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more: bool
    next_offset: int | None


class ProvenanceManifest(WireModel):
    project_id: str
    models: list[ProvenanceModelSummary]
    touched: list[str]
    providers: list[str]
    action_kinds: list[ProvenanceActionKindSummary]
    runs: list[ProvenanceRunSummary]
    runs_page: ProvenancePage
    receipts: list[ProvenanceReceiptSummary]
    receipts_page: ProvenancePage
    total_cost: float
    has_unknown_costs: bool
    unknown_cost_runs: int = Field(ge=0)


__all__ = [
    "ProvenanceManifest",
    "RunTraceRowEvidence",
]
