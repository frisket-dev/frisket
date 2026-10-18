"""Typed wire contract for durable multi-PDF import drafts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.contracts.http.models import WireModel
from frisket.contracts.http.selector_choices import (
    EngineModelSelection,
    EngineSelection,
)


PDF_IMPORT_SCHEMA_VERSION = "frisket.pdf_import.v1"
PDF_IMPORT_OCCURRENCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$"
PdfImportState = Literal[
    "open", "committing", "committed", "cancelling", "cancelled", "error"
]
PdfTextCategory = Literal[
    "text", "suspicious", "missing", "image", "other", "unreadable", "unchecked"
]
PdfOcrPolicy = Literal["missing_or_suspicious", "all", "none"]
PdfImportOcrSelection = Annotated[
    EngineSelection | EngineModelSelection, Field(discriminator="kind")
]


class PdfImportCategoryCounts(WireModel):
    text: int = Field(default=0, ge=0)
    suspicious: int = Field(default=0, ge=0)
    missing: int = Field(default=0, ge=0)
    image: int = Field(default=0, ge=0)
    other: int = Field(default=0, ge=0)
    unreadable: int = Field(default=0, ge=0)
    unchecked: int = Field(default=0, ge=0)


class PdfImportCreateRequest(WireModel):
    expected_files: int = Field(ge=2)
    expected_bytes: int = Field(ge=0)
    source_label: str | None = Field(default=None, max_length=512)


class PdfImportManifestFile(WireModel):
    client_file_id: str = Field(pattern=PDF_IMPORT_OCCURRENCE_ID_PATTERN)
    logical_path: str = Field(min_length=1, max_length=4096)
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)


class PdfImportManifestBatchRequest(WireModel):
    batch_id: str = Field(min_length=1, max_length=128)
    files: list[PdfImportManifestFile] = Field(min_length=1, max_length=256)


class PdfImportReselectFile(WireModel):
    logical_path: str = Field(min_length=1, max_length=4096)
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)


class PdfImportReselectRequest(WireModel):
    files: list[PdfImportReselectFile] = Field(min_length=1, max_length=256)


class PdfImportReselectResult(PdfImportReselectFile):
    matched: bool
    occurrence_id: str | None
    upload_needed: bool


class PdfImportReselectResponse(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    draft_id: str
    items: list[PdfImportReselectResult] = Field(max_length=256)


class PdfImportProgress(WireModel):
    files_done: int = Field(ge=0)
    files_total: int = Field(ge=0)
    bytes_done: int | None = Field(default=None, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    sealed: bool | None = None


class PdfImportAnalysisProgress(WireModel):
    files_done: int = Field(ge=0)
    files_total: int = Field(ge=0)


class PdfImportPreflight(WireModel):
    state: Literal["waiting", "ready", "insufficient"]
    free_bytes: int | None = Field(default=None, ge=0)


class PdfImportNewDestination(WireModel):
    kind: Literal["new"]
    sheet_name: str = Field(min_length=1, max_length=512)


class PdfImportExistingDestination(WireModel):
    kind: Literal["existing"]
    sheet_id: int = Field(gt=0)


PdfImportDestination = Annotated[
    PdfImportNewDestination | PdfImportExistingDestination, Field(discriminator="kind")
]


class PdfImportChoices(WireModel):
    ocr_policy: PdfOcrPolicy
    ocr_selection: PdfImportOcrSelection
    semantic_search: bool
    semantic_setup_confirmation: str | None
    destination: PdfImportDestination


class PdfImportChoicesRequest(PdfImportChoices):
    expected_revision: int = Field(ge=1)


class PdfImportMatch(WireModel):
    state: Literal["idle", "running", "ready", "error"]
    sheet_id: int | None = Field(default=None, gt=0)
    checked: int = Field(ge=0)
    total: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    new_category_counts: PdfImportCategoryCounts
    error: str | None


class PdfImportBatchOcrState(WireModel):
    state: Literal["idle", "ready", "error"]
    revision: int | None = Field(default=None, ge=1)
    advisory: bool = False
    purpose: Literal["batch_pdf_ocr"] | None = None
    ocr_action_params: dict | None = None
    options: dict | None = None
    ocr_policy: PdfOcrPolicy | None
    manifest_revision: int | None = Field(default=None, ge=1)
    manifest_hash: str | None
    selected_through: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    known_pages: int = Field(default=0, ge=0)
    work_scope_hash: str | None = None
    destination: dict | None = None
    match: dict | None = None
    estimate: ActionEstimate | None
    needs_confirmation: bool


class PdfImportSemanticSetup(WireModel):
    setup_ref: str | None
    display_name: str
    ready: bool
    runtime_ready: bool
    download_bytes: int | None = Field(default=None, ge=0)
    additional_runtime_download: bool
    needs_confirmation: bool
    confirmation_hash: str | None
    license: str | None
    source_url: str | None
    available: bool


class PdfImportPreviewJob(WireModel):
    job_id: str
    occurrence_id: str
    page: int = Field(ge=1)
    ocr_action_params: dict
    status: Literal["running", "done", "failed", "cancelled"]
    receipt_id: str | None
    error: str | None = None


class PdfImportCleanup(WireModel):
    bytes_done: int = Field(ge=0)
    bytes_total: int = Field(ge=0)


class PdfImportStatus(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    id: str
    draft_id: str
    state: PdfImportState
    paused: bool
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str
    expires_at: str
    source_label: str | None
    preflight: PdfImportPreflight
    manifest: PdfImportProgress
    upload: PdfImportProgress
    analysis: PdfImportAnalysisProgress
    category_counts: PdfImportCategoryCounts
    sample_ready_counts: PdfImportCategoryCounts
    can_review_text: bool
    choices: PdfImportChoices
    match: PdfImportMatch
    batch_ocr: PdfImportBatchOcrState
    semantic_setup: PdfImportSemanticSetup
    preview_jobs: list[PdfImportPreviewJob]
    cleanup: PdfImportCleanup
    commit: dict | None


class PdfImportListResponse(WireModel):
    items: list[PdfImportStatus]


class PdfImportSample(WireModel):
    occurrence_id: str
    logical_path: str
    filename: str
    size: int = Field(ge=0)
    page_count: int | None = Field(default=None, ge=1)
    category: PdfTextCategory
    analysis_state: Literal["pending", "done", "error"]
    sample_pages: list[int]
    thumbnail_url: str | None
    error: str | None


class PdfImportSamplesResponse(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    draft_id: str
    category: PdfTextCategory
    items: list[PdfImportSample] = Field(max_length=6)
    next_cursor: str | None


class PdfImportPageResponse(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    draft_id: str
    occurrence_id: str
    page: int = Field(ge=1)
    thumbnail_url: str
    native_text: str
    native_text_truncated: bool
    ocr_text: str | None
    ocr_blocks: list[dict]
    ocr_action_params: dict | None


class PdfImportOcrPreviewRequest(WireModel):
    ocr_selection: PdfImportOcrSelection
    confirmation: str | None = None


class PdfImportOcrPreviewEstimate(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    draft_id: str
    occurrence_id: str
    page: int = Field(ge=1)
    ocr_action_params: dict
    cached: bool
    estimate: ActionEstimate | None


class PdfImportOcrPreviewStart(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    draft_id: str
    occurrence_id: str
    page: int = Field(ge=1)
    ocr_action_params: dict
    job_id: str | None
    status: Literal["running", "done"]
    receipt_id: str | None
    estimate: ActionEstimate | None
    needs_confirmation: bool


class PdfImportBatchOcrEstimateRequest(WireModel):
    expected_revision: int = Field(ge=1)
    ocr_selection: PdfImportOcrSelection
    ocr_policy: PdfOcrPolicy


class PdfImportCommitRequest(WireModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    ocr_confirmation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PdfImportCommitResponse(WireModel):
    schema_version: Literal[PDF_IMPORT_SCHEMA_VERSION]
    draft_id: str
    commit_id: str
    status: Literal["accepted"]
    manifest_hash: str
    selected_total: int = Field(ge=0)
    available_through: int = Field(ge=0)
    uploads_complete: bool
    job: dict | None = None


__all__ = [
    name
    for name in globals()
    if name.startswith("PdfImport") or name == "PDF_IMPORT_SCHEMA_VERSION"
]
