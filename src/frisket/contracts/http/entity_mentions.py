"""Compatibility contracts for the entity-mention read routes.

The preview service owns validation for object payloads so its bare v1 action
errors remain stable. Requests therefore preserve raw values and ignore
extensions, while FastAPI continues to reject non-object JSON with 422.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, JsonValue

from frisket.contracts.http.models import CoerciveRequest, WireModel


class _CompatibleRequest(CoerciveRequest):
    pass


class EntityMentionsPreviewRequest(_CompatibleRequest):
    sheet_id: Any = None
    column_id: Any = None
    search: Any = None
    type: Any = None
    limit: Any = None
    offset: Any = None


class EntityMentionDocumentsRequest(_CompatibleRequest):
    sheet_id: Any = None
    column_id: Any = None
    type: Any = None
    fingerprint: Any = None
    text: Any = None
    limit: Any = None
    offset: Any = None


class EntityMentionOccurrencesRequest(_CompatibleRequest):
    sheet_id: Any = None
    row_id: Any = None
    column_id: Any = None
    type: Any = None
    fingerprint: Any = None
    text: Any = None
    limit: Any = None
    offset: Any = None
    snippet_radius: Any = None


class _OpenEntityMentionResponse(WireModel):
    """Strict mention core with lossless producer-owned JSON additions."""

    model_config = ConfigDict(extra="allow", strict=True)

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class EntityMentionColumn(_OpenEntityMentionResponse):
    id: int
    name: str
    semantic_type: str | None


class EntityMentionFingerprintSelector(_OpenEntityMentionResponse):
    kind: Literal["fingerprint"]
    fingerprint: str


class EntityMentionTextSelector(_OpenEntityMentionResponse):
    kind: Literal["text"]
    text: str


EntityMentionSelector = EntityMentionFingerprintSelector | EntityMentionTextSelector


class EntityMentionsCoverage(_OpenEntityMentionResponse):
    target_rows: int | None
    completed_rows: int | None
    failed_rows: int | None
    sheet_rows: int | None
    scope_kind: Literal["all_rows", "exact_membership"] | None


class EntityMentionSurface(_OpenEntityMentionResponse):
    text: str
    row_count: int
    mention_count: int


class EntityMentionGroup(_OpenEntityMentionResponse):
    type: str
    selector: EntityMentionSelector
    label: str
    row_count: int
    mention_count: int
    surface_count: int
    surfaces: list[EntityMentionSurface]


class EntityMentionTypeTotal(_OpenEntityMentionResponse):
    type: str
    total_groups: int


class EntityMentionsPreviewResponse(_OpenEntityMentionResponse):
    """Required mention inventory core plus additive producer JSON fields."""

    schema_version: Literal["entity-mentions-preview.v2"]
    sheet_id: int
    column: EntityMentionColumn
    coverage: EntityMentionsCoverage
    search: str | None
    type: str | None
    total_groups: int
    type_totals: list[EntityMentionTypeTotal]
    limit: int
    offset: int
    items: list[EntityMentionGroup]


class EntityMentionTotals(_OpenEntityMentionResponse):
    documents: int
    mentions: int


class EntityMentionDocument(_OpenEntityMentionResponse):
    row_id: int
    title: str | None
    occurrence_count: int


class EntityMentionDocumentsResponse(_OpenEntityMentionResponse):
    """Required document page core plus additive producer JSON fields."""

    schema_version: Literal["entity-mention-detail.v1"]
    sheet_id: int
    column: EntityMentionColumn
    type: str
    selector: EntityMentionSelector
    totals: EntityMentionTotals
    limit: int
    offset: int
    documents: list[EntityMentionDocument]
    next_offset: int | None


class EntityMentionOccurrencesTotals(_OpenEntityMentionResponse):
    occurrences: int


class EntityMentionTextColumn(_OpenEntityMentionResponse):
    id: int
    name: str | None


class EntityMentionSnippet(_OpenEntityMentionResponse):
    text: str
    mark_start: int
    mark_end: int
    truncated_start: bool
    truncated_end: bool


class EntityMentionOccurrence(_OpenEntityMentionResponse):
    occurrence_id: str
    start: int
    end: int
    quote: str
    snippet: EntityMentionSnippet


class EntityMentionUnpositioned(_OpenEntityMentionResponse):
    reason: Literal[
        "content_hash_mismatch", "unsupported_offset_unit", "invalid_geometry"
    ]
    total: int


class EntityMentionOccurrencesResponse(_OpenEntityMentionResponse):
    """Required occurrence page core plus additive producer JSON fields."""

    schema_version: Literal["entity-mention-occurrences.v1"]
    sheet_id: int
    row_id: int
    column: EntityMentionColumn
    type: str
    selector: EntityMentionSelector
    snippet_radius: int
    limit: int
    offset: int
    text_column: EntityMentionTextColumn | None
    totals: EntityMentionOccurrencesTotals
    occurrences: list[EntityMentionOccurrence]
    next_offset: int | None
    unpositioned: EntityMentionUnpositioned | None


__all__ = [
    "EntityMentionDocumentsRequest",
    "EntityMentionDocumentsResponse",
    "EntityMentionOccurrencesRequest",
    "EntityMentionOccurrencesResponse",
    "EntityMentionsPreviewRequest",
    "EntityMentionsPreviewResponse",
]
