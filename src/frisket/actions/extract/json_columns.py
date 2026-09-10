from __future__ import annotations

from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, DynamicOutput, Row, RowResult
from frisket.output_names import (
    validate_runner_output_family,
    validate_runner_output_name,
)


class JsonRoute(ActionParams):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)

    @field_validator("name", "path")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must be non-empty")
        return value

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        try:
            return validate_runner_output_name(value)
        except ValueError as error:
            raise ValueError("output name conflicts with row metadata") from error


class ColumnsFromJsonParams(ActionParams):
    source_column: ColumnRef[dict[str, Any] | list[Any]]
    routes: list[JsonRoute] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_routes(self) -> Self:
        try:
            validate_runner_output_family(route.name for route in self.routes)
        except ValueError as error:
            raise ValueError("route output names must be unique") from error
        return self


def _json_outputs(params: ColumnsFromJsonParams) -> dict[str, Any]:
    return {route.name: Any for route in params.routes}


def columns_from_json(
    params: ColumnsFromJsonParams,
    row: Row,
) -> RowResult[DynamicOutput]:
    # Imported at execution time to keep action registration independent from
    # the executor package initializer while sharing its JSON-path authority.
    from frisket.engine.executor.action_support import _extract_json_path

    source = params.source_column.read(row)
    if not isinstance(source, (dict, list)):
        values = {route.name: None for route in params.routes}
    else:
        values: dict[str, Any] = {}
        for route in params.routes:
            try:
                value, found = _extract_json_path(source, route.path)
            except Exception:  # noqa: BLE001 - one malformed row remains isolated
                value, found = None, False
            values[route.name] = value if found else None
    return RowResult(output=DynamicOutput(values))


COLUMNS_FROM_JSON = action(
    examples=(
        ColumnsFromJsonParams(
            source_column="json_source", routes=[JsonRoute(name="city", path="$.city")]
        ),
    ),
    name="columns_from_json",
    title="Extract columns from JSON",
    description="Extract dotted or indexed JSON paths into columns.",
    category=ActionCategory.EXTRACT,
    run=map_rows(columns_from_json, dynamic_outputs=_json_outputs),
)
