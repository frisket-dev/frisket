"""HTTP contracts for the embedding metadata/preview routes.

Existing-behavior migration: these DTOs freeze the wire truth the embedding
route service already emits (``server/services/embeddings.py`` and
``server/embedding_catalog.py``). Field order matches producer construction
order so a validated response serializes to the same bytes the bare dicts
did. Open leaves stay open by design: similarity/hybrid ``values`` maps carry
current row cell values of whatever columns the sheet has, ``pricing``/
``privacy``/``provider_policy``/``maintenance`` are producer-owned policy objects
(``_safe_obj`` keeps corrupt rows renderable). Export ``artifacts`` are closed
receipt artifact refs emitted by the exporter. The export download and manifest
routes are raw (binary artifact + integrity surface) and have no DTOs here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.models import CoerciveRequest, WireModel


# Producer schema literals (restated: the contracts package stays out of the
# service layer; the backend contract test pins them against the producers).
EMBEDDING_PROVIDER_CATALOG_SCHEMA_VERSION = "frisket.embedding_provider_catalog.v1"
EMBEDDING_INDEX_LIST_SCHEMA_VERSION = "frisket.embedding_index_list.v1"
EMBEDDING_INDEX_EXPORT_RESULT_SCHEMA_VERSION = (
    "frisket.embedding_index_export_result.v1"
)
EMBEDDING_SIMILARITY_PREVIEW_SCHEMA_VERSION = "frisket.embedding_similarity_preview.v1"
EMBEDDING_HYBRID_PREVIEW_SCHEMA_VERSION = "frisket.embedding_hybrid_preview.v1"


class _CompatibleRequest(CoerciveRequest):
    """Keep the existing operational request coercion at this boundary."""


class EmbeddingSimilarityPreviewRequest(_CompatibleRequest):
    """Envelope for both similarity and hybrid previews; the query object is
    owned by the similarity resolver, whose typed refusals must not become
    transport 422s."""

    query: dict[str, JsonValue]


class EmbeddingIndexExportRequest(_CompatibleRequest):
    formats: list[str] = Field(default_factory=lambda: ["jsonl"])
    include_vectors: bool = True


class EmbeddingProviderOption(WireModel):
    """An ``EmbeddingCapability`` plus the picker's disabled/compatibility
    annotations."""

    provider_id: str
    provider_kind: str
    model_id: str
    label: str
    modalities: list[str]
    dimensions: list[int] | None
    distance_metrics: list[str]
    local: bool
    available: bool
    error: str | None
    pricing: dict[str, JsonValue] | None
    privacy: dict[str, JsonValue]
    size_gb: float | None
    max_input_tokens: int | None
    recommended: bool
    dimension_discovery_required: bool
    disabled_reason: str | None
    modality_compatible: bool


class EmbeddingProviderCatalogResponse(WireModel):
    schema_version: Literal["frisket.embedding_provider_catalog.v1"]
    modality: str | None
    source_column_type: str | None
    providers: list[EmbeddingProviderOption]


class EmbeddingIndexFreshness(WireModel):
    """First-class freshness state; ``reason`` stays an open string owned by
    ``frisket.ai.embeddings.freshness``."""

    reason: str
    refresh_needed: bool
    ready: int
    current: int
    missing: int
    stale: int
    error: int
    total: int
    scope_resolved: bool
    # Mirrors the stored maintenance-policy leaf verbatim; ``_safe_obj``
    # guarantees corrupt/legacy rows still render, so a wrong-typed value
    # (e.g. a numeric mode) must pass through, never 500 the list.
    maintenance_mode: JsonValue
    last_refreshed_at: str | None
    last_refresh_job_id: str | None
    last_refresh_receipt_id: str | None
    pending_refresh_job_id: int | None


class EmbeddingIndexSummary(WireModel):
    freshness: EmbeddingIndexFreshness
    index_id: str
    name: str
    sheet_id: int | None
    space_id: str
    modality: str | None
    provider_id: str | None
    provider_kind: str | None
    model_id: str | None
    dimension: int | None
    distance_metric: str | None
    source_columns: list[str]
    status: str
    total_items: int
    ready_items: int
    stale_items: int
    stale_source_items: int
    missing_source_items: int
    error_items: int
    refresh_needed: bool
    last_refreshed_at: str | None
    remote: bool
    provider_policy: dict[str, JsonValue]
    # The stored maintenance-policy object as ``_safe_obj`` returns it: any
    # well-formed object — including a wrong-typed legacy/corrupt leaf such
    # as ``{"mode": 123}`` — renders byte-for-byte rather than 500ing.
    maintenance: dict[str, JsonValue]


class EmbeddingIndexListResponse(WireModel):
    schema_version: Literal["frisket.embedding_index_list.v1"]
    sheet_id: int | None
    indexes: list[EmbeddingIndexSummary]


class EmbeddingIndexExportArtifact(WireModel):
    """A durable embedding-index export receipt artifact.

    Field order deliberately matches the existing export writer's ref shape.
    """

    kind: Literal["export_artifact"]
    export_kind: Literal["embedding_index_export"]
    format: Literal["jsonl", "parquet", "arrow"]
    path: str = Field(min_length=1)
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    row_count: int = Field(ge=0)


class EmbeddingIndexExportResponse(WireModel):
    schema_version: Literal["frisket.embedding_index_export_result.v1"]
    index_id: str
    receipt_id: str | None
    artifacts: list[EmbeddingIndexExportArtifact]


class EmbeddingSimilarityHit(WireModel):
    row_id: int
    sheet_id: int | None
    distance: float
    score: float | None
    values: dict[str, JsonValue]


class EmbeddingSimilarityPreviewResponse(WireModel):
    schema_version: Literal["frisket.embedding_similarity_preview.v1"]
    index_id: str
    space_id: str
    sheet_id: int | None
    distance_metric: str
    anchor: dict[str, JsonValue]
    limit: int
    hits: list[EmbeddingSimilarityHit]


class EmbeddingHybridHit(WireModel):
    row_id: int
    sheet_id: int
    distance: float | None
    score: float
    vector_rank: int | None
    keyword_rank: int | None
    values: dict[str, JsonValue]


class EmbeddingHybridPreviewResponse(WireModel):
    schema_version: Literal["frisket.embedding_hybrid_preview.v1"]
    index_id: str
    sheet_id: int
    distance_metric: str
    hits: list[EmbeddingHybridHit]


__all__ = [
    "EMBEDDING_HYBRID_PREVIEW_SCHEMA_VERSION",
    "EMBEDDING_INDEX_EXPORT_RESULT_SCHEMA_VERSION",
    "EMBEDDING_INDEX_LIST_SCHEMA_VERSION",
    "EMBEDDING_PROVIDER_CATALOG_SCHEMA_VERSION",
    "EMBEDDING_SIMILARITY_PREVIEW_SCHEMA_VERSION",
    "EmbeddingHybridHit",
    "EmbeddingHybridPreviewResponse",
    "EmbeddingIndexExportRequest",
    "EmbeddingIndexExportArtifact",
    "EmbeddingIndexExportResponse",
    "EmbeddingIndexFreshness",
    "EmbeddingIndexListResponse",
    "EmbeddingIndexSummary",
    "EmbeddingProviderCatalogResponse",
    "EmbeddingProviderOption",
    "EmbeddingSimilarityHit",
    "EmbeddingSimilarityPreviewRequest",
    "EmbeddingSimilarityPreviewResponse",
]
