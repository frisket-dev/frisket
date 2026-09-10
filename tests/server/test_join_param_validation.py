from types import SimpleNamespace

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.sdk import JoinedTablesReader, JoinKeyPair
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicTableResult,
    SheetRef,
)
from frisket.engine.executor.table_action import (
    prepare_table_producer,
    run_typed_create_sheet_action,
)
from frisket.engine.store import Project
from frisket.server.services import action_param_validation


class CustomJoinParams(ActionParams):
    lookup: SheetRef


def custom_join(
    params: CustomJoinParams, tables: JoinedTablesReader
) -> DynamicTableResult:
    return tables.read(
        params.lookup,
        join_keys=[JoinKeyPair(left_column="key", right_column="key")],
        indicator=True,
    )


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "join-form.frisket")
    try:
        yield project
    finally:
        project.close()


def seed(project, *, count=1001):
    sheets = []
    for side in ("left", "right"):
        sheet = project.add_sheet(side)
        columns = {
            "key": project.add_column(sheet, "key", type="text"),
            f"{side}_value": project.add_column(sheet, f"{side}_value", type="integer"),
        }
        project.add_rows(sheet, [{"key": "same", f"{side}_value": 1}] * count, columns)
        sheets.append(sheet)
    return sheets


def snapshot(project):
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("sheets", "columns", "rows", "ops", "receipts")
    }


def test_semantic_child_schema_uses_source_types_without_running_matcher(project):
    left, right = seed(project, count=1)
    service = action_param_validation.ActionParamValidationService(
        SimpleNamespace(edition="local", get=lambda _id: project)
    )
    before = snapshot(project)
    result = service.validate_params(
        "project",
        {
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": left},
            "params": {
                "source": "key",
                "target": {"sheet_id": right, "column": "key"},
                "carry": ["left_value"],
            },
        },
    )
    assert result["diagnostics"] == {}
    assert result["logical_outputs"][-2:] == [
        {"key": "source", "column_type": "text"},
        {"key": "carry.left_value", "column_type": "integer"},
    ]
    assert snapshot(project) == before


@pytest.mark.parametrize("custom", [False, True])
def test_unconfirmed_million_row_join_validates_names_but_cannot_expand(
    project, monkeypatch, custom
):
    from frisket.engine.executor import joined_tables_read

    left, right = seed(project)
    kind = "custom.lookup" if custom else "derive.join"
    if custom:
        definition = action(
            name="lookup",
            title="Lookup",
            description="Discover actual custom join arguments",
            category=ActionCategory.CONVERT,
            run=create_sheet(custom_join),
        )
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {**ACTION_REGISTRY._actions, kind: RegisteredAction(kind, definition)},
        )
        monkeypatch.setattr(
            action_param_validation,
            "NEW_ACTION_IDS",
            action_param_validation.NEW_ACTION_IDS | {kind},
        )
    request = {
        "action_id": kind,
        "scope": {"kind": "sheet_rows", "sheet_id": left},
        "params": {"lookup": {"sheet_id": right}}
        if custom
        else {
            "right": {"sheet_id": right},
            "join_keys": [{"left_column": "key", "right_column": "key"}],
            "indicator": True,
        },
    }

    def no_expansion(**kwargs):
        pytest.fail("discovery and unconfirmed execution must not expand the join")

    monkeypatch.setattr(joined_tables_read, "iter_join_records", no_expansion)
    service = action_param_validation.ActionParamValidationService(
        SimpleNamespace(edition="local", get=lambda _id: project)
    )
    before = snapshot(project)
    result = service.validate_params(
        "project",
        {
            **request,
            # Invalid physical names are reported without expanding the join.
            "sheet_name": "left",
            "output_names": {"key": "duplicate", "left_value": "duplicate"},
        },
    )
    assert result["diagnostics"] == {
        "output_names": {
            "ok": False,
            "message": "final output names must be unique",
        }
    }
    assert result["logical_outputs"] == []
    assert snapshot(project) == before
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(kind),
        ActionRequest.model_validate(
            {**request, "sheet_name": "Joined", "idempotency_key": "oversized-join"}
        ),
    )
    execution = run_typed_create_sheet_action(project, "project", bound)
    assert execution.status == "needs_confirmation"
    assert execution.errors[0].code == "join_fanout_requires_confirmation"
    assert execution.errors[0].details["estimated_rows"] == 1002001
    assert snapshot(project) == before


def test_schema_preparation_does_not_even_open_iterator_and_closes_resources(project):
    closed = []

    class UnopenedRows:
        def __iter__(self):
            pytest.fail("schema preparation must not open the row iterator")

        def close(self):
            closed.append(True)

    def lazy_join(
        params: CustomJoinParams, tables: JoinedTablesReader
    ) -> DynamicTableResult:
        produced = custom_join(params, tables)
        return DynamicTableResult(schema=produced.schema, rows=UnopenedRows())

    left, right = seed(project, count=1)
    definition = action(
        name="lazy",
        title="Lazy join",
        description="Discover schema without opening rows",
        category=ActionCategory.CONVERT,
        run=create_sheet(lazy_join),
    )
    bound = BoundTypedActionRequest.bind(
        RegisteredAction("custom.lazy", definition),
        ActionRequest.model_validate(
            {
                "action_id": "custom.lazy",
                "scope": {"kind": "sheet_rows", "sheet_id": left},
                "params": {"lookup": {"sheet_id": right}},
                "sheet_name": "Joined",
                "idempotency_key": "schema-only",
            }
        ),
    )
    with prepare_table_producer(project, bound, schema_only=True) as prepared:
        assert len(prepared.table.columns) == 4
        assert not closed
    assert closed == [True]
