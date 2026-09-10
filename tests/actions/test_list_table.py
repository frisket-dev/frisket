from __future__ import annotations


import pytest
from pydantic import ValidationError

from frisket.actions.list_table import (
    ListProjectionColumn,
    ListTableParams,
    declared_columns,
    expand_list_table,
)
from frisket.actions.types import (
    DynamicTableResult,
    ListColumnSource,
    ListItem,
    NamedListSource,
    RowSource,
    TableError,
    TableResult,
)
from frisket.engine.executor.list_table_read import AdmittedListTableReader
from frisket.engine.store import Project
from helpers import replace_test_source_cell


class ItemReader:
    def __init__(self, values):
        self.items = tuple(
            ListItem(value=value, source=RowSource(sheet_id=1, row_id=1))
            for value in values
        )

    def read(self, source, *, item_schema=None):
        return self.items


def _live_params(**source):
    return ListTableParams(
        source=ListColumnSource(kind="column", sheet_id=1, column_id=1, **source)
    )


def test_infers_complete_key_union_and_keeps_one_identity_per_item():
    reader = ItemReader([{"early": 1}] * 120 + [{"late": 2.5, "flag": False}])
    params = _live_params()
    assert declared_columns(params) is None
    result = expand_list_table(params, reader)
    assert isinstance(result, DynamicTableResult)
    assert [(column.key, column.type) for column in result.schema] == [
        ("early", "integer"),
        ("late", "number"),
        ("flag", "boolean"),
    ]
    rows = tuple(result.rows)
    assert rows[0].output.root == {"early": 1, "late": None, "flag": None}
    assert rows[-1].output.root == {"early": None, "late": 2.5, "flag": False}
    assert rows[0].sources[0] is reader.items[0].source
    assert rows[1].sources[0] is not rows[0].sources[0]


@pytest.mark.parametrize(
    ("values", "expected_type"),
    [
        ([1, 2.5], "number"),
        ([None, None], "text"),
        ([False, True], "boolean"),
        ([1, "1"], "json"),
    ],
)
def test_scalar_inference_keeps_existing_widening(values, expected_type):
    result = expand_list_table(_live_params(), ItemReader(values))
    assert result.schema[0].type == expected_type
    assert [row.output.root["value"] for row in result.rows] == values


def test_file_envelopes_are_one_file_column_unless_projection_requested():
    value = {"blob": "a" * 64, "mime": "application/pdf", "filename": "a.pdf"}
    result = expand_list_table(_live_params(), ItemReader([value]))
    assert [(column.key, column.type) for column in result.schema] == [
        ("attachment", "file")
    ]
    assert tuple(result.rows)[0].output.root == {"attachment": value}
    selected = expand_list_table(
        _live_params(include_columns=["filename", "mime"]), ItemReader([value])
    )
    assert [column.key for column in selected.schema] == ["filename", "mime"]


@pytest.mark.parametrize("values", [[], [{}, {}], [{"x": 1}, 2]])
def test_noninferable_tables_refuse(values):
    with pytest.raises(TableError):
        expand_list_table(_live_params(), ItemReader(values))


def test_named_projection_does_not_rediscover_schema_and_preserves_hidden():
    params = ListTableParams(
        source=NamedListSource(
            kind="named_result",
            sheet_id=1,
            column_id=1,
            run_id=1,
            route="items",
            schema="items",
        ),
        item_schema={"type": "object"},
        columns=[
            ListProjectionColumn(
                name="selected", path="$.values[1]", type="integer", hidden=True
            )
        ],
    )
    assert declared_columns(params)[0].hidden is True
    result = expand_list_table(params, ItemReader([{"values": [1, 8]}]))
    assert isinstance(result, TableResult)
    assert not isinstance(result, DynamicTableResult)
    assert tuple(result.rows)[0].output.root == {"selected": 8}
    assert tuple(expand_list_table(params, ItemReader([])).rows) == ()
    with pytest.raises(TableError, match="path did not resolve"):
        tuple(expand_list_table(params, ItemReader([{}])).rows)


def test_source_modes_cannot_silently_change_schema_authority():
    with pytest.raises(ValidationError):
        ListTableParams(
            source=ListColumnSource(kind="column", sheet_id=1, column_id=1), columns=[]
        )
    with pytest.raises(ValidationError):
        ListTableParams(
            source=NamedListSource(
                kind="named_result",
                sheet_id=1,
                column_id=1,
                run_id=1,
                route="r",
                schema="s",
            )
        )


