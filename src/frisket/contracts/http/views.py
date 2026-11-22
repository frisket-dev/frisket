"""One View: selection, presentation, and a shared paginated resolver."""

from __future__ import annotations

from pydantic import ConfigDict, JsonValue, RootModel
from frisket.contracts.http.models import WireModel
from frisket.contracts.view import ViewSelection
from typing import Annotated
from frisket.contracts.actions.schemas._validators import nonblank


class _ClosedRequest(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class View(WireModel):
    id: int
    name: str
    sheet_id: int | None
    spec: dict[str, JsonValue]
    change_id: int | None
    created_at: str
    updated_at: str
    unavailable_reason: str | None


class ViewDelete(WireModel):
    ok: bool
    deleted: int


class ViewList(RootModel[list[View]]):
    model_config = ConfigDict(strict=True)


class ViewRowRef(WireModel):
    sheet_id: int
    row_id: int
    search_score: float | None
    similarity_score: float | None
    relevance_score: float | None


class ViewResolved(WireModel):
    view_id: int
    query_hash: str
    refs: list[ViewRowRef]
    total: int
    offset: int
    limit: int


class ViewDefinitionReplaceRequest(_ClosedRequest):
    selection: ViewSelection
    presentation: dict[str, JsonValue] | None = None


class ViewRenameRequest(_ClosedRequest):
    name: Annotated[str, nonblank("name is required")]


class ViewCreateRequest(ViewDefinitionReplaceRequest, ViewRenameRequest):
    pass
