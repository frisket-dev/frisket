"""Saved action settings, separate from execution and consent authority."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.action_compare import ActionDraftScope
from frisket.contracts.http.models import QueryWireModel, WireModel


class ActionHistoryProjectScope(WireModel):
    kind: Literal["project"]


class ActionHistoryDraft(WireModel):
    action_id: str = Field(min_length=1)
    scope: Annotated[
        ActionDraftScope | ActionHistoryProjectScope, Field(discriminator="kind")
    ]
    params: dict[str, JsonValue]
    output_names: dict[str, str] = Field(default_factory=dict)
    sheet_name: str | None = None


class ActionHistoryQuery(QueryWireModel):
    action_id: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class ActionHistoryItem(WireModel):
    id: str
    action_id: str
    sheet_id: int | None
    sheet_name: str | None
    submitted_at: str
    status: Literal[
        "not_started",
        "queued",
        "running",
        "completed",
        "partial",
        "failed",
        "cancelled",
    ]
    run_id: int | None


class ActionHistoryPage(WireModel):
    items: list[ActionHistoryItem]
    total: int
    offset: int
    limit: int


class ActionHistoryDetail(WireModel):
    item: ActionHistoryItem
    draft: ActionHistoryDraft | None
    unavailable_reason: str | None
