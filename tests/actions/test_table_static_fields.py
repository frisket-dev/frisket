import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, Field

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    EmailSourceReader,
    ProjectScope,
    Row,
    RowResult,
    StagedFile,
    TableColumn,
    TableResult,
    TableRow,
)
from frisket.sdk import map_rows


class Params(ActionParams):
    pass


class EmailFields(BaseModel):
    from_: str = Field(alias="from")
    body: str = Field(json_schema_extra={"format": "plain_text"})
    attachments_: list[StagedFile] = Field(alias="attachments")


def _registered(producer):
    return RegisteredAction(
        "test.email_fields",
        action(
            name="email_fields",
            title="Email fields",
            description="Static table field projection",
            category=ActionCategory.CONVERT,
            run=create_sheet(producer),
        ),
    )


def test_static_alias_schema_names_bindings_and_formats_agree_before_invocation():
    calls = []

    def produce(params: Params) -> TableResult[EmailFields]:
        calls.append(params)
        return TableResult(rows=[])

    registered = _registered(produce)
    terminal = registered.definition.run
    assert terminal.output_bindings() == {
        "from": "from_",
        "body": "body",
        "attachments": "attachments_",
    }
    with pytest.raises(TypeError):
        terminal.output_bindings()["from"] = "body"
    assert [
        (field.key, field.column_type, field.format) for field in terminal.output_fields
    ] == [
        ("from", "text", None),
        ("body", "text", "plain_text"),
        ("attachments", "json", None),
    ]
    assert terminal.resolve_columns(Params()) == (
        TableColumn("from", "text"),
        TableColumn("body", "text", "plain_text"),
        TableColumn("attachments", "json"),
    )
    catalog = registered.catalog_entry()
    properties = catalog["output_schema"]["properties"]
    assert list(properties) == ["from", "body", "attachments"]
    assert properties["body"]["format"] == "plain_text"
    assert [field["key"] for field in catalog["ui_hints"]["logical_outputs"]] == [
        "from",
        "body",
        "attachments",
    ]
    registered.bind_values(
        scope=ProjectScope(),
        params={},
        output_names={"from": "Sender", "attachments": "Files"},
    )
    with pytest.raises(ValueError, match="unknown output names"):
        registered.bind_values(
            scope=ProjectScope(), params={}, output_names={"from_": "Sender"}
        )
    with pytest.raises(ValueError, match="final output names must be unique"):
        registered.bind_values(
            scope=ProjectScope(), params={}, output_names={"from": "body"}
        )
    assert calls == []


def test_alias_bindings_preserve_live_tokens_and_exclude_python_fields():
    def produce(params: Params) -> TableResult[EmailFields]:
        return TableResult(rows=[])

    terminal = create_sheet(produce)
    token = StagedFile(7)
    output = EmailFields.model_validate(
        {"from": "Ada", "body": "Hello", "attachments": [token]}
    )
    attribute = terminal.output_bindings()["attachments"]
    assert getattr(output, attribute)[0] is token
    assert output.model_dump(mode="json", by_alias=True, exclude={attribute}) == {
        "from": "Ada",
        "body": "Hello",
    }


@pytest.mark.parametrize(
    "alias_fields",
    [
        {"serialization_alias": "other"},
        {"alias": "input", "serialization_alias": "output"},
        {"validation_alias": AliasChoices("value", "other")},
        {"validation_alias": AliasPath("nested", "value")},
        {"alias": " "},
        {"alias": ""},
    ],
)
def test_ambiguous_or_invalid_aliases_refuse_before_invocation(alias_fields):
    class Output(BaseModel):
        value: str = Field(**alias_fields)

    calls = []

    def produce(params: Params) -> TableResult[Output]:
        calls.append(params)
        return TableResult(rows=[])

    with pytest.raises(TypeError, match="static table output"):
        create_sheet(produce)
    assert calls == []


def test_consistent_explicit_serialization_alias_is_supported():
    class Output(BaseModel):
        value: str = Field(alias="text", serialization_alias="text")

    def produce(params: Params) -> TableResult[Output]:
        return TableResult(rows=[])

    terminal = create_sheet(produce)
    assert terminal.output_bindings() == {"text": "value"}
    assert terminal.resolve_columns(Params()) == (TableColumn("text", "text"),)
    assert Output.model_validate({"text": "hello"}).model_dump(by_alias=True) == {
        "text": "hello"
    }


def test_alias_colliding_with_another_canonical_key_refuses_before_invocation():
    class Output(BaseModel):
        first: str = Field(alias="second")
        second: str

    calls = []

    def produce(params: Params) -> TableResult[Output]:
        calls.append(params)
        return TableResult(rows=[])

    with pytest.raises(TypeError, match="unique logical keys"):
        create_sheet(produce)
    assert calls == []


@pytest.mark.parametrize("format_value", ["", " plain_text", 42, []])
def test_invalid_static_format_metadata_refuses(format_value):
    class Output(BaseModel):
        body: str = Field(json_schema_extra={"format": format_value})

    def produce(params: Params) -> TableResult[Output]:
        return TableResult(rows=[])

    with pytest.raises(TypeError, match="column format"):
        create_sheet(produce)


def test_email_reader_is_an_explicit_table_capability_with_catalog_facts():
    def produce(params: Params, sources: EmailSourceReader) -> TableResult[EmailFields]:
        return TableResult(rows=[])

    registered = _registered(produce)
    assert registered.definition.run.capabilities == (EmailSourceReader,)
    catalog = registered.catalog_entry()
    assert "resolve_server_issued_email_sources" in catalog["side_effects"]
    assert "email_sources_unavailable" in {error["code"] for error in catalog["errors"]}


def test_dynamic_tables_do_not_invent_static_attribute_bindings():
    def produce(params: Params) -> TableResult[DynamicOutput]:
        return TableResult(rows=[])

    terminal = create_sheet(
        produce, columns_from=lambda params: (TableColumn("value", "text"),)
    )
    assert terminal.output_bindings() == {}


def test_row_fields_keep_logical_names_and_share_declared_formats():
    class Output(BaseModel):
        from_: str = Field(alias="from", json_schema_extra={"format": "plain_text"})

    def transform(params: Params, row: Row) -> RowResult[Output]:
        raise NotImplementedError

    terminal = map_rows(transform)
    assert [(field.key, field.format) for field in terminal.output_fields] == [
        ("from_", "plain_text")
    ]


def test_aliased_static_table_materializes_renames_and_formats(tmp_path):
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.engine.executor.table_action import run_typed_create_sheet_action
    from frisket.engine.store import Project

    def produce(params: Params) -> TableResult[EmailFields]:
        return TableResult(
            rows=[
                TableRow(
                    output=EmailFields.model_validate(
                        {"from": "Ada", "body": "<plain>", "attachments": []}
                    )
                )
            ]
        )

    registered = _registered(produce)
    bound = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            params={},
            sheet_name="Mail",
            output_names={"from": "Sender", "body": "Text", "attachments": "Files"},
            idempotency_key="static-aliases",
        ),
    )
    project = Project.create(tmp_path / "project")
    try:
        result = run_typed_create_sheet_action(project, "project", bound)
        assert result.status == "completed", result.errors
        columns = project.db.execute(
            "SELECT name, type, format FROM columns ORDER BY position"
        ).fetchall()
        assert [tuple(column) for column in columns] == [
            ("Sender", "text", None),
            ("Text", "text", "plain_text"),
            ("Files", "json", None),
        ]
    finally:
        project.close()
