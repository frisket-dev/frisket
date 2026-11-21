"""Typed public HTTP projections for project-citation reads.

The route envelopes are closed: an unexpected producer/store field is a
contract mismatch, not data we silently relay.  A small number of explicitly
producer-owned leaves remain recursive JSON because their contents are part of
the citation payload rather than a stable HTTP envelope (metadata, selectors,
geometry, previews, raw provider data, and value references).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from frisket.contracts.action import ActionError
from frisket.contracts.http.models import HttpError, WireModel


class CitationSummary(WireModel):
    id: int
    stable_id: str
    export_ref: str
    status: str
    role: str
    citation_kind: str
    span_count: int
    artifact_count: int
    snippet: str | None
    viewer_href: str


class CellCitationsResponse(WireModel):
    schema_version: Literal["frisket.cell_citations.v1"]
    sheet_id: int
    row_id: int
    column_id: int
    current_value_ref: JsonValue | None
    links: list[CitationSummary]
    stale_count: int


class ColumnCitationsRow(WireModel):
    row_id: int
    links: list[CitationSummary]


class ColumnCitationsResponse(WireModel):
    schema_version: Literal["frisket.column_citations_batch.v1"]
    sheet_id: int
    column_id: int
    rows: list[ColumnCitationsRow]


class RowSourceDocument(WireModel):
    sheet_id: int
    sheet_name: str
    row_id: int
    row_position: int = Field(ge=0)
    column_id: int
    column_name: str
    column_type: Literal["file", "image"]
    filename: str | None
    page: int | None = Field(default=None, ge=1)


class RowSourceDocumentsResponse(WireModel):
    schema_version: Literal["frisket.row_source_documents.v1"]
    sheet_id: int
    row_id: int
    ancestry_found: bool
    lineage_stale: bool
    lineage_freshness: Literal["synced", "stale", "unknown", "checking"] | None = None
    unavailable_source_count: int = Field(ge=0)
    truncated: bool
    citations: list[CitationSummary]
    documents: list[RowSourceDocument]


class TextAnnotationGeneratedColumn(WireModel):
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
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    quote_truncated: bool = False


class CitationTextWindow(WireModel):
    """Absolute Unicode codepoint coordinates for one bounded text passage."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    total_characters: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    retrieval_revision: int | None
    previous_start: int | None
    next_start: int | None
    reached_start: bool
    reached_end: bool


class TextAnnotationLayer(WireModel):
    toggle_key: str
    layer_family: str
    producer: TextAnnotationProducer
    generated_column: TextAnnotationGeneratedColumn
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
    window: CitationTextWindow | None = None


class CitationBlobReference(WireModel):
    hash: str
    url: str
    filename: str | None


class CitationArtifactReference(WireModel):
    kind: Literal["source_artifact"]
    stable_id: str
    artifact_kind: str
    media_type: str
    blob: CitationBlobReference | None
    source_url: str | None
    external_ref: JsonValue


class CitationProducer(WireModel):
    """Known citation producer keys emitted by current writers."""

    schema_version: str | None = None
    value_hash: str | None = None
    capability: str | None = None
    source_row_id: int | None = None
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
    source: str | None = None
    model_call_id: int | None = None
    item_index: int | None = None
    repointed_from_link_id: int | None = None
    repointed_from_row_id: int | None = None


class CitationViewerLink(WireModel):
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
    item_index: int | None = Field(default=None, ge=0)
    role: str
    status: str
    confidence: float | None
    pinned: bool
    producer: CitationProducer
    stale_reason: str | None
    stale_at: str | None
    created_at: str
    text_layer_hash_mismatch: bool


class CitationPageImage(WireModel):
    blob_hash: str
    url: str
    width: int | None
    height: int | None


class CitationPageRegion(WireModel):
    id: int
    stable_id: str
    bbox: JsonValue
    snippet: str | None
    raw: JsonValue


