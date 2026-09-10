from __future__ import annotations

from contextlib import closing

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    DynamicTableResult,
    TableColumn,
    TableResult,
    TableRow,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project


class Params(ActionParams):
    declared: bool


def _columns(params: Params):
    if params.declared:
        return (
            TableColumn("value", "text", hidden=True),
            TableColumn("label", "text"),
        )
    return None


def _produce(params: Params) -> TableResult[DynamicOutput] | DynamicTableResult:
    rows = [TableRow(output=DynamicOutput({"value": "kept", "label": "visible"}))]
    if params.declared:
        return TableResult(rows=rows)
    return DynamicTableResult(
        schema=(
            TableColumn("value", "text", hidden=True),
            TableColumn("label", "text"),
        ),
        rows=rows,
    )


@pytest.mark.parametrize("declared", [False, True])
def test_hidden_display_column_renames_preserve_schema_and_request_identity(
    tmp_path, declared
):
    registered = RegisteredAction(
        "custom.hidden_table",
        action(
            name="hidden_table",
            title="Hidden table",
            description="Reporter-owned hidden columns",
            category=ActionCategory.CONVERT,
            run=create_sheet(_produce, columns_from=_columns),
        ),
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope={"kind": "project"},
        params={"declared": declared},
        sheet_name="Imported",
        idempotency_key="hidden-table",
        # Reusing a hidden column's original key is legal after that column moves.
        output_names={"value": "Private value", "label": "value"},
    )
    bound = BoundTypedActionRequest.bind(registered, request)
    changed = BoundTypedActionRequest.bind(
        registered,
        request.model_copy(
            update={
                "output_names": {"value": "Different private value", "label": "value"},
            }
        ),
    )
    assert typed_request_hash(bound) != typed_request_hash(changed)
    with closing(Project.create(tmp_path / "hidden.frisket")) as project:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = next(o.sheet_id for o in result.outputs if o.kind == "sheet")
        columns = project.columns(sheet, include_hidden=True)
        assert [(c["name"], bool(c["hidden"])) for c in columns] == [
            ("Private value", True),
            ("value", False),
        ]
        assert list(project.get_values(sheet, columns[0]["id"]).values()) == ["kept"]
        assert list(project.get_values(sheet, columns[1]["id"]).values()) == ["visible"]
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        conflict = run_typed_create_sheet_action(project, "p", changed)
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
