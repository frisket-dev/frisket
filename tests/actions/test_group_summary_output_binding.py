from dataclasses import replace

import pytest

from frisket.actions.core import (
    ActionCategory,
    RegisteredAction,
    action,
    has_dynamic_outputs,
)
from frisket.actions.group_summary import GroupSummaryParams, group_summary
from frisket.actions.group_summary_types import (
    GROUP_SUMMARY_COLUMNS,
    GroupSummarizer,
    PreparedGroupSummary,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request, validate_root_action
from frisket.engine.executor.group_summary_action import prepare_group_summary_action
from frisket.engine.store import Project


@pytest.fixture
def registered(monkeypatch):
    calls = []

    def summarize(
        params: GroupSummaryParams, summarizer: GroupSummarizer
    ) -> PreparedGroupSummary:
        calls.append(params)
        return group_summary(params, summarizer)

    definition = RegisteredAction(
        "custom.grouped",
        action(
            name="grouped",
            title="Grouped",
            description="Reuse the grouped-summary capability.",
            category=ActionCategory.TEXT,
            run=summarize,
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, definition.action_id: definition},
    )
    return definition, calls


def request(action_id, names):
    return {
        "action_id": action_id,
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": ["body"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Summarize the reports.",
        },
        "sheet_name": "Summaries",
        "output_names": names,
        "idempotency_key": "summary",
    }


@pytest.mark.parametrize("action_id", ["reduce.group_summary", "custom.grouped"])
@pytest.mark.parametrize(
    "names",
    [
        {"unknown": "Other"},
        {"summary": "group"},
        {"group": "rows"},
        {"summary": "Same", "rows": "Same"},
    ],
)
def test_fixed_group_outputs_refuse_invalid_names_before_preparation(
    registered, action_id, names
):
    _, calls = registered
    result = validate_root_action(request(action_id, names))
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert calls == []


def test_group_binding_catalog_and_preparation_share_fixed_columns(
    registered, tmp_path
):
    definition, calls = registered
    names = {"group": "Bucket", "rows": "Contributors", "summary": "Overview"}
    bound = typed_action_for_request(request(definition.action_id, names))
    expected = [(column.key, column.type) for column in GROUP_SUMMARY_COLUMNS]
    assert [(field.key, field.column_type) for field in bound.output_fields] == expected
    assert not has_dynamic_outputs(definition.definition.run)
    hints = definition.catalog_entry()["ui_hints"]
    assert [
        (field["key"], field["column_type"]) for field in hints["logical_outputs"]
    ] == expected
    assert not hints.get("dynamic_outputs")
    assert calls == []
    project = Project.create(tmp_path / "summary.frisket")
    try:
        sheet = project.add_sheet("Documents")
        assert sheet == 1
        column = project.add_column(sheet, "body", type="text")
        project.add_rows(sheet, [{"body": "A report."}], {"body": column})
        prepared = prepare_group_summary_action(project, bound)
        assert [
            (field["key"], field["column_type"]) for field in prepared.output_fields
        ] == expected
        assert prepared.operation.group_column_name == "Bucket"
        assert prepared.operation.row_count_column_name == "Contributors"
        assert prepared.operation.summary_column_name == "Overview"
        assert len(calls) == 1
        assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
    finally:
        project.close()


def test_group_preparation_retains_defensive_name_validation(registered):
    definition, _ = registered
    bound = typed_action_for_request(request(definition.action_id, {}))
    forged = replace(
        bound,
        request=bound.request.model_copy(update={"output_names": {"summary": "group"}}),
    )
    with pytest.raises(ValueError, match="distinct"):
        prepare_group_summary_action(None, forged)
