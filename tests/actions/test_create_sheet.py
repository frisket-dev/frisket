from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import ActionRequest
from frisket.engine.executor.table_action import (
    run_typed_create_sheet_action,
)
from frisket.engine.store import Project
from frisket.sdk import (
    ActionParams,
    DynamicOutput,
    TableColumn,
    TableResult,
    TableRow,
    action,
    create_sheet,
)


class Params(ActionParams):
    text: str


class Output(BaseModel):
    text: str
    count: int


class SchemaParams(ActionParams):
    column_type: str


def test_create_sheet_is_not_a_row_scoped_copilot_proposal():
    from frisket.contracts.http.copilot import CopilotRegisteredActionDraft

    with pytest.raises(ValueError, match="action_id"):
        CopilotRegisteredActionDraft.model_validate(
            {
                "action_id": "import.rows",
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {},
                "output_names": {"text": "Text"},
            }
        )


@pytest.mark.parametrize("column_type", ["unregistered_type", "disabled_plugin_type"])
def test_table_schema_refuses_before_producer(tmp_path, column_type):
    from frisket.authoring.column_types import (
        register_column_type,
        unregister_column_type,
    )
    from frisket.actions.core import RegisteredAction

    calls = []

    def produce(params: SchemaParams) -> TableResult[DynamicOutput]:
        calls.append(params.column_type)
        return TableResult(rows=[])

    register_column_type("disabled_plugin_type", plugin="disabled.plugin")
    project = Project.create(tmp_path / "project")
    try:
        registered = RegisteredAction(
            "test.table",
            action(
                name="table",
                title="Table",
                description="Create a table",
                category=ActionCategory.CONVERT,
                run=create_sheet(
                    produce,
                    columns_from=lambda params: (
                        TableColumn("value", params.column_type),
                    ),
                ),
            ),
        )
        bound = BoundTypedActionRequest.bind(
            registered,
            ActionRequest(
                action_id="test.table",
                scope={"kind": "project"},
                sheet_name="Table",
                params={"column_type": column_type},
                idempotency_key="invalid-schema",
            ),
        )
        result = run_typed_create_sheet_action(project, "project", bound)
        assert result.status == "failed"
        assert calls == []
        for table in ("sheets", "columns", "rows", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
    finally:
        project.close()
        unregister_column_type("disabled_plugin_type")


def test_table_schema_has_one_authority():
    def static(params: Params) -> TableResult[Output]:
        return TableResult(rows=[])

    def dynamic(params: Params) -> TableResult[DynamicOutput]:
        return TableResult(rows=[])

    with pytest.raises(TypeError, match="omit columns_from"):
        create_sheet(static, columns_from=lambda _: (TableColumn("text", "text"),))
    with pytest.raises(TypeError, match="requires columns_from"):
        create_sheet(dynamic)


def test_create_sheet_materializes_typed_outputs_once_and_pins_destination(tmp_path):
    project = Project.create(tmp_path / "project")
    calls = []

    def produce(params: Params) -> TableResult[Output]:
        assert not project.db.in_transaction
        calls.append(params.text)
        return TableResult(
            rows=[TableRow(output=Output(text=params.text, count=len(params.text)))]
        )

    registered = ActionRegistry(
        (
            ActionNamespace(
                "test",
                actions=(
                    action(
                        name="table",
                        title="Table",
                        description="Create a table",
                        category=ActionCategory.CONVERT,
                        run=create_sheet(produce),
                    ),
                ),
            ),
        )
    ).get("test.table")
    request = ActionRequest(
        action_id="test.table",
        scope={"kind": "project"},
        sheet_name="Destination",
        params={"text": "hello"},
        output_names={"text": "Message"},
        idempotency_key="table",
    )
    try:
        bound = BoundTypedActionRequest.bind(registered, request)
        first = run_typed_create_sheet_action(project, "project", bound)
        assert first.status == "completed", first.errors
        assert first.run_id is None
        assert (
            run_typed_create_sheet_action(project, "project", bound).receipt_id
            == first.receipt_id
        )
        assert calls == ["hello"]
        columns = project.db.execute(
            "SELECT name, type, hidden FROM columns ORDER BY position"
        ).fetchall()
        assert [tuple(column) for column in columns] == [
            ("Message", "text", 0),
            ("count", "integer", 0),
        ]
        op = project.db.execute("SELECT spec, undo_info FROM ops").fetchone()
        assert json.loads(op["spec"])["sheet_name"] == "Destination"
        sheet_id = first.outputs[0].ref["sheet_id"]
        assert json.loads(op["undo_info"]) == {"created_sheets": [sheet_id]}
        original_rows = [tuple(row) for row in project.db.execute("SELECT * FROM rows")]
        assert project.undo() == first.op_ids[0]
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (sheet_id,)
            ).fetchone()[0]
            == 1
        )
        assert project.redo() == first.op_ids[0]
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (sheet_id,)
            ).fetchone()[0]
            == 0
        )
        assert [
            tuple(row) for row in project.db.execute("SELECT * FROM rows")
        ] == original_rows
        assert [
            tuple(column)
            for column in project.db.execute(
                "SELECT name, type, hidden FROM columns ORDER BY position"
            )
        ] == [("Message", "text", 0), ("count", "integer", 0)]
        for changed in ({"sheet_name": "Other"}, {"output_names": {"text": "Other"}}):
            other = BoundTypedActionRequest.bind(
                registered, request.model_copy(update=changed)
            )
            result = run_typed_create_sheet_action(project, "project", other)
            assert result.errors[0].code == "idempotency_conflict"
        assert calls == ["hello"]
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
    finally:
        project.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"sheet_name": None},
        {"sheet_name": "   "},
        {"output_names": {"missing": "Other"}},
        {"output_names": {"text": "count"}},
        {"replace_existing": True},
        {"scope": {"kind": "sheet_rows", "sheet_id": 1}},
    ],
)
def test_import_destination_validation(patch):
    request = {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Rows",
        "idempotency_key": "create",
        "params": {
            "columns": [
                {"name": "text", "type": "text"},
                {"name": "count", "type": "integer"},
            ],
            "rows": [{"text": "hello", "count": 5}],
        },
    }
    result = validate_root_action({**request, **patch})
    assert not result.ok
    assert result.error.code == "invalid_action_request"


