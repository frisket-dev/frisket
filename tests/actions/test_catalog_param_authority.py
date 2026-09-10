from decimal import Decimal

import pytest
from pydantic import Field

from frisket.actions.core import RegisteredAction, _semantic_metadata
from frisket.actions.group_summary import GROUP_SUMMARY
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ActionParams, ColumnRef
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.group_summary_action import run_typed_group_summary_action
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.consent_coverage import ConsentCoverage


class OptionalReferences(ActionParams):
    required_column: ColumnRef[str]
    optional_column: ColumnRef[str] | None = None
    nullable_column: ColumnRef[str] | None


def test_column_list_bounds_are_projected_from_the_params_schema():
    class References(ActionParams):
        bounded: list[ColumnRef[str]] = Field(min_length=2, max_length=3)
        unbounded: list[ColumnRef[str]]

    _, requirements = _semantic_metadata(References)
    by_param = {item["param"]: item for item in requirements}
    assert by_param["bounded"]["min"] == 2
    assert by_param["bounded"]["max"] == 3
    assert "max" not in by_param["unbounded"]


def test_source_requirement_minimum_follows_param_presence_and_nullability():
    _, requirements = _semantic_metadata(OptionalReferences)
    assert {item["param"]: item["min"] for item in requirements} == {
        "required_column": 1,
        "optional_column": 0,
        "nullable_column": 0,
    }
    OptionalReferences(required_column="body", nullable_column=None)
    group = RegisteredAction("custom.summary", GROUP_SUMMARY).catalog_entry()
    requirements = group["ui_hints"]["source_requirements"]
    assert (
        next(item for item in requirements if item["param"] == "group_by")["min"] == 0
    )
    assert group["execution_mode"] == "grouped"


@pytest.mark.parametrize("kind,allows_blob", [("map.ner", False), ("map.ask", True)])
def test_rich_source_kinds_follow_direct_column_types_not_template_types(
    kind, allows_blob
):
    entry = ACTION_REGISTRY.get(kind).catalog_entry()
    source = next(
        item
        for item in entry["ui_hints"]["source_requirements"]
        if item["param"] == "source"
    )
    assert ("blob" in source["accepted_cell_kinds"]) is allows_blob
    assert "template" in source["accepted_cell_kinds"]
    if kind == "map.ner":
        assert source["accepted_column_types"] == ["text", "timestamped_transcript"]
        assert "image" in source["template_accepted_column_types"]


@pytest.mark.parametrize(
    "model,reason,decline_price",
    [
        ("anthropic/claude-haiku-4-5", "model_cost", False),
        ("anthropic/unpriced-test", "unknown_estimate", False),
        ("anthropic/claude-haiku-4-5", "unknown_estimate", True),
    ],
)
def test_group_confirmation_reports_actual_quote_reason_before_effects(
    tmp_path, monkeypatch, model, reason, decline_price
):
    if decline_price:
        from types import SimpleNamespace

        from frisket.execution.pricing_policy import Unpriceable

        policy = SimpleNamespace(
            policy_id="test.declines",
            rate=lambda facts: Unpriceable(
                policy_id="test.declines", reason="No tariff"
            ),
        )
        monkeypatch.setattr(
            "frisket.execution.pricing_policy.default_pricing_policy", lambda: policy
        )
    project = Project.create(tmp_path / "group-quote.frisket")
    try:
        deps = ExecutorDeps(
            consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0"))
        )
        sheet = project.add_sheet("Reports")
        column = project.add_column(sheet, "body", type="text")
        project.add_rows(sheet, [{"body": "A public report. " * 80}], {"body": column})
        bound = typed_action_for_request(
            {
                "action_id": "reduce.group_summary",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {
                    "source": ["body"],
                    "model": model,
                    "instruction": "Summarize.",
                },
                "sheet_name": "Summary",
                "idempotency_key": "quote",
            }
        )
        before = tuple(project.db.iterdump())
        result = run_typed_group_summary_action(project, "quote", bound, deps=deps)
        assert result.status == "needs_confirmation", result.errors
        assert result.errors[0].details["reason"] == reason
        assert (result.errors[0].details["estimate"]["cost"] is None) is (
            model == "anthropic/unpriced-test"
        )
        if decline_price:
            assert result.errors[0].details["estimate"]["billed_cost"] is None
        assert result.errors[0].details["promise_set_hash"]
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()
