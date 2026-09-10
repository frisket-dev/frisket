from __future__ import annotations

from contextlib import closing
from dataclasses import replace

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from frisket.actions.core import (
    ActionCategory,
    RegisteredAction,
    action,
    create_sheet,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    DynamicTableResult,
    ListColumnSource,
    ListItem,
    ListTableReader,
    ListTableSource,
    NamedListSource,
    Row,
    RowResult,
    RowSource,
    TableColumn,
    TableResult,
    TableRow,
)
from frisket.engine.executor.map_rows_action import (
    normalized_typed_request_identity,
    typed_request_hash,
)
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project


class Params(ActionParams):
    declared: bool = False


class StaticOutput(BaseModel):
    value: str


class SourceParams(ActionParams):
    source: ListTableSource


def _registered(producer, *, columns_from=None):
    return RegisteredAction(
        "example.table",
        action(
            name="table",
            title="Table",
            description="Create a table.",
            category=ActionCategory.CONVERT,
            run=create_sheet(producer, columns_from=columns_from),
        ),
    )


def _request(*, params=None, output_names=None):
    return ActionRequest(
        action_id="example.table",
        scope={"kind": "project"},
        params=params or {},
        output_names=output_names or {},
        sheet_name="Result",
        idempotency_key="table-request",
    )


def test_hybrid_binding_freezes_projection_once_without_reading_source():
    projections = []

    def produce(
        p: Params, tables: ListTableReader
    ) -> TableResult[DynamicOutput] | DynamicTableResult:
        raise AssertionError("binding must not invoke the producer")

    def columns(p):
        projections.append(p.declared)
        return (TableColumn("value", "text"),) if p.declared else None

    registered = _registered(produce, columns_from=columns)
    runtime = BoundTypedActionRequest.bind(
        registered, _request(output_names={"discovered": "Display"})
    )
    known = BoundTypedActionRequest.bind(
        registered, _request(params={"declared": True})
    )
    assert runtime.output_fields is None
    assert [field.key for field in known.output_fields] == ["value"]
    assert projections == [False, True]
    assert registered.definition.run.capabilities == (ListTableReader,)
    hints = registered.catalog_entry()["ui_hints"]
    assert hints["dynamic_outputs"] is True
    assert hints["typed_action"]["creates_sheet"] is True
    assert projections == [False, True]


def test_runtime_request_identity_does_not_need_discovery_and_omits_identity_renames():
    def produce(p: Params) -> DynamicTableResult:
        raise AssertionError("hashing must not inspect the source")

    registered = _registered(produce)
    omitted = BoundTypedActionRequest.bind(registered, _request())
    explicit = BoundTypedActionRequest.bind(
        registered, _request(output_names={"value": "value"})
    )
    renamed = BoundTypedActionRequest.bind(
        registered, _request(output_names={"value": "Display"})
    )
    assert typed_request_hash(omitted) == typed_request_hash(explicit)
    assert typed_request_hash(renamed) != typed_request_hash(omitted)
    before = typed_request_hash(renamed)
    fields = registered.definition.run.fields_from_columns(
        [TableColumn("value", "text"), TableColumn("other", "integer")]
    )
    registered.validate_output_names(fields, renamed.request.output_names)
    assert renamed.output_fields is None
    assert typed_request_hash(renamed) == before
    assert normalized_typed_request_identity(renamed)["output_names"] == {
        "value": "Display"
    }


def test_installed_implementation_identity_is_part_of_request_hash():
    def produce(p: Params) -> TableResult[StaticOutput]:
        return TableResult(rows=[])

    bound = BoundTypedActionRequest.bind(_registered(produce), _request())
    installed = replace(
        bound,
        implementation_identity={"plugin_id": "example", "package_sha256": "a" * 64},
    )
    assert normalized_typed_request_identity(installed)["implementation_identity"] == {
        "plugin_id": "example",
        "package_sha256": "a" * 64,
    }
    assert typed_request_hash(installed) != typed_request_hash(bound)


def test_static_table_identity_keeps_its_existing_expanded_default_names():
    def produce(p: Params) -> TableResult[StaticOutput]:
        return TableResult(rows=[])

    registered = _registered(produce)
    bound = BoundTypedActionRequest.bind(registered, _request())
    explicit = BoundTypedActionRequest.bind(
        registered, _request(output_names={"value": "value"})
    )
    assert normalized_typed_request_identity(bound) == {
        "action_id": "example.table",
        "scope": {"kind": "project"},
        "params": {},
        "output_names": {"value": "value"},
        "replace_existing": False,
        "sheet_name": "Result",
    }
    assert typed_request_hash(bound) == typed_request_hash(explicit)


