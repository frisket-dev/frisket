"""Runtime-discovered tables from project-admitted importer contributions."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, StrictStr, field_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    DynamicTableResult,
    RuntimeImporter,
    RuntimeImportSource,
)
from frisket.contracts.actions.schemas._base import _is_json_value


class RuntimeImportParams(ActionParams):
    importer_kind: StrictStr = Field(min_length=1)
    source: RuntimeImportSource
    handler_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("handler_params")
    @classmethod
    def _bounded_handler_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not _is_json_value(value):
            raise ValueError("handler_params must contain finite JSON values")
        if (
            len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
            > 100_000
        ):
            raise ValueError("handler_params exceeds the 100000-byte limit")
        return value


def import_runtime(
    params: RuntimeImportParams, importer: RuntimeImporter
) -> DynamicTableResult:
    return importer.read(
        params.importer_kind, source=params.source, handler_params=params.handler_params
    )


RUNTIME = action(
    examples=(
        RuntimeImportParams(
            importer_kind="demo_ndjson",
            source=RuntimeImportSource(kind="runtime", label="Example records"),
            handler_params={"path": "examples/data.ndjson"},
        ),
    ),
    name="runtime",
    title="Import via trusted runtime binding",
    description="Read a project-admitted importer and create a table from its inspected schema.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_runtime),
)
