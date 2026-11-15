"""One saved View: selection, presentation, and a shared paginated resolver."""

from __future__ import annotations

from pydantic import ConfigDict, JsonValue, RootModel, field_validator
from frisket.contracts.http.models import WireModel
from frisket.contracts.view import ViewSelection


class _ClosedRequest(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class SavedView(WireModel):
    id: int
    name: str
    sheet_id: int | None
    spec: dict[str, JsonValue]
    op_id: int | None
    created_at: str
    updated_at: str
    unavailable_reason: str | None


class SavedViewDelete(WireModel):
    ok: bool
    deleted: int


class SavedViewList(RootModel[list[SavedView]]):
    model_config = ConfigDict(strict=True)


class ViewRowRef(WireModel):
    sheet_id: int
    row_id: int
    search_score: float | None
    similarity_score: float | None
    relevance_score: float | None


class SavedViewResolved(WireModel):
    view_id: int
    query_hash: str
    refs: list[ViewRowRef]
    total: int
    offset: int
    limit: int


class SavedViewDefinitionReplaceRequest(_ClosedRequest):
    selection: ViewSelection
    presentation: dict[str, JsonValue] | None = None


class SavedViewRenameRequest(_ClosedRequest):
    name: str

    @field_validator("name")
    @classmethod
    def _name_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value


class SavedViewCreateRequest(SavedViewDefinitionReplaceRequest, SavedViewRenameRequest):
    pass
