"""Expand admitted list items into a declared or discovered child table."""

from __future__ import annotations

from typing import Any, Iterator

from pydantic import Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    DynamicTableResult,
    ListItem,
    ListTableReader,
    ListTableSource,
    NamedListSource,
    TableColumn,
    TableError,
    TableResult,
    TableRow,
)
from frisket.authoring import column_types
from frisket.contracts.actions.schemas._base import (
    MAX_IMPORT_ROWS_COLUMNS,
    canonical_column_type,
)


class ListProjectionColumn(ActionParams):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    type: str = Field(min_length=1)
    hidden: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("column name must be nonempty and trimmed")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if value != "$" and not value.startswith("$."):
            raise ValueError("invalid_item_schema")
        return value

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        canonical = canonical_column_type(value)
        if column_types.get_column_type(canonical) is None:
            raise ValueError("invalid_column_type")
        return canonical


class ListTableParams(ActionParams):
    source: ListTableSource
    item_schema: dict[str, Any] | None = None
    columns: list[ListProjectionColumn] | None = Field(
        default=None, max_length=MAX_IMPORT_ROWS_COLUMNS
    )

    @model_validator(mode="after")
    def _source_projection(self) -> ListTableParams:
        if isinstance(self.source, NamedListSource):
            if self.item_schema is None or not isinstance(
                self.item_schema.get("type"), str
            ):
                raise ValueError("invalid_item_schema")
            if not self.columns:
                raise ValueError("invalid_item_schema")
            names = [column.name for column in self.columns]
            if len(names) != len(set(names)):
                raise ValueError("duplicate_column_name")
        elif self.item_schema is not None or self.columns is not None:
            raise ValueError("live list columns discover their output schema")
        return self


def declared_columns(params: ListTableParams) -> tuple[TableColumn, ...] | None:
    if params.columns is None:
        return None
    return tuple(
        TableColumn(key=column.name, type=column.type, hidden=column.hidden)
        for column in params.columns
    )


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "text"
    return "json"


def _merge_type(current: str | None, incoming: str) -> str:
    if current is None or current == incoming:
        return incoming
    return "number" if {current, incoming} <= {"integer", "number"} else "json"


def _is_file_envelope(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"blob", "mime", "filename"}:
        return False
    blob = value["blob"]
    return (
        isinstance(blob, str)
        and len(blob) == 64
        and all(char in "0123456789abcdef" for char in blob)
        and isinstance(value["mime"], str)
        and bool(value["mime"])
        and isinstance(value["filename"], str)
        and bool(value["filename"])
    )


def infer_projection(
    items: tuple[ListItem, ...], include_columns: list[str] | None
) -> tuple[ListProjectionColumn, ...]:
    if not items:
        raise TableError("invalid_input_ref", "Source column has no list items")
    key_types: dict[str, str | None] = {}
    has_object = any(isinstance(item.value, dict) for item in items)
    if has_object and any(not isinstance(item.value, dict) for item in items):
        raise TableError(
            "invalid_item_schema", "List items must be uniformly objects or scalars"
        )
    if has_object:
        for item in items:
            for key, value in item.value.items():
                key_types.setdefault(key, None)
                if value is not None:
                    key_types[key] = _merge_type(key_types[key], _infer_type(value))
        if include_columns is None and all(
            _is_file_envelope(item.value) for item in items
        ):
            return (ListProjectionColumn(name="attachment", path="$", type="file"),)
        if include_columns is not None:
            unknown = [key for key in include_columns if key not in key_types]
            if unknown:
                raise TableError(
                    "invalid_item_schema",
                    "include_columns names keys absent from the list-item union",
                    details={"unknown": unknown, "available": list(key_types)},
                )
        keys = list(key_types) if include_columns is None else include_columns
        columns = tuple(
            ListProjectionColumn(
                name=key, path=f"$.{key}", type=key_types[key] or "text"
            )
            for key in keys
        )
    else:
        if include_columns is not None:
            raise TableError(
                "invalid_item_schema", "include_columns requires object list items"
            )
        scalar_type = None
        for item in items:
            if item.value is not None:
                scalar_type = _merge_type(scalar_type, _infer_type(item.value))
        columns = (
            ListProjectionColumn(name="value", path="$", type=scalar_type or "text"),
        )
    if not columns or len(columns) > MAX_IMPORT_ROWS_COLUMNS:
        raise TableError(
            "invalid_item_schema",
            "List items must define a nonempty supported set of columns",
            details={"max_columns": MAX_IMPORT_ROWS_COLUMNS, "found": len(columns)},
        )
    return columns


def project_items(
    items: tuple[ListItem, ...],
    columns: list[ListProjectionColumn] | tuple[ListProjectionColumn, ...],
    *,
    require_paths: bool,
) -> Iterator[TableRow[DynamicOutput]]:
    # Share the established path semantics without importing the executor while
    # action registration is still being initialized.
    from frisket.engine.executor.action_support import _extract_json_path

    for item in items:
        values = {}
        for column in columns:
            value, found = _extract_json_path(item.value, column.path)
            if not found and require_paths:
                raise TableError(
                    "invalid_item_schema",
                    "Declared column path did not resolve",
                    details={"column": column.name, "path": column.path},
                )
            value = value if found else None
            if not column_types.validate_value(column.type, value):
                raise TableError(
                    "invalid_item_schema",
                    "Projected value does not match the declared column type",
                    details={"column": column.name, "type": column.type},
                )
            values[column.name] = value
        yield TableRow(
            output=DynamicOutput(values), sources=(item.source,), parent=item.source
        )


def expand_list_table(
    params: ListTableParams, tables: ListTableReader
) -> TableResult[DynamicOutput] | DynamicTableResult:
    items = tables.read(params.source, item_schema=params.item_schema)
    if params.columns is not None:
        return TableResult(
            rows=project_items(items, params.columns, require_paths=True)
        )
    projection = infer_projection(items, params.source.include_columns)
    return DynamicTableResult(
        schema=tuple(
            TableColumn(key=column.name, type=column.type, hidden=column.hidden)
            for column in projection
        ),
        rows=project_items(items, projection, require_paths=False),
    )


TABLE_FROM_LIST = action(
    examples=(
        ListTableParams(source={"kind": "column", "sheet_id": 1, "column_id": 1}),
        ListTableParams(
            source=NamedListSource(
                kind="named_result",
                sheet_id=1,
                column_id=1,
                run_id=1,
                route="items",
                schema="items.v1",
            ),
            item_schema={"type": "object", "properties": {"name": {"type": "string"}}},
            columns=[ListProjectionColumn(name="name", path="$.name", type="text")],
        ),
    ),
    name="table_from_list",
    title="Table from list",
    description="Expand existing list cells or a named result into a child table.",
    category=ActionCategory.CONVERT,
    run=create_sheet(expand_list_table, columns_from=declared_columns),
)
