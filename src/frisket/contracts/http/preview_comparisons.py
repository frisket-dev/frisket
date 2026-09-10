"""Typed success envelopes for the three native multipart scratch compares.

The comparison producers intentionally own the engine-specific inner records.
These contracts close each stable envelope and source receipt while preserving
those producer-owned JSON leaves for the existing workbench result types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from frisket.contracts.http.models import WireModel
from frisket.contracts.http.action_estimate_validation import ActionEstimate


class PreviewProducerRecord(WireModel):
    """Stable browser-readable core plus producer-owned JSON extensions."""

    model_config = ConfigDict(extra="allow", strict=True)

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class PreviewEngine(PreviewProducerRecord):
    id: str
    label: str
    tier: Literal["local", "sidecar", "hosted"]
    billable: bool


class PreviewMessage(PreviewProducerRecord):
    code: str
    message: str
    # Missing means the producer had no structured details. Explicit null is
    # not a wire value, and response_model_exclude_unset preserves omission.
    details: dict[str, JsonValue] = Field(default=None)  # type: ignore[assignment]


class PreviewEngineMessage(PreviewMessage):
    engine: str


class OcrCompareScratchEstimateSource(WireModel):
    filename: str | None
    mime: str | None
    size: int
    kind: Literal["image", "pdf"]
    pages: list[int]


class OcrCompareScratchEstimateResponse(WireModel):
    schema_version: Literal["frisket.ocr_compare_estimate.v1"]
    source: OcrCompareScratchEstimateSource
    estimate: ActionEstimate


class TranscribeCompareScratchEstimateSource(WireModel):
    filename: str | None
    mime: str | None
    size: int
    duration_seconds: float
    effective_duration_seconds: float
    time_limit_seconds: float


class TranscribeCompareScratchEstimateResponse(WireModel):
    schema_version: Literal["frisket.transcribe_compare_estimate.v1"]
    source: TranscribeCompareScratchEstimateSource
    estimate: ActionEstimate


class TopicSegmentationCompareEngine(PreviewProducerRecord):
    id: str
    label: str
    description: str
    version: str
    tier: Literal["local"]
    available: bool
    error: str | None
    recommended: bool


class TopicSegmentationCompareUnit(PreviewProducerRecord):
    id: str
    ordinal: int
    text: str
    speaker: str | None
    start_ms: int | None
    end_ms: int | None


class TopicSegmentationCompareBoundary(PreviewProducerRecord):
    id: str
    locator: dict[str, JsonValue]
    canonical_key: int
    locking_kind: Literal["point", "span"]
    strength: float | None
    label: str | None
    diagnostics: dict[str, JsonValue]


class TopicSegmentationCompareCanonicalBoundary(PreviewProducerRecord):
    key: int
    kind: Literal["point", "span"]
    candidate_ids: list[str]


class TopicSegmentationCompareSection(PreviewProducerRecord):
    index: int
    unit_ids: list[str]


class TopicSegmentationCompareUnitMembership(PreviewProducerRecord):
    unit_id: str
    section_indexes: list[int]


class TopicSegmentationCompareSettings(PreviewProducerRecord):
    detail: Literal["fewer", "balanced", "more"]


class TopicSegmentationCompareEngineResult(PreviewProducerRecord):
    variant_id: str
    engine: str
    engine_version: str | None
    settings: TopicSegmentationCompareSettings
    status: Literal["completed", "failed"]
    runtime_ms: int
    boundaries: list[TopicSegmentationCompareBoundary]
    canonical_boundaries: list[TopicSegmentationCompareCanonicalBoundary]
    sections: list[TopicSegmentationCompareSection]
    unit_membership: list[TopicSegmentationCompareUnitMembership]
    diagnostics: dict[str, JsonValue]
    warnings: list[PreviewMessage]
    errors: list[PreviewMessage]


class TopicSegmentationCompareMessage(PreviewEngineMessage):
    variant_id: str


class TopicSegmentationCompareScratchSource(WireModel):
    scratch: Literal[True]
    filename: str
    mime: str | None
    size: int
    source_kind: Literal["untimed_transcript", "timestamped_transcript"]
    snapshot_hash: str
    language: str | None


class TopicSegmentationCompareScratchResponse(WireModel):
    schema_version: Literal["frisket.topic_segmentation_compare.v1"]
    source: TopicSegmentationCompareScratchSource
    engines: list[TopicSegmentationCompareEngine]
    units: list[TopicSegmentationCompareUnit]
    results: list[TopicSegmentationCompareEngineResult]
    warnings: list[PreviewMessage]
    errors: list[TopicSegmentationCompareMessage]


__all__ = [
    "OcrCompareScratchEstimateResponse",
    "TopicSegmentationCompareScratchResponse",
    "TranscribeCompareScratchEstimateResponse",
]
