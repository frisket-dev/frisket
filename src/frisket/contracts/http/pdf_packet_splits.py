"""Typed HTTP contract for the guided PDF-packet split import."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.contracts.http.models import WireModel


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
    document_count: int = Field(gt=0)
    receipt_id: str | None


class PdfPacketSplitStatus(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    status: Literal[
        "preparing", "ready", "committing", "completed", "error", "cancelled"
    ]
    packet: PdfPacketInfo
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
    thumbnail_ready: bool
    thumbnail_url: str
    native_text: str | None
    ocr_text: str | None
    ocr_blocks: list[dict[str, JsonValue]]
    ocr_engine: str | None


class PdfPacketOcrEstimateRequest(WireModel):
    engine: str = Field(min_length=1, max_length=128)
    scope: Literal["sample", "all"]
    pages: list[int] | None = None

    @model_validator(mode="after")
    def _pages_match_scope(self) -> "PdfPacketOcrEstimateRequest":
        if self.scope == "sample" and not self.pages:
            raise ValueError("sample OCR requires at least one page")
        if self.scope == "all" and self.pages is not None:
            raise ValueError("full OCR does not accept an explicit page list")
        return self


class PdfPacketOcrEstimateResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    engine: str
    scope: Literal["sample", "all"]
    pages: list[int]
    cached_pages: list[int]
    estimate: ActionEstimate | None


class PdfPacketOcrJobRequest(PdfPacketOcrEstimateRequest):
    confirmation: str | None = None


class PdfPacketJobStartResponse(WireModel):
    schema_version: Literal["frisket.pdf_packet_split.v1"]
    split_id: str
    job_id: str
    kind: Literal["ocr_sample", "ocr_full", "commit"]
    total: int = Field(ge=0)
    receipt_id: str | None


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
    confirmed_starts: list[int] = Field(min_length=1)
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
    question_pages: list[int] = Field(max_length=3)
    phrase_counts: dict[str, int]
    accept_all_scope: Literal["packet"]


class PdfPacketDestination(WireModel):
    kind: Literal["new_sheet"]
    name: str = Field(min_length=1, max_length=80)


class PdfPacketCommitRequest(WireModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    confirmed_starts: list[int] = Field(min_length=1)
    destination: PdfPacketDestination
    name_pattern: str = Field(min_length=1, max_length=256)
    keep_ocr_text: bool = False


__all__ = [name for name in globals() if name.startswith("PdfPacket")]
