"""Typed column and row project mutations."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    ColumnCreator,
    ColumnPatcher,
    ColumnTyper,
    CreatedColumn,
    CreatedRow,
    DeletedRows,
    EditedCells,
    PatchedColumn,
    QueryCellEditor,
    QueryEditedCells,
    RetypedColumn,
    CellEditor,
    RowCreator,
    RowDeleter,
)
from frisket.contracts.actions.schemas._base import (
    MAX_CELL_EDITS,
    canonical_column_type,
)


COLUMN_DISPLAY_FORMATS = frozenset({"markdown", "filesize", "currency", "percent"})
_MAX_ROW_ADD_CELLS = 256
_MAX_ROW_DELETE_ROWS = 10_000


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


class ColumnPatchParams(ActionParams):
    column_id: int = Field(ge=1, strict=True)
    sheet_id: int | None = Field(default=None, ge=1, strict=True)
    format: str | None


class ColumnAddParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    name: str = Field(max_length=200)
    type: str = "text"
    position: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, name: str) -> str:
        return name.strip()

    @field_validator("type")
    @classmethod
    def _canonicalize_type(cls, type_name: str) -> str:
        return canonical_column_type(type_name)


class RowAddParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    cells: dict[str, Any | None] = Field(default_factory=dict)

    @field_validator("cells")
    @classmethod
    def _validate_cells(cls, cells: dict[str, Any | None]) -> dict[str, Any | None]:
        if len(cells) > _MAX_ROW_ADD_CELLS:
            raise ValueError("invalid_params")
        for name, value in cells.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid_row_ref")
            if not _is_json_value(value):
                raise ValueError("invalid_params")
        return cells


class RowDeleteParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    row_ids: list[Annotated[int, Field(strict=True)]]

    @field_validator("row_ids")
    @classmethod
    def _validate_row_ids(cls, row_ids: list[int]) -> list[int]:
        if len(row_ids) > _MAX_ROW_DELETE_ROWS:
            raise ValueError("invalid_params")
        if any(row_id < 1 for row_id in row_ids):
            raise ValueError("invalid_row_ref")
        return list(dict.fromkeys(row_ids))


class ColumnSetTypeParams(ActionParams):
    column_id: int = Field(ge=1, strict=True)
    sheet_id: int | None = Field(default=None, ge=1, strict=True)
    type: str = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _canonicalize_type(cls, type_name: str) -> str:
        return canonical_column_type(type_name)


class CellEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: int = Field(ge=1, strict=True)
    column_id: int = Field(ge=1, strict=True)
    value: Any | None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: Any | None) -> Any | None:
        if not _is_json_value(value):
            raise ValueError("invalid_params")
        return value


class CellEditParams(ActionParams):
    edits: list[CellEdit] = Field(min_length=1, max_length=MAX_CELL_EDITS)

    @model_validator(mode="after")
    def _unique_targets(self) -> CellEditParams:
        targets = [(edit.row_id, edit.column_id) for edit in self.edits]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate cell targets are not allowed")
        return self


class CellEditQueryParams(ActionParams):
    query: dict[str, Any]
    column_id: int = Field(ge=1, strict=True)
    value: Any | None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: Any | None) -> Any | None:
        if not _is_json_value(value):
            raise ValueError("invalid_params")
        return value


def patch_column(params: ColumnPatchParams, columns: ColumnPatcher) -> PatchedColumn:
    return columns.patch(
        params.column_id,
        sheet_id=params.sheet_id,
        format=params.format,
    )


def create_column(params: ColumnAddParams, columns: ColumnCreator) -> CreatedColumn:
    return columns.create(
        params.sheet_id,
        name=params.name,
        column_type=params.type,
        position=params.position,
    )


def create_row(params: RowAddParams, rows: RowCreator) -> CreatedRow:
    return rows.create(params.sheet_id, cells=params.cells)


def delete_rows(params: RowDeleteParams, rows: RowDeleter) -> DeletedRows:
    return rows.delete(params.sheet_id, row_ids=tuple(params.row_ids))


def set_column_type(params: ColumnSetTypeParams, columns: ColumnTyper) -> RetypedColumn:
    return columns.set_type(
        params.column_id, sheet_id=params.sheet_id, column_type=params.type
    )


def edit_cells(params: CellEditParams, cells: CellEditor) -> EditedCells:
    return cells.edit(
        tuple((edit.row_id, edit.column_id, edit.value) for edit in params.edits)
    )


def edit_query(params: CellEditQueryParams, cells: QueryCellEditor) -> QueryEditedCells:
    return cells.edit_query(
        query=params.query, column_id=params.column_id, value=params.value
    )


COLUMN_PATCH = action(
    examples=(ColumnPatchParams(column_id=1, sheet_id=1, format="currency"),),
    name="patch",
    title="Patch column presentation",
    description=(
        "Patch display format metadata for a visible column and record the "
        "transition as an undoable operation plus receipt."
    ),
    category=ActionCategory.CLEANUP,
    run=patch_column,
    form="column_patch",
)

COLUMN_ADD = action(
    examples=(ColumnAddParams(sheet_id=1, name="notes", type="text"),),
    name="add",
    title="Add column",
    description=(
        "Insert a new empty source column into a visible sheet as an undoable "
        "operation plus receipt evidence."
    ),
    category=ActionCategory.CLEANUP,
    run=create_column,
    form="column_add",
)

ROW_ADD = action(
    examples=(RowAddParams(sheet_id=1, cells={"title": "Example document"}),),
    name="add",
    title="Add row",
    description=(
        "Append a source row to a visible sheet as an undoable operation plus "
        "receipt evidence."
    ),
    category=ActionCategory.CLEANUP,
    run=create_row,
    form="row_add",
)

ROW_DELETE = action(
    examples=(RowDeleteParams(sheet_id=1, row_ids=[1, 2]),),
    name="delete",
    title="Delete rows",
    description=(
        "Hide selected visible rows as an undoable operation plus receipt evidence."
    ),
    category=ActionCategory.CLEANUP,
    run=delete_rows,
    form="row_delete",
)

COLUMN_SET_TYPE = action(
    examples=(ColumnSetTypeParams(column_id=1, sheet_id=1, type="number"),),
    name="set_type",
    title="Set column type",
    description=(
        "Retype a visible column after validating its current values and record an "
        "undoable operation plus receipt."
    ),
    category=ActionCategory.CLEANUP,
    run=set_column_type,
    form="column_set_type",
)

CELL_EDIT = action(
    examples=(
        CellEditParams(edits=[CellEdit(row_id=1, column_id=1, value="Corrected")]),
    ),
    name="edit",
    title="Edit cells",
    description="Record manual edit overlays for visible cells.",
    category=ActionCategory.CLEANUP,
    run=edit_cells,
    form="cell_edit",
)

CELL_EDIT_QUERY = action(
    examples=(
        CellEditQueryParams(
            query={
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": 1},
                "filter": {"status": {"eq": "open"}},
            },
            column_id=1,
            value="Reviewed",
        ),
    ),
    name="edit_query",
    title="Edit cells selected by query",
    description="Resolve a sheet filter and record manual edits for its exact rows.",
    category=ActionCategory.CLEANUP,
    run=edit_query,
    form="cell_edit_query",
)
