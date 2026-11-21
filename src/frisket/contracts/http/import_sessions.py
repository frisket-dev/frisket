"""HTTP contracts for resumable file import intake."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.contracts.http.import_errors import ImportSourceErrorGroup
from frisket.contracts.http.models import WireModel

MAX_IMPORT_ROW_STATE_IDS = 500


class ImportPreparationChild(WireModel):
    action_id: Literal["media.ocr"]
    row_count: int
    completed_rows: int
    status: str
    receipt_id: str | None


class ImportSessionStatus(WireModel):
    import_ref: str
    intake_kind: Literal["files", "table"]
    state: Literal[
        "admitting",
        "running",
        "paused",
        "cancelling",
        "cancelled",
        "completed",
        "kept",
        "removed",
    ]
    admitted_files: int
    admitted_bytes: int
    through: int
    committed_rows: int
    committed_bytes: int
    sheet_id: int | None
    sheet_name: str
    cancel_requested: bool
    sealed: bool
    error: str | None
    # Availability of the handoff, independent of preparation execution status.
    search_preparation_status: Literal["pending", "available", "none"]
    search_preparation_batch_id: str | None
    selected_total: int | None
    estimated_rows: int | None
    processed_rows: int
    skipped_duplicates: int
    problem_count: int
    source_label: str | None
    can_rerun_workbook: bool
    source_error_groups: list[ImportSourceErrorGroup]
    preparation_completed: int
    preparation_total: int
    ocr_completed: int
    ocr_total: int
    unreadable_total: int
    preparation_stopped: bool
    semantic_preparation_error: str | None
    preparation_children: list[ImportPreparationChild]
    # Rows whose page text is being read now, and rows still waiting, front
    # first; each list is bounded.
    reading_row_ids: list[int] = Field(max_length=MAX_IMPORT_ROW_STATE_IDS)
    waiting_row_ids: list[int] = Field(max_length=MAX_IMPORT_ROW_STATE_IDS)
    failed_pages: int = Field(ge=0)


class ImportPreparationRetryEstimate(WireModel):
    """The estimate for reading an import's failed pages again."""

    plan_token: str
    failed_pages: int = Field(ge=1)
    pages: int = Field(ge=0)
    estimate: ActionEstimate


class ImportPreparationRetryRequest(WireModel):
    plan_token: str = Field(min_length=1, max_length=128)
    confirmation: str | None = Field(default=None, max_length=128)


class ImportSessionList(WireModel):
    sessions: list[ImportSessionStatus]


__all__ = [
    "MAX_IMPORT_ROW_STATE_IDS",
    "ImportPreparationChild",
    "ImportPreparationRetryEstimate",
    "ImportPreparationRetryRequest",
    "ImportSessionList",
    "ImportSessionStatus",
]