class CitationViewerPage(WireModel):
    page: int
    image: CitationPageImage | None
    render_url: str | None = None
    text: str | None
    regions: list[CitationPageRegion]


class CitationTemporalRun(WireModel):
    index: int
    start_ms: int
    end_ms: int
    span_ids: list[str]
    clip_url: str | None


class CitationViewerSpan(WireModel):
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
    quote_truncated: bool = False
    snippet_truncated: bool = False
    text_layer_hash: str | None
    preview: JsonValue
    raw: JsonValue
    warnings: list[str]
    deep_link_url: str | None
    clip_url: str | None
    run_index: int | None


class CitationSourceCell(WireModel):
    sheet_id: int | None
    row_id: int | None
    column_id: int | None


class CitationTextRange(WireModel):
    span_id: str
    start: int
    end: int
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)


class CitationTranscriptSegment(WireModel):
    span_id: str
    start: int
    end: int
    start_ms: int
    end_ms: int
    speaker: str | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)


class CitationTranscriptContext(WireModel):
    schema_version: Literal["frisket.timestamped_text_context.v1"]
    evidence_link_stable_id: str
    artifact_stable_id: str
    offset_unit: Literal["utf16_code_unit"]
    segments: list[CitationTranscriptSegment]


class CitationTextContext(WireModel):
    text: str
    offset_unit: Literal["utf16_code_unit"]
    ranges: list[CitationTextRange]
    transcript: CitationTranscriptContext | None = None
    window: CitationTextWindow | None = None
    content_hash: str | None = None
    citation_id: str | None = None
    artifact_stable_id: str | None = None
    recovered: bool = False


class CitationViewerArtifact(WireModel):
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
    source_cell: CitationSourceCell | None
    external_ref: JsonValue
    artifact_ref: CitationArtifactReference
    metadata: JsonValue
    text_context: CitationTextContext | None = None
    text_context_status: Literal["available", "stale"] | None = None
    recovery_status: (
        Literal["located", "not_found", "unsupported", "incomplete"] | None
    ) = None
    recovery_scanned_characters: int | None = Field(default=None, ge=0)
    recovery_notice: str | None = None
    spans: list[CitationViewerSpan]
    pages: list[CitationViewerPage]
    runs: list[CitationTemporalRun]


class CitationViewerResponse(WireModel):
    schema_version: Literal["frisket.citation_viewer.v1"]
    link: CitationViewerLink
    artifacts: list[CitationViewerArtifact]
    warnings: list[str]


PROJECT_CITATIONS_ERROR_RESPONSES = {
    400: {"model": ActionError},
    409: {"model": HttpError},
    # Project lookup is detail-wrapped; per-cell/link/column misses are the
    # older bare ActionError envelope.  Both are observable public behavior.
    404: {"model": ActionError | HttpError},
}


__all__ = [
    "CellCitationsResponse",
    "CellTextAnnotationsResponse",
    "CitationArtifactReference",
    "CitationBlobReference",
    "CitationPageImage",
    "CitationPageRegion",
    "CitationProducer",
    "CitationSourceCell",
    "CitationSummary",
    "CitationTemporalRun",
    "CitationTextContext",
    "CitationTextRange",
    "CitationTextWindow",
    "CitationTranscriptContext",
    "CitationTranscriptSegment",
    "CitationViewerArtifact",
    "CitationViewerLink",
    "CitationViewerPage",
    "CitationViewerResponse",
    "CitationViewerSpan",
    "ColumnCitationsResponse",
    "ColumnCitationsRow",
    "PROJECT_CITATIONS_ERROR_RESPONSES",
    "RowSourceDocument",
    "RowSourceDocumentsResponse",
    "TextAnnotationCounts",
    "TextAnnotationLayer",
    "TextAnnotationGeneratedColumn",
    "TextAnnotationProducer",
    "TextAnnotationSpan",
    "TextAnnotationSpanMetadata",
    "TextAnnotationUnpositioned",
]
