"""Typed HTTP contract for the guided PDF-packet split import."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.contracts.http.models import WireModel
from frisket.contracts.http.pdf_imports import PdfImportOcrSelection, PdfOcrPolicy


PDF_PACKET_SPLIT_SCHEMA_VERSION = "frisket.pdf_packet_split.v1"


class PdfPacketInfo(WireModel):
    blob_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=512)
    mime: Literal["application/pdf"]
    size: int = Field(ge=0)
    page_count: int | None = Field(ge=1)


class PdfPacketProgress(WireModel):
    status: Literal["queued", "running", "done", "error", "cancelled"]
    done: int = Field(ge=0)
    total: int | None = Field(ge=0)
    error: str | None


class PdfPacketPrepareState(WireModel):
    job_id: str
    progress: PdfPacketProgress
    pages_ready: list[int]
    native_text_pages: list[int]
    text_pages_read: list[int] = Field(default_factory=list)
    visual_pages_ready: int = Field(ge=0)


class PdfPacketJobState(WireModel):
    job_id: str
    kind: Literal["prepare", "ocr_sample", "ocr_full", "commit"]
    progress: PdfPacketProgress
    engine: str | None
    pages: list[int]
    receipt_id: str | None
    accounting: dict[str, JsonValue] | None


class PdfPacketCommitResult(WireModel):
    sheet_id: int = Field(gt=0)
    document_count: int = Field(ge=0)
    receipt_id: str | None
    import_ref: str | None = None
    job_id: int | None = None
    source_count: int = Field(default=0, ge=0)
    status: Literal[
        "queued", "running", "completed", "partial", "failed", "cancelled"
    ] = "completed"


class PdfPacketIndexRange(WireModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "PdfPacketIndexRange":
        if self.end < self.start:
            raise ValueError("index range end precedes start")
        return self


class PdfPacketCachedAnchor(WireModel):
    index: int = Field(ge=0)
    row_id: int = Field(gt=0)


class PdfPacketSheetScopeRequest(WireModel):
    sheet_id: int = Field(gt=0)
    scope: Literal["selected", "filtered", "all"]
    filter_: str | None = None
    sort: str | None = None
    parent_row_id: int | None = Field(default=None, gt=0)
    scope_row_ids: list[int] | None = None
    ranked_row_ids: list[int] | None = None

    @model_validator(mode="after")
    def _rows(self) -> "PdfPacketSheetScopeRequest":
        for rows in (self.scope_row_ids, self.ranked_row_ids):
            if rows is not None and (
                any(type(row) is not int or row <= 0 for row in rows)
                or len(rows) != len(set(rows))
            ):
                raise ValueError("row IDs must be unique positive integers")
        return self


class PdfPacketSheetScopeResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    order_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PdfPacketSheetCreateRequest(WireModel):
    sheet_id: int = Field(gt=0)
    file_column_id: int = Field(gt=0)
    organization: Literal["pages", "split_documents"]
    scope: Literal["selected", "filtered", "all"]
    filter_: str | None = None
    sort: str | None = None
    parent_row_id: int | None = Field(default=None, gt=0)
    scope_row_ids: list[int] | None = None
    ranked_row_ids: list[int] | None = None
    selected_row_ids: list[int] | None = None
    selected_index_ranges: list[PdfPacketIndexRange] | None = None
    cached_anchors: list[PdfPacketCachedAnchor] = Field(default_factory=list)
    expected_order_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _selection(self) -> "PdfPacketSheetCreateRequest":
        for rows in (self.scope_row_ids, self.ranked_row_ids, self.selected_row_ids):
            if rows is not None and (
                any(type(row) is not int or row <= 0 for row in rows)
                or len(rows) != len(set(rows))
            ):
                raise ValueError("row IDs must be unique positive integers")
        if self.scope == "selected":
            if bool(self.selected_row_ids) == bool(self.selected_index_ranges):
                raise ValueError("select row IDs or index ranges")
            if self.selected_index_ranges and (
                not self.cached_anchors or self.expected_order_hash is None
            ):
                raise ValueError(
                    "index selection requires cached anchors and an order hash"
                )
        elif (
            self.selected_row_ids is not None
            or self.selected_index_ranges is not None
            or self.cached_anchors
        ):
            raise ValueError("selection is only valid for selected scope")
        return self


class PdfPacketDraftCreateRequest(WireModel):
    draft_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    organization: Literal["pages", "split_documents"]


class PdfPacketSourceInfo(WireModel):
    key: str
    blob_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str
    size: int = Field(ge=0)
    page_count: int = Field(ge=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    display_start: int = Field(ge=1)
    source_url: str | None = None
    source_sheet_id: int | None = None
    source_row_id: int | None = None
    source_column_id: int | None = None
    prepared_page_count: int = Field(ge=0)
    has_prepared: bool
    prepared_complete: bool
    text_category: Literal["text", "missing", "suspicious", "unreadable"] | None = None
    manual_text_preview: str | None = None


class PdfPacketScopeExcluded(WireModel):
    row_id: int = Field(gt=0)
    reason: str


class PdfPacketScopePreview(WireModel):
    row_id: int = Field(gt=0)
    filename: str | None
    reason: str | None


class PdfPacketScopeSummary(WireModel):
    excluded: list[PdfPacketScopeExcluded]
    excluded_count: int = Field(ge=0)
    preview: list[PdfPacketScopePreview]
    order_hash: str


class PdfPacketSplitStatus(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    status: Literal[
        "preparing", "ready", "committing", "completed", "error", "cancelled"
    ]
    packet: PdfPacketInfo
    organization: Literal["pages", "split_documents"]
    sources: list[PdfPacketSourceInfo]
    text_categories: dict[str, int] = Field(default_factory=dict)
    fixed_starts: list[int]
    example_pages: list[int]
    sheet_scope: PdfPacketScopeSummary | None = None
    prepare: PdfPacketPrepareState
    text_source: Literal["unconfirmed", "native", "ocr"]
    ocr_engine: str | None
    ocr_pages: list[int]
    jobs: list[PdfPacketJobState]
    analysis_revision: int = Field(ge=0)
    expires_at: str
    commit_result: PdfPacketCommitResult | None


class PdfPacketPageResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    page: int = Field(gt=0)
    source_key: str
    local_page: int = Field(gt=0)
    preview_text: str | None = None
    preview_source: Literal["prepared", "native"] | None = None
    preview_text_kind: Literal["ocr", "native"] | None = None
    preview_truncated: bool = False
    thumbnail_ready: bool
    thumbnail_url: str
    native_text: str | None
    ocr_text: str | None
    ocr_blocks: list[dict[str, JsonValue]]
    ocr_engine: str | None


class PdfPacketOcrEstimateRequest(WireModel):
    engine: str | None = Field(default=None, min_length=1, max_length=128)
    ocr_selection: PdfImportOcrSelection | None = None
    force_rescan: bool = False
    scope: Literal["sample"]
    pages: list[int] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def _pages_match_scope(self) -> "PdfPacketOcrEstimateRequest":
        if self.engine is None and self.ocr_selection is None:
            raise ValueError("OCR engine selection is required")
        if (
            self.engine is not None
            and self.ocr_selection is not None
            and self.engine != self.ocr_selection.engine
        ):
            raise ValueError("OCR engine selections disagree")
        return self


class PdfPacketOcrEstimateResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    engine: str
    scope: Literal["sample"]
    pages: list[int]
    cached_pages: list[int]
    estimate: ActionEstimate | None


class PdfPacketBatchOcrEstimateRequest(WireModel):
    expected_analysis_revision: int = Field(ge=0)
    ocr_policy: PdfOcrPolicy
    ocr_selection: PdfImportOcrSelection


class PdfPacketBatchOcrEstimateResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    analysis_revision: int = Field(ge=0)
    pages: int = Field(ge=0)
    estimate: ActionEstimate | None


class PdfPacketOcrJobRequest(PdfPacketOcrEstimateRequest):
    confirmation: str | None = None


class PdfPacketJobStartResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    job_id: str | int | None
    status: Literal["running", "cached"] = "running"
    kind: Literal["ocr_sample", "ocr_full", "commit"]
    total: int = Field(ge=0)
    receipt_id: str | None
    import_ref: str | None = None
    sheet_id: int | None = None
    source_count: int | None = None


class PdfPacketTextSourceRequest(WireModel):
    kind: Literal["native", "ocr"]
    engine: str | None = None

    @model_validator(mode="after")
    def _ocr_requires_engine(self) -> "PdfPacketTextSourceRequest":
        if self.kind == "ocr" and not self.engine:
            raise ValueError("OCR text source requires an engine")
        if self.kind == "native" and self.engine is not None:
            raise ValueError("native text source does not accept an engine")
        return self


class PdfPacketPhraseRequest(WireModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    fuzzy: bool = False


class PdfPacketCandidatesRequest(WireModel):
    expected_analysis_revision: int = Field(ge=0)
    confirmed_starts: list[int] = Field(default_factory=list)
    example_pages: list[int] | None = None
    rejected: list[int] = Field(default_factory=list)
    threshold_pct: float = Field(default=80.0, ge=0.0, le=100.0)
    phrases: list[PdfPacketPhraseRequest] = Field(default_factory=list, max_length=32)


class PdfPacketStartKind(WireModel):
    id: int = Field(ge=0)
    confirmed_pages: list[int]


class PdfPacketPageMatch(WireModel):
    page: int = Field(gt=0)
    visual_score: float | None = Field(ge=0.0, le=100.0)
    closest_confirmed_page: int | None = Field(gt=0)
    kind_id: int | None = Field(ge=0)
    matched_phrase_ids: list[str]
    suggested: bool


class PdfPacketCandidatesResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    analysis_revision: int = Field(ge=0)
    clusters: list[PdfPacketStartKind]
    pages: list[PdfPacketPageMatch]
    suggested_pages: list[int]
    unsure_pages: list[int]
    phrase_counts: dict[str, int]
    accept_all_scope: Literal["packet"]


class PdfPacketDestination(WireModel):
    kind: Literal["new_sheet", "append", "replace"]
    name: str | None = Field(default=None, min_length=1, max_length=512)
    sheet_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _destination(self) -> "PdfPacketDestination":
        if self.kind == "new_sheet":
            if self.name is None or self.sheet_id is not None:
                raise ValueError("new sheet requires a name and no existing sheet")
        elif self.sheet_id is None or self.name is not None:
            raise ValueError("existing destination requires a sheet ID and no new name")
        return self


class PdfPacketCommitRequest(WireModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    confirmed_starts: list[int] = Field(default_factory=list)
    destination: PdfPacketDestination
    name_pattern: str = Field(
        default="{packet}-{index}.pdf", min_length=1, max_length=256
    )
    keep_ocr_text: bool = False
    carried_column_ids: list[int] = Field(default_factory=list)
    analysis_revision: int | None = Field(default=None, ge=0)
    ocr_policy: PdfOcrPolicy = "none"
    ocr_selection: PdfImportOcrSelection | None = None
    ocr_confirmation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _carried_columns(self) -> "PdfPacketCommitRequest":
        if any(
            type(column) is not int or column <= 0 for column in self.carried_column_ids
        ) or len(set(self.carried_column_ids)) != len(self.carried_column_ids):
            raise ValueError("carried columns must be distinct positive IDs")
        if self.ocr_policy != "none" and self.ocr_selection is None:
            raise ValueError("OCR policy requires an OCR selection")
        return self


__all__ = [name for name in globals() if name.startswith("PdfPacket")]