def test_create_sheet_rejects_invalid_produced_rows_without_publishing(tmp_path):
    project = Project.create(tmp_path / "project")

    def produce(params: Params) -> TableResult[DynamicOutput]:
        return TableResult(
            rows=[TableRow(output=DynamicOutput({"text": params.text, "count": 1}))]
        )

    registered = ActionRegistry(
        (
            ActionNamespace(
                "test",
                actions=(
                    action(
                        name="bad",
                        title="Bad",
                        description="Create an invalid table",
                        category=ActionCategory.CONVERT,
                        run=create_sheet(
                            produce,
                            columns_from=lambda _: (TableColumn("text", "text"),),
                        ),
                    ),
                ),
            ),
        )
    ).get("test.bad")
    request = ActionRequest(
        action_id="test.bad",
        scope={"kind": "project"},
        sheet_name="Bad",
        params={"text": "hello"},
        idempotency_key="bad",
    )
    try:
        result = run_typed_create_sheet_action(
            project, "project", BoundTypedActionRequest.bind(registered, request)
        )
        assert result.errors[0].code == "invalid_params"
        for table in ("sheets", "columns", "rows", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
    finally:
        project.close()


def test_params_defined_schema_is_resolved_once_and_retains_metadata(tmp_path):
    from frisket.actions.core import RegisteredAction

    calls = []

    def columns(params):
        calls.append(params.column_type)
        if len(calls) != 1:
            raise AssertionError("schema was projected twice")
        return (TableColumn("value", params.column_type, format=",.2f", hidden=True),)

    def produce(params: SchemaParams) -> TableResult[DynamicOutput]:
        return TableResult(rows=[TableRow(output=DynamicOutput({"value": 2.5}))])

    registered = RegisteredAction(
        "test.schema",
        action(
            name="schema",
            title="Schema",
            description="Schema",
            category=ActionCategory.CONVERT,
            run=create_sheet(produce, columns_from=columns),
        ),
    )
    bound = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="test.schema",
            scope={"kind": "project"},
            sheet_name="Table",
            params={"column_type": "number"},
            idempotency_key="schema",
        ),
    )
    project = Project.create(tmp_path / "schema")
    try:
        result = run_typed_create_sheet_action(project, "schema", bound)
        assert result.status == "completed", result.errors
        column = project.db.execute(
            "SELECT type, format, hidden FROM columns"
        ).fetchone()
        assert tuple(column) == ("number", ",.2f", 1)
        assert calls == ["number"]
    finally:
        project.close()