def test_none_projection_requires_runtime_annotation_and_static_schema_stays_inferred():
    def known(p: Params) -> TableResult[DynamicOutput]:
        return TableResult(rows=[])

    registered = _registered(known, columns_from=lambda p: None)
    with pytest.raises(TypeError, match="only for a runtime table"):
        BoundTypedActionRequest.bind(registered, _request())

    def static_or_runtime(p: Params) -> TableResult[StaticOutput] | DynamicTableResult:
        return TableResult(rows=[])

    with pytest.raises(TypeError, match=r"TableResult\[DynamicOutput\]"):
        create_sheet(static_or_runtime)

    def runtime(p: Params) -> DynamicTableResult:
        return DynamicTableResult(schema=(), rows=())

    with pytest.raises(TypeError, match="omit columns_from"):
        create_sheet(runtime, columns_from=lambda p: (TableColumn("value", "text"),))


@pytest.mark.parametrize(
    ("names", "message"),
    [({"missing": "Display"}, "unknown output"), ({"value": "other"}, "unique")],
)
def test_runtime_name_validation_defers_only_schema_dependent_errors(names, message):
    def produce(p: Params) -> DynamicTableResult:
        raise AssertionError("binding must not discover columns")

    registered = _registered(produce)
    bound = BoundTypedActionRequest.bind(registered, _request(output_names=names))
    assert bound.output_fields is None
    fields = registered.definition.run.fields_from_columns(
        [TableColumn("value", "text"), TableColumn("other", "text")]
    )
    with pytest.raises(ValueError, match=message):
        registered.validate_output_names(fields, names)
    with pytest.raises(ValueError, match="trimmed"):
        registered.bind_values(
            scope=bound.request.scope, params={}, output_names={"value": " "}
        )


class TrackedRows:
    def __init__(self):
        self.iterations = 0
        self.closed = False

    def __iter__(self):
        self.iterations += 1
        return iter((TableRow(output=DynamicOutput({"value": "Ada"})),))

    def close(self):
        self.closed = True


@pytest.mark.parametrize("failure", ["empty", "duplicate", "unknown", "collision"])
def test_runtime_schema_errors_close_source_before_row_iteration(tmp_path, failure):
    rows = TrackedRows()
    columns = [TableColumn("value", "text"), TableColumn("other", "text")]
    if failure == "empty":
        columns = []
    elif failure == "duplicate":
        columns = [TableColumn("value", "text"), TableColumn("value", "text")]
    names = (
        {"missing": "Display"}
        if failure == "unknown"
        else {"value": "other"}
        if failure == "collision"
        else {}
    )

    def produce(p: Params) -> DynamicTableResult:
        return DynamicTableResult(schema=columns, rows=rows)

    registered = _registered(produce)
    bound = BoundTypedActionRequest.bind(registered, _request(output_names=names))
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "failed"
        assert rows.iterations == 0
        assert rows.closed
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


@pytest.mark.parametrize("declared", [False, True])
def test_hybrid_refuses_wrong_result_variant_without_consuming_rows(tmp_path, declared):
    rows = TrackedRows()

    def produce(p: Params) -> TableResult[DynamicOutput] | DynamicTableResult:
        if p.declared:
            return DynamicTableResult(schema=[TableColumn("value", "text")], rows=rows)
        return TableResult(rows=rows)

    registered = _registered(
        produce,
        columns_from=lambda p: [TableColumn("value", "text")] if p.declared else None,
    )
    bound = BoundTypedActionRequest.bind(
        registered, _request(params={"declared": declared})
    )
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "failed"
        assert rows.iterations == 0
        assert rows.closed
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


def test_known_schema_name_error_precedes_producer():
    def produce(p: Params) -> TableResult[DynamicOutput] | DynamicTableResult:
        raise AssertionError("known names must fail before producer invocation")

    registered = _registered(
        produce, columns_from=lambda p: [TableColumn("value", "text")]
    )
    with pytest.raises(ValueError, match="unknown output names"):
        BoundTypedActionRequest.bind(
            registered, _request(output_names={"unknown": "Display"})
        )


def test_runtime_table_publishes_renamed_columns_and_replays_without_discovery(
    tmp_path,
):
    calls = []

    def produce(p: Params) -> DynamicTableResult:
        calls.append(True)
        assert len(calls) == 1, "replay must not rediscover the table schema"
        return DynamicTableResult(
            schema=[
                TableColumn("value", "text", format="markdown", hidden=True),
                TableColumn("count", "integer"),
            ],
            rows=[TableRow(output=DynamicOutput({"value": "**Ada**", "count": 2}))],
        )

    registered = _registered(produce)
    request = _request(output_names={"value": "Display"})
    bound = BoundTypedActionRequest.bind(registered, request)
    original_hash = typed_request_hash(bound)
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        assert sheet["row_count"] == 1
        assert set(sheet["columns"]) == {"Display", "count"}
        descriptor = project.db.execute(
            "SELECT name, hidden, format FROM columns WHERE id=?",
            (sheet["columns"]["Display"],),
        ).fetchone()
        assert tuple(descriptor) == ("Display", 1, "markdown")
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["Display"]).values()
        ) == ["**Ada**"]
        assert bound.output_fields is None
        assert typed_request_hash(bound) == original_hash
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        changed = BoundTypedActionRequest.bind(
            registered,
            request.model_copy(update={"output_names": {"value": "Changed"}}),
        )
        conflict = run_typed_create_sheet_action(project, "p", changed)
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert calls == [True]


