"""HTTP contracts for resumable file import intake."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, JsonValue, SerializerFunctionWrapHandler, model_serializer

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
    preparation_completed: int = 0
    preparation_total: int = 0
    ocr_completed: int = 0
    ocr_total: int = 0
    unreadable_total: int = 0
    preparation_pending_confirmation: dict[str, JsonValue] | None = None
    preparation_children: list[ImportPreparationChild] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_pdf_fields_for_ordinary_imports(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        payload = handler(self)
        if self.selected_total is None:
            for name in (
                "selected_total",
                "preparation_completed",
                "preparation_total",
                "ocr_completed",
                "ocr_total",
                "unreadable_total",
                "preparation_pending_confirmation",
                "preparation_children",
            ):
                payload.pop(name, None)
        return payload


class ImportSessionList(WireModel):
    sessions: list[ImportSessionStatus]


__all__ = ["ImportPreparationChild", "ImportSessionList", "ImportSessionStatus"]
