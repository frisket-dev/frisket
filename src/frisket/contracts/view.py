"""Shared saved/draft View selection shape; binding validation belongs to the store."""

from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


PositiveId = Annotated[int, Field(gt=0)]


class ViewSheetScope(_ViewModel):
    kind: Literal["sheet"]
    sheet_id: PositiveId
    row_ids: list[PositiveId] | None = None
    parent_row_id: PositiveId | None = None


class ViewProjectScope(_ViewModel):
    kind: Literal["project"]


class ViewFilter(_ViewModel):
    column_id: PositiveId
    condition: dict[str, JsonValue]


class ViewColumnOrder(_ViewModel):
    column_id: PositiveId
    dir: Literal["asc", "desc"] = "asc"


class ViewScoreOrder(_ViewModel):
    score: Literal["search", "similarity"]
    dir: Literal["asc", "desc"] = "desc"


class ViewSearch(_ViewModel):
    text: Annotated[str, Field(min_length=1)]


class ViewSelection(_ViewModel):
    scope: Annotated[ViewSheetScope | ViewProjectScope, Field(discriminator="kind")]
    filters: list[ViewFilter] = Field(default_factory=list)
    order: list[ViewColumnOrder | ViewScoreOrder] = Field(default_factory=list)
    limit: PositiveId | None = None
    search: ViewSearch | None = None
    similarity: dict[str, JsonValue] | None = None
    ranking: Literal["hybrid"] | None = None
    rank_constant: PositiveId | None = None
