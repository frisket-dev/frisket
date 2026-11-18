"""HTTP contracts for resumable file import intake."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.contracts.http.import_errors import ImportSourceErrorGroup
from frisket.contracts.http.models import WireModel


class ImportPreparationChild(WireModel):
    action_id: Literal["media.read_pdf_text", "media.ocr"]
    row_count: int
    completed_rows: int
    status: str
    receipt_id: str | None = None
    run_id: int | None = None
    job_id: int | None = None


class ImportSessionStatus(WireModel):
    import_ref: str
    intake_kind: Literal["files", "table"] = "files"
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
    committed_rows: int = 0
    committed_bytes: int = 0
    sheet_id: int | None = None
    sheet_name: str
    cancel_requested: bool
    sealed: bool
    error: str | None = None
    # Availability of the handoff, independent of preparation execution status.
    search_preparation_status: Literal["pending", "available", "none"]
    search_preparation_batch_id: str | None = None
    selected_total: int | None = None
    estimated_rows: int | None = None
    processed_rows: int = 0
    skipped_duplicates: int = 0
    problem_count: int = 0
    source_label: str | None = None
    can_rerun_workbook: bool = False
    source_error_groups: list[ImportSourceErrorGroup] = Field(default_factory=list)
    preparation_completed: int = 0
    preparation_total: int = 0
    ocr_completed: int = 0
    ocr_total: int = 0
    unreadable_total: int = 0
    preparation_stopped: bool = False
    semantic_preparation_error: str | None = None
    preparation_children: list[ImportPreparationChild] = Field(default_factory=list)
    # Rows whose page text is being read now, and rows still waiting, front
    # first; each list is bounded.
    reading_row_ids: list[int] = Field(default_factory=list, max_length=500)
    waiting_row_ids: list[int] = Field(default_factory=list, max_length=500)
    failed_pages: int = Field(default=0, ge=0)


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
    "ImportPreparationChild",
    "ImportPreparationRetryEstimate",
    "ImportPreparationRetryRequest",
    "ImportSessionList",
    "ImportSessionStatus",
]
