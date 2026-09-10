"""Run selected JSON row values through the admitted local Python evaluator."""

from __future__ import annotations

from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.python_types import (
    PythonEvaluator,
    PythonOutputRoute,
    RoutedOutput,
)
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    DynamicOutput,
    Row,
    RowError,
    RowResult,
)


class PythonParams(ActionParams):
    input_columns: list[ColumnRef[Any]] = Field(min_length=1)
    code: str = Field(min_length=1)
    return_schema: dict[str, Any]
    output_routes: list[PythonOutputRoute] = Field(min_length=1)

    @field_validator("return_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("type"), str):
            raise ValueError("invalid_return_schema")
        return value

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [column.name for column in self.input_columns]
        if len(names) != len(set(names)):
            raise ValueError("invalid_input_ref")
        routes = [route.name for route in self.output_routes]
        if len(routes) != len(set(routes)):
            raise ValueError("duplicate_output_route")
        return self


def python_outputs(params: PythonParams) -> dict[str, RoutedOutput]:
    from frisket.authoring.json_schema_paths import named_result_schema_for_path

    return {
        route.name: RoutedOutput(
            route, named_result_schema_for_path(params.return_schema, route.path) or {}
        )
        for route in params.output_routes
    }


async def run_python(
    params: PythonParams, row: Row, evaluator: PythonEvaluator
) -> RowResult[DynamicOutput]:
    from frisket.authoring import column_types
    from frisket.engine.executor.action_support import (
        _extract_json_path,
        _json_schema_error,
    )

    result = await evaluator.evaluate(
        code=params.code,
        row={column.name: column.read(row) for column in params.input_columns},
    )
    error = _json_schema_error(result, params.return_schema)
    if error is not None:
        raise RowError("return_schema_mismatch", error)
    values = {}
    for route in params.output_routes:
        value, found = _extract_json_path(result, route.path)
        if not found:
            raise RowError(
                "output_route_missing",
                f"route {route.name!r} path {route.path!r} did not resolve",
            )
        if route.target.kind == "column" and not column_types.validate_value(
            route.target.type, value
        ):
            raise RowError(
                "return_schema_mismatch",
                f"route {route.name!r} value does not match target type {route.target.type!r}",
            )
        values[route.name] = value
    return RowResult(output=DynamicOutput(values))


PYTHON = action(
    name="python",
    title="Run trusted local Python",
    description="Run a trusted local Python transform over selected JSON row values, routing results to columns, named results, and receipt evidence.",
    category=ActionCategory.CONVERT,
    run=map_rows(run_python, dynamic_outputs=python_outputs),
    examples=(
        PythonParams(
            input_columns=["transcript"],
            code="result = {'excerpt': ' '.join(row['transcript'].split()[:25])}",
            return_schema={
                "type": "object",
                "properties": {"excerpt": {"type": "string"}},
            },
            output_routes=[
                {
                    "name": "excerpt",
                    "path": "$.excerpt",
                    "target": {"kind": "column", "type": "text"},
                }
            ],
        ),
    ),
)
