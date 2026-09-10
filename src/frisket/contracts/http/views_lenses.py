"""HTTP contracts for the saved-view and saved-lens browser routes.

A saved view row and a saved lens row are the same closed seven-field shape:
the ``views`` and ``lenses`` tables are column-identical
(``frisket.engine.store.schema``) and both services return ``dict(row)`` with
the JSON ``spec`` decoded in place. The shape is declared once here and named
twice, because the two wire owners stay separately addressable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, JsonValue, RootModel, field_validator

from frisket.contracts.http.models import NamedCoerciveRequest, WireModel


# The literal ``schema_version`` that frisket.preview.query.query_preview_payload
# emits (QUERY_PREVIEW_SCHEMA_VERSION). Restated here because the contracts
# package stays out of the preview/query runtime layer; the backend contract
# test pins this value against the producer's own constant.
LENS_RESOLVE_SCHEMA_VERSION = "frisket.query_preview.v1"


class _CompatibleRequest(NamedCoerciveRequest):
    """Keep the existing operational request coercion at this boundary."""


class _ClosedRequest(WireModel):
    """The greenfield Saved View mutation boundary is closed and strict."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class _SavedRow(WireModel):
    """The stored saved-view/saved-lens row, exactly as both services emit it."""

    id: int
    name: str
    spec: dict[str, JsonValue]
    op_id: int | None
    created_at: str
    updated_at: str


class _SavedRowDelete(WireModel):
    """The shared delete acknowledgement: the deleted row's own id."""

    ok: bool
    deleted: int


class SavedView(_SavedRow):
    sheet_id: int


class SavedLens(_SavedRow):
    sheet_id: int | None


class SavedViewDelete(_SavedRowDelete):
    pass


class SavedLensDelete(_SavedRowDelete):
    pass


class SavedViewList(RootModel[list[SavedView]]):
    model_config = ConfigDict(strict=True)


class SavedLensList(RootModel[list[SavedLens]]):
    model_config = ConfigDict(strict=True)


class SavedLensResolveEvaluator(WireModel):
    kind: str
    version: str


class SavedLensResolveScore(WireModel):
    # Ranked previews (embedding_similarity / embedding_hybrid) carry both;
    # a fused hybrid rank has no single cosine distance, so distance is null.
    distance: float | None
    score: float | None


class SavedLensResolved(WireModel):
    """The literal frisket.query_preview.v1 payload with ``lens_id`` prepended."""

    lens_id: int
    schema_version: Literal[LENS_RESOLVE_SCHEMA_VERSION]
    query: dict[str, JsonValue]
    query_hash: str
    sheet_id: int
    row_ids: list[int]
    row_count: int
    total: int
    offset: int
    limit: int
    evaluator: SavedLensResolveEvaluator
    # Keyed by row id as a string, exactly as query_preview_payload emits it.
    scores: dict[str, SavedLensResolveScore]


class SavedViewCreateRequest(_ClosedRequest):
    """A saved view: a named, reusable filter/sort over a sheet."""

    name: str
    sheet_id: int
    filter: dict[str, JsonValue]
    sort: list[JsonValue] | None = None
    columns: list[JsonValue] | None = None
    column_groups: list[JsonValue] | None = None

    @field_validator("name")
    @classmethod
    def _name_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value


class SavedViewRenameRequest(_ClosedRequest):
    name: str

    @field_validator("name")
    @classmethod
    def _name_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value


class SavedViewDefinitionReplaceRequest(_ClosedRequest):
    """The complete, explicit replacement of presentation definition only."""

    filter: dict[str, JsonValue]
    sort: list[JsonValue] | None
    columns: list[JsonValue] | None
    column_groups: list[JsonValue] | None


class SavedLensCreateRequest(_CompatibleRequest):
    """A saved lens: a name plus a QuerySpec and presentation metadata."""

    name: str
    query: dict[str, JsonValue]
    presentation: dict[str, JsonValue] | None = None


class SavedLensPatchRequest(_CompatibleRequest):
    name: str | None = None
    query: dict[str, JsonValue] | None = None
    presentation: dict[str, JsonValue] | None = None


__all__ = [
    "LENS_RESOLVE_SCHEMA_VERSION",
    "SavedLens",
    "SavedLensCreateRequest",
    "SavedLensDelete",
    "SavedLensList",
    "SavedLensPatchRequest",
    "SavedLensResolveEvaluator",
    "SavedLensResolveScore",
    "SavedLensResolved",
    "SavedView",
    "SavedViewCreateRequest",
    "SavedViewDelete",
    "SavedViewList",
    "SavedViewDefinitionReplaceRequest",
    "SavedViewRenameRequest",
]