def test_reader_holds_snapshot_uses_actual_source_and_retains_item_associations(
    tmp_path,
):
    project = Project.create(tmp_path / "p")
    try:
        sheet = project.add_sheet("source")
        column = project.add_column(sheet, "items", "json", ai_generated=True)
        rows = project.add_rows(sheet, [{"items": ["a", "b"]}], {"items": column})
        reader = AdmittedListTableReader(project, "custom.expand")
        result = reader.read(
            ListColumnSource(kind="column", sheet_id=sheet, column_id=column)
        )
        assert [item.value for item in result] == ["a", "b"]
        assert reader.parent_sheet_id == sheet
        assert reader.source_ai_generated is True
        assert reader.sources == {item.source for item in result}
        assert [
            reader.item_associations[item.source]["item_index"] for item in result
        ] == [0, 1]
        assert all(
            reader.item_associations[item.source]["source_row_id"] == rows[0]
            for item in result
        )
        assert RowSource(sheet_id=sheet, row_id=rows[0]) not in reader.sources
        assert not project.db.in_transaction
        # A read inside refresh's transaction must neither commit it nor replace
        # its snapshot with another connection that cannot see these changes.
        project.db.execute("BEGIN IMMEDIATE")
        replace_test_source_cell(
            project,
            db=project.db,
            row_id=rows[0],
            column_id=column,
            value=["edited"],
        )
        nested = AdmittedListTableReader(project)
        assert (
            nested.read(
                ListColumnSource(kind="column", sheet_id=sheet, column_id=column)
            )[0].value
            == "edited"
        )
        assert project.db.in_transaction
        project.db.rollback()
    finally:
        project.close()


@pytest.fixture
def named_project(tmp_path, monkeypatch, request):
    from tests.engine import test_derive_table_from_list_executor as fixtures

    if getattr(request, "param", None) == "empty":
        original = fixtures._map_named_results_action

        def empty_action(sheet_id):
            action = original(sheet_id)
            action["params"]["code"] = "result = {'entities': [], 'debug': {}}"
            return action

        monkeypatch.setattr(fixtures, "_map_named_results_action", empty_action)

    project = Project.create(tmp_path / "named")
    try:
        seeded = fixtures._seed(project, tmp_path)
        yield project, seeded
    finally:
        project.close()


def _named_source(seeded):
    return NamedListSource(
        kind="named_result",
        sheet_id=seeded["sheet_id"],
        column_id=seeded["entities_column_id"],
        run_id=seeded["run_id"],
        route="entities",
        schema="entity_list",
    )


def _item_schema():
    return {
        "type": "object",
        "required": ["name", "title"],
        "properties": {
            "name": {"type": "string"},
            "title": {"type": "string"},
        },
    }


def test_named_reader_checks_receipt_schema_and_result_cells(
    named_project, monkeypatch
):
    from frisket.contracts.action import Receipt
    from frisket.engine.store.receipts import ReceiptStore

    project, seeded = named_project
    source = _named_source(seeded)
    reader = AdmittedListTableReader(project)
    assert len(reader.read(source, item_schema=_item_schema())) == 2
    assert reader.facts[0]["source_receipt_id"]
    assert reader.facts[0]["named_result"]["route"] == "entities"
    assert "derive.table_from_list" in reader.facts[0]["named_result"]["may_feed"]
    with pytest.raises(TableError, match="differs"):
        reader.read(source, item_schema={"type": "string"})
    # A malformed receipt may claim a real source row that its run never
    # produced. Keep published results immutable and test that claim directly.
    missing_row = project.add_rows(source.sheet_id, [{}], {})[0]
    bodies = ReceiptStore(project).bodies_for_run_status(source.run_id, "completed")
    receipts = [Receipt.model_validate_json(body) for body in bodies]
    for receipt in receipts:
        for output in receipt.outputs:
            if output.ref.get("column_id") == source.column_id:
                output.ref["row_ids"].append(missing_row)
    monkeypatch.setattr(
        ReceiptStore,
        "bodies_for_run_status",
        lambda self, run_id, status: [
            receipt.model_dump_json() for receipt in receipts
        ],
    )
    with pytest.raises(TableError, match="missing or errored"):
        AdmittedListTableReader(project).read(source, item_schema=_item_schema())


@pytest.mark.parametrize("named_project", ["empty"], indirect=True)
def test_empty_named_values_keep_admitted_parent(named_project):
    project, seeded = named_project
    source = _named_source(seeded)
    reader = AdmittedListTableReader(project)
    assert reader.read(source, item_schema=_item_schema()) == ()
    assert reader.parent_sheet_id == source.sheet_id
    assert reader.facts[0]["item_count"] == 0
