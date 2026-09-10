from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    Row,
    RowResult,
    StagedFile,
)
from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram


class Params(ActionParams):
    pass


class StaticOutput(BaseModel):
    payload: dict[str, Any]


@dataclass
class WrappedFile:
    file: Any


def static_row(params: Params, row: Row) -> RowResult[StaticOutput]:
    raise AssertionError("this probe exercises publication, not handler execution")


def dynamic_row(params: Params, row: Row) -> RowResult[DynamicOutput]:
    raise AssertionError("this probe exercises publication, not handler execution")


@pytest.mark.parametrize("schema_kind", ["static", "dynamic"])
@pytest.mark.parametrize(
    "container", ["list", "mapping", "set", "frozenset", "dataclass"]
)
def test_active_json_row_output_rejects_nested_staged_file(schema_kind, container):
    terminal = (
        map_rows(static_row)
        if schema_kind == "static"
        else map_rows(dynamic_row, dynamic_outputs=lambda params: {"payload": Any})
    )
    registered = ActionRegistry(
        (
            ActionNamespace(
                "test",
                actions=(
                    action(
                        name="opaque_json",
                        title="Opaque JSON probe",
                        description="JSON cells cannot publish table-only file handles.",
                        category=ActionCategory.CONVERT,
                        run=terminal,
                    ),
                ),
            ),
        )
    ).get("test.opaque_json")
    params = Params()
    fields = terminal.resolve_output_fields(params)
    program = _TypedMapRowsProgram(
        registered,
        params,
        {"payload": "Stored payload"},
        fields,
        ({"name": "Stored payload", "column_type": "json"},),
    )
    file = StagedFile(size=7)
    nested = {
        "list": [file],
        "mapping": {"file": file},
        "set": {file},
        "frozenset": frozenset({file}),
        "dataclass": WrappedFile(file),
    }[container]
    output = (
        StaticOutput(payload={"nested": nested})
        if schema_kind == "static"
        else DynamicOutput({"payload": nested})
    )

    # Reject the semantic object before Pydantic can erase its identity into
    # ordinary JSON such as {"size": 7}; no blob is published by a row action.
    with pytest.raises((TypeError, ValueError), match="(?i)(staged|opaque)"):
        program._prepare_publication(RowResult(output=output))