def test_dynamic_table_freezes_schema_without_consuming_rows():
    columns = [TableColumn("value", "text")]
    rows = TrackedRows()
    metadata = {"label": "Observed"}
    result = DynamicTableResult(schema=columns, rows=rows, source=metadata)
    columns.clear()
    metadata["label"] = "Changed"
    assert result.schema == (TableColumn("value", "text"),)
    assert result.rows is rows
    assert rows.iterations == 0
    assert result.source["label"] == "Observed"


def test_list_source_models_are_strict_and_round_trip_named_schema_alias():
    source = {
        "kind": "named_result",
        "sheet_id": 1,
        "column_id": 2,
        "run_id": 3,
        "route": "items",
        "schema": "ExtractedItems",
    }
    params = SourceParams(source=source)
    assert isinstance(params.source, NamedListSource)
    assert params.model_dump(mode="json") == {"source": source}
    assert SourceParams.model_validate(params.model_dump(mode="json")) == params
    for changes in ({"sheet_id": True}, {"column_id": "2"}, {"extra": "ignored"}):
        with pytest.raises(ValidationError):
            TypeAdapter(ListTableSource).validate_python({**source, **changes})
    column = ListColumnSource(
        kind="column", sheet_id=1, column_id=2, include_columns=[" b ", "a"]
    )
    assert column.include_columns == ["b", "a"]
    for names in ([], ["a", " a "], [" "], [1]):
        with pytest.raises(ValidationError):
            ListColumnSource(
                kind="column", sheet_id=1, column_id=2, include_columns=names
            )


def test_two_list_items_from_one_row_have_distinct_source_tokens():
    first = ListItem("first", RowSource(1, 2))
    second = ListItem("second", RowSource(1, 2))
    associations = {first.source: 0, second.source: 1}
    assert associations[first.source] == 0
    assert associations[second.source] == 1
    assert RowSource(1, 2) not in associations


def test_ordinary_row_catalog_does_not_claim_sheet_creation():
    def render(p: Params, row: Row) -> RowResult[StaticOutput]:
        return RowResult(output=StaticOutput(value="Ada"))

    registered = RegisteredAction(
        "example.row",
        action(
            name="row",
            title="Row",
            description="Render a row.",
            category=ActionCategory.TEXT,
            run=map_rows(render),
        ),
    )
    assert "typed_action" not in registered.catalog_entry()["ui_hints"]


@pytest.mark.parametrize("forge", [False, True])
def test_list_reader_tokens_not_parent_ids_authorize_publication(tmp_path, forge):
    def produce(p: SourceParams, tables: ListTableReader) -> DynamicTableResult:
        items = tables.read(p.source)
        return DynamicTableResult(
            schema=[TableColumn("value", "text")],
            rows=[
                TableRow(
                    output=DynamicOutput({"value": item.value}),
                    sources=(
                        RowSource(item.source.sheet_id, item.source.row_id)
                        if forge
                        else item.source,
                    ),
                )
                for item in items
            ],
        )

    with closing(Project.create(tmp_path / "project")) as project:
        sheet = project.add_sheet("Parent")
        column = project.add_column(sheet, "items", "json")
        project.add_rows(sheet, [{"items": ["a", "b"]}], {"items": column})
        bound = BoundTypedActionRequest.bind(
            _registered(produce),
            _request(
                params={
                    "source": {"kind": "column", "sheet_id": sheet, "column_id": column}
                }
            ),
        )
        result = run_typed_create_sheet_action(project, "p", bound)
        if forge:
            assert result.status == "failed"
            assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
        else:
            assert result.status == "completed", result.errors
            output = result.outputs[0].ref
            assert output["row_count"] == 2
            assert list(
                project.get_values(
                    output["sheet_id"], output["columns"]["value"]
                ).values()
            ) == ["a", "b"]


def test_empty_table_parent_comes_from_admitted_reader_not_producer_metadata(tmp_path):
    def produce(p: SourceParams, tables: ListTableReader) -> DynamicTableResult:
        tables.read(p.source)
        return DynamicTableResult(
            schema=[TableColumn("value", "text")],
            rows=[],
            source={"parent_sheet_id": 987654},
        )

    with closing(Project.create(tmp_path / "project")) as project:
        sheet = project.add_sheet("Parent")
        column = project.add_column(sheet, "items", "json")
        project.add_rows(sheet, [{"items": ["filtered out"]}], {"items": column})
        bound = BoundTypedActionRequest.bind(
            _registered(produce),
            _request(
                params={
                    "source": {"kind": "column", "sheet_id": sheet, "column_id": column}
                }
            ),
        )
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        output = result.outputs[0].ref
        assert output["row_count"] == 0
        assert (
            project.db.execute(
                "SELECT parent_sheet_id FROM sheets WHERE id=?", (output["sheet_id"],)
            ).fetchone()[0]
            == sheet
        )
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
