"""Typed HTTP transport for the in-memory action-preview lifecycle.

The preview action is intentionally an open JSON object: its action-kind
resolver owns validation, so the HTTP boundary must pass it through without
turning an action-specific refusal into a transport 422.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


ACTION_PREVIEW_SCHEMA_VERSION = "frisket.action_preview.v1"


class ActionPreviewRunRequest(RootModel[dict[str, JsonValue]]):
    """Tolerant, producer-owned v1 action envelope."""

    model_config = ConfigDict(strict=True)


class ActionPreviewError(WireModel):
    schema_version: Literal["frisket.action_error.v1"]
    code: str
    message: str
    action_kind: str | None
    field: str | None
    details: dict[str, JsonValue]
    needs_confirmation: bool


class ActionPreviewErrorResponse(WireModel):
    schema_version: Literal["frisket.action_preview.v1"]
    error: ActionPreviewError


class ActionPreviewStartResponse(WireModel):
    schema_version: Literal["frisket.action_preview.v1"]
    preview_id: str
    total: int | None


class ActionPreviewProgress(WireModel):
    done: int
    total: int | None


class ActionPreviewJobError(WireModel):
    """Terminal async-job error, deliberately smaller than an admission error."""

    code: str
    message: str


class ActionPreviewColumn(WireModel):
    name: str
    column_type: str
    format: str | None
    hidden: bool
    overwrites_column_id: int | None


class ActionPreviewRowOverlay(WireModel):
    kind: Literal["row_overlay"]
    sheet_id: int
    columns: list[ActionPreviewColumn]
    rows: dict[str, dict[str, dict[str, JsonValue]]]
    row_ids: list[int]
    sampled: int
    total: int | None


class ActionPreviewTable(WireModel):
    kind: Literal["table"]
    columns: list[ActionPreviewColumn]
    rows: list[dict[str, dict[str, JsonValue]]]
    sampled: int
    total: int | None
    warnings: list[str] = Field(default_factory=list)


ActionPreviewResult = Annotated[
    ActionPreviewRowOverlay | ActionPreviewTable, Field(discriminator="kind")
]


class ActionPreviewAccounting(WireModel):
    receipt_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    model_call_count: int
    cost_actual: float | None
    elapsed_ms: int


class ActionPreviewStatusResponse(WireModel):
    schema_version: Literal["frisket.action_preview.v1"]
    preview_id: str
    status: Literal["running", "done", "error", "cancelled"]
    progress: ActionPreviewProgress
    result: ActionPreviewResult | None = None
    error: ActionPreviewJobError | None = None
    accounting: ActionPreviewAccounting | None = None


ACTION_PREVIEW_ERROR_RESPONSES = {
    status: {"model": ActionPreviewErrorResponse} for status in (400, 402, 404)
}


__all__ = [
    "ACTION_PREVIEW_ERROR_RESPONSES",
    "ACTION_PREVIEW_SCHEMA_VERSION",
    "ActionPreviewColumn",
    "ActionPreviewError",
    "ActionPreviewErrorResponse",
    "ActionPreviewJobError",
    "ActionPreviewProgress",
    "ActionPreviewResult",
    "ActionPreviewRunRequest",
    "ActionPreviewStartResponse",
    "ActionPreviewStatusResponse",
]
