"""Typed public HTTP projections for project-evidence reads.

The route envelopes are closed: an unexpected producer/store field is a
contract mismatch, not data we silently relay.  A small number of explicitly
producer-owned leaves remain recursive JSON because their contents are part of
the evidence payload rather than a stable HTTP envelope (metadata, selectors,
geometry, previews, raw provider data, and value references).
"""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from frisket.contracts.action import ActionError
from frisket.contracts.http.models import HttpError, WireModel


class EvidenceLinkSummary(WireModel):
    id: int
    stable_id: str
    export_ref: str
    status: str
    role: str
    evidence_kind: str
    span_count: int
    artifact_count: int
    snippet: str | None
    viewer_href: str


class CellEvidenceResponse(WireModel):
    schema_version: Literal["frisket.cell_evidence.v1"]
    sheet_id: int
    row_id: int
    column_id: int
    current_value_ref: JsonValue | None
    links: list[EvidenceLinkSummary]
    stale_count: int


class ColumnEvidenceRow(WireModel):
    row_id: int
    links: list[EvidenceLinkSummary]


class ColumnEvidenceResponse(WireModel):
    schema_version: Literal["frisket.column_evidence_batch.v1"]
    sheet_id: int
    column_id: int
    rows: list[ColumnEvidenceRow]


class TextAnnotationOutputColumn(WireModel):
    id: int
    name: str | None


class TextAnnotationProducer(WireModel):
    kind: str | None
    engine: str | None = None


class TextAnnotationSpanMetadata(WireModel):
    entity_type: str | None
    entity_fingerprint: str | None


class TextAnnotationCounts(WireModel):
    shown: int
    total: int
    invalid: int


class TextAnnotationUnpositioned(WireModel):
    reason: Literal[
        "unsupported_offset_unit", "content_hash_mismatch", "invalid_geometry"
    ]
    total: int


class TextAnnotationSpan(WireModel):
    occurrence_id: str
    start: int
    end: int
    quote: str
    metadata: TextAnnotationSpanMetadata


class TextAnnotationLayer(WireModel):
    toggle_key: str
    layer_family: str
    producer: TextAnnotationProducer
    output_column: TextAnnotationOutputColumn
    positioned: bool
    counts: TextAnnotationCounts | None = None
    spans: list[TextAnnotationSpan] | None = None
    unpositioned: TextAnnotationUnpositioned | None = None


class CellTextAnnotationsResponse(WireModel):
    sheet_id: int
    row_id: int
    column_id: int
    content_hash: str | None
    offset_unit: Literal["utf16_code_unit"]
    text: str | None
    layers: list[TextAnnotationLayer]


class EvidenceBlobReference(WireModel):
    hash: str
    url: str
    filename: str | None


class EvidenceArtifactReference(WireModel):
    kind: Literal["source_artifact"]
    stable_id: str
    artifact_kind: str
    media_type: str
    blob: EvidenceBlobReference | None
    source_url: str | None
    external_ref: JsonValue


class EvidenceProducer(WireModel):
    """Known evidence-link producer keys emitted by current writers."""

    action_kind: str | None = None
    source_action_kind: str | None = None
    kind: str | None = None
    engine: str | None = None
    model: str | None = None
    grounding_method: str | None = None
    projection_version: str | None = None
    render_mode: str | None = None
    warnings: list[str] | None = None
    project_id: str | None = None
    field: str | None = None
    output_role: str | None = None
    model_call_id: int | None = None
    item_index: int | None = None
    repointed_from_link_id: int | None = None
    repointed_from_row_id: int | None = None


class EvidenceViewerLink(WireModel):
    id: int
    stable_id: str
    export_ref: str
    subject_kind: str
    subject_ref: JsonValue
    sheet_id: int | None
    row_id: int | None
    column_id: int | None
    run_id: int | None
    op_id: int | None
    receipt_id: str | None
    role: str
    status: str
    confidence: float | None
    pinned: bool
    producer: EvidenceProducer
    stale_reason: str | None
    stale_at: str | None
    created_at: str
    text_layer_hash_mismatch: bool


class EvidencePageImage(WireModel):
    blob_hash: str
    url: str
    width: int | None
    height: int | None


class EvidencePageRegion(WireModel):
    id: int
    stable_id: str
    bbox: JsonValue
    snippet: str | None
    raw: JsonValue


class EvidenceViewerPage(WireModel):
    page: int
    image: EvidencePageImage | None
    text: str | None
    regions: list[EvidencePageRegion]


class EvidenceTemporalRun(WireModel):
    index: int
    start_ms: int
    end_ms: int
    span_ids: list[str]
    clip_url: str | None


class EvidenceViewerSpan(WireModel):
    id: int
    stable_id: str
    export_ref: str
    span_kind: str
    rank: int
    span_role: str
    required: bool
    note: str | None
    status: str
    selector: JsonValue
    quote: str | None
    snippet: str | None
    text_layer_hash: str | None
    preview: JsonValue
    raw: JsonValue
    warnings: list[str]
    deep_link_url: str | None
    clip_url: str | None
    run_index: int | None


class EvidenceSourceCell(WireModel):
    sheet_id: int | None
    row_id: int | None
    column_id: int | None


class EvidenceViewerArtifact(WireModel):
    id: int
    stable_id: str
    export_ref: str
    artifact_kind: str
    media_type: str
    title: str | None
    filename: str | None
    page_count: int | None
    duration_ms: int | None
    source_url: str | None
    canonical_url: str | None
    source_cell: EvidenceSourceCell | None
    external_ref: JsonValue
    artifact_ref: EvidenceArtifactReference
    metadata: JsonValue
    spans: list[EvidenceViewerSpan]
    pages: list[EvidenceViewerPage]
    runs: list[EvidenceTemporalRun]


class EvidenceViewerResponse(WireModel):
    schema_version: Literal["frisket.evidence_viewer.v1"]
    link: EvidenceViewerLink
    artifacts: list[EvidenceViewerArtifact]
    warnings: list[str]


PROJECT_EVIDENCE_ERROR_RESPONSES = {
    400: {"model": ActionError},
    # Project lookup is detail-wrapped; per-cell/link/column misses are the
    # older bare ActionError envelope.  Both are observable public behavior.
    404: {"model": ActionError | HttpError},
}


__all__ = [
    "CellEvidenceResponse",
    "CellTextAnnotationsResponse",
    "ColumnEvidenceResponse",
    "EvidenceArtifactReference",
    "EvidenceBlobReference",
    "EvidenceLinkSummary",
    "EvidencePageImage",
    "EvidencePageRegion",
    "EvidenceProducer",
    "EvidenceSourceCell",
    "EvidenceTemporalRun",
    "EvidenceViewerArtifact",
    "EvidenceViewerLink",
    "EvidenceViewerPage",
    "EvidenceViewerResponse",
    "EvidenceViewerSpan",
    "PROJECT_EVIDENCE_ERROR_RESPONSES",
    "TextAnnotationCounts",
    "TextAnnotationLayer",
    "TextAnnotationOutputColumn",
    "TextAnnotationProducer",
    "TextAnnotationSpan",
    "TextAnnotationSpanMetadata",
    "TextAnnotationUnpositioned",
]
