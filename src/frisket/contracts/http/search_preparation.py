"""Strict HTTP contracts for local search preparation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from frisket.contracts.http.models import ActionJobProgress, WireModel


SearchPreparationStatus = Literal[
    "planning",
    "needs_consent",
    "preparing",
    "complete",
    "stopped",
    "failed",
]
SearchPreparationGroupKind = Literal["native_pdf", "ocr", "transcribe"]
SearchPreparationGroupStatus = Literal[
    "planning",
    "needs_consent",
    "waiting_for_download",
    "queued",
    "running",
    "complete",
    "not_prepared",
    "stopped",
    "failed",
]


class SearchPreparationCreateRequest(WireModel):
    sheet_id: int = Field(gt=0)
    row_ids: list[int] | None = None
    force_ocr: bool = False

    @model_validator(mode="after")
    def _canonical_scope(self) -> SearchPreparationCreateRequest:
        if self.force_ocr and self.row_ids is None:
            raise ValueError("force_ocr requires an explicit row scope")
        if self.row_ids is not None:
            if not self.row_ids or any(row_id <= 0 for row_id in self.row_ids):
                raise ValueError("row_ids must contain positive persisted ids")
            self.row_ids = sorted(set(self.row_ids))
        return self


class SearchPreparationDecisionRequest(WireModel):
    plan_hash: str = Field(min_length=1, max_length=96)
    decision: Literal["prepare", "not_now"]
    selected_setup_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _canonical_refs(self) -> SearchPreparationDecisionRequest:
        if any(not value.strip() for value in self.selected_setup_refs):
            raise ValueError("selected_setup_refs must be non-empty strings")
        self.selected_setup_refs = sorted(set(self.selected_setup_refs))
        if self.decision == "not_now" and self.selected_setup_refs:
            raise ValueError("not_now cannot select setup references")
        return self


class SearchPreparationGroup(WireModel):
    kind: SearchPreparationGroupKind
    count: int = Field(ge=0)
    status: SearchPreparationGroupStatus


class SearchPreparationModel(WireModel):
    setup_ref: str
    display_name: str
    purpose: Literal["read_text", "transcribe", "search_by_meaning"]
    required: bool
    ready: bool
    runtime_ready: bool
    can_setup: bool
    download_bytes: int | None = Field(default=None, ge=0)
    additional_runtime_download: bool
    remediation: str | None
    license: str
    source_url: str
    pull_id: int | None = Field(default=None, gt=0)


class SearchPreparationJob(WireModel):
    job_id: int = Field(gt=0)
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    action_kind: str | None = None
    progress: ActionJobProgress | None = None


class SearchPreparationBatch(WireModel):
    schema_version: Literal["frisket.search_preparation.v1"] = Field(
        default="frisket.search_preparation.v1", alias="schemaVersion"
    )
    id: str
    sheet_id: int = Field(gt=0)
    plan_hash: str
    status: SearchPreparationStatus
    groups: list[SearchPreparationGroup]
    models: list[SearchPreparationModel]
    jobs: list[SearchPreparationJob]
    can_stop: bool
    can_setup: bool
    errors: list[str]
    warnings: list[str] = Field(default_factory=list)


class SearchPreparationIndexing(WireModel):
    status: Literal["idle", "pending", "queued", "running", "failed"]
    job_id: int | None = Field(default=None, gt=0)
    error: str | None = None


class SearchPreparationBatchList(WireModel):
    batches: list[SearchPreparationBatch]
    indexing: SearchPreparationIndexing


class SearchPreparationTextSample(WireModel):
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)
    filename: str | None
    page_number: int = Field(gt=0)
    page_count: int = Field(gt=0)
    page_scoped: bool
    preview_url: str
    text: str
    source: Literal["native", "prepared"]
    truncated: bool


class SearchPreparationTextSamples(WireModel):
    schema_version: Literal["frisket.search_preparation_text_samples.v1"] = Field(
        default="frisket.search_preparation_text_samples.v1", alias="schemaVersion"
    )
    batch_id: str
    samples: list[SearchPreparationTextSample]


__all__ = [
    "SearchPreparationBatch",
    "SearchPreparationBatchList",
    "SearchPreparationCreateRequest",
    "SearchPreparationDecisionRequest",
    "SearchPreparationGroup",
    "SearchPreparationIndexing",
    "SearchPreparationJob",
    "SearchPreparationModel",
    "SearchPreparationTextSample",
    "SearchPreparationTextSamples",
]
