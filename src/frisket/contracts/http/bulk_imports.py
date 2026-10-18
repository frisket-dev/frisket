"""HTTP contracts for deterministic staged bulk imports."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from frisket.contracts.http.models import WireModel
from frisket.contracts.http.pdf_imports import PdfImportStatus


BulkImportOutputKind = Literal["csv_group", "xlsx", "files_group", "email"]


class BulkImportQuestion(WireModel):
    id: str
    kind: Literal["csv_combine"]
    default: Literal["combine"]
    logical_paths: list[str]


class BulkImportProposedOutput(WireModel):
    id: str
    kind: BulkImportOutputKind
    sheet_name: str
    logical_paths: list[str]


class BulkImportPlanResponse(WireModel):
    plan_id: str
    questions: list[BulkImportQuestion]
    proposed_outputs: list[BulkImportProposedOutput]
    message: str


class PrepareImportFilesPdfResponse(WireModel):
    kind: Literal["pdf_import"]
    draft: PdfImportStatus


class PrepareImportFilesBulkResponse(WireModel):
    kind: Literal["bulk"]
    plan: BulkImportPlanResponse


PrepareImportFilesResponse = Annotated[
    PrepareImportFilesPdfResponse | PrepareImportFilesBulkResponse,
    Field(discriminator="kind"),
]


class BulkImportExecuteBody(WireModel):
    decisions: dict[str, str] = Field(default_factory=dict)


class BulkImportCreatedOutput(WireModel):
    id: str
    kind: Literal["sheet"]
    sheet_id: int
    sheet_name: str
    logical_paths: list[str]
    rows: int
    search_preparation_batch_id: str | None = None


class BulkImportFailedOutput(WireModel):
    id: str
    kind: BulkImportOutputKind
    logical_paths: list[str]
    logical_paths_omitted: int = Field(
        default=0,
        ge=0,
        exclude_if=lambda value: value == 0,
    )
    error: str


class BulkImportExecuteResponse(WireModel):
    created: list[BulkImportCreatedOutput]
    failed: list[BulkImportFailedOutput]
    failed_omitted: int = Field(
        default=0,
        ge=0,
        exclude_if=lambda value: value == 0,
    )
    first_sheet_id: int | None
    warnings: list[str] = Field(default_factory=list)
    message: str


__all__ = [
    "BulkImportCreatedOutput",
    "BulkImportExecuteBody",
    "BulkImportExecuteResponse",
    "BulkImportFailedOutput",
    "BulkImportPlanResponse",
    "BulkImportProposedOutput",
    "BulkImportQuestion",
    "PrepareImportFilesBulkResponse",
    "PrepareImportFilesPdfResponse",
    "PrepareImportFilesResponse",
]
