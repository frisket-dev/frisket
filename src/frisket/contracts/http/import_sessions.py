"""HTTP contracts for resumable file import intake."""

from __future__ import annotations

from typing import Literal

from frisket.contracts.http.models import WireModel


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


class ImportSessionList(WireModel):
    sessions: list[ImportSessionStatus]


__all__ = ["ImportSessionList", "ImportSessionStatus"]
