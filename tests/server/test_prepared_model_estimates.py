"""The preview service quotes the same prepared invocation that execution admits."""

from __future__ import annotations

from decimal import Decimal

import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.find_types import (
    FindOptions,
    FindScanner,
    FindSourceColumn,
    PreparedFind,
)
from frisket.actions.group_summary_types import (
    GroupSummarizer,
    GroupSummaryColumn,
    GroupSummaryOptions,
    PreparedGroupSummary,
)
from frisket.actions.types import ActionParams, ModelRef
from frisket.actions.system import typed_action_for_request
from frisket.contracts.http.action_estimate_validation import ActionEstimateResult
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.find_action import prepare_typed_find_admission
from frisket.engine.executor.group_summary_action import run_typed_group_summary_action
from frisket.engine.store import Project
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.pricing_policy import IDENTITY_PRICING_POLICY
from frisket.server.services.action_previews import (
    ActionPreviewService,
    ActionPreviewRouteError,
)


class FindingArgs(ActionParams):
    document: FindSourceColumn
    selected_model: ModelRef


def authored_find(params: FindingArgs, scanner: FindScanner) -> PreparedFind:
    return scanner.prepare(
        params.document,
        options=FindOptions(
            model=params.selected_model, instruction="Find every policy mention."
        ),
    )


class SummaryArgs(ActionParams):
    content: list[GroupSummaryColumn]
    bucket: GroupSummaryColumn
    selected_model: ModelRef


def authored_summary(
    params: SummaryArgs, summarizer: GroupSummarizer
) -> PreparedGroupSummary:
    return summarizer.prepare(
        params.content,
        options=GroupSummaryOptions(
            group_by=params.bucket,
            model=params.selected_model,
            instruction="Summarize each policy theme.",
        ),
    )


@pytest.fixture
def preview_project(tmp_path):
    project = Project.create(tmp_path / "preview.frisket")
    sheet = project.add_sheet("stories")
    columns = {
        name: project.add_column(sheet, name, type="text")
        for name in ("body", "bucket")
    }
    rows = project.add_rows(
        sheet,
        [
            {"body": "First policy report.", "bucket": "A"},
            {"body": "Another policy report.", "bucket": "A"},
        ],
        columns,
    )
    before = {
        table: project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("runs", "results", "receipts", "model_calls", "ops")
    }

    class Workspace:
        edition = "solo"

        def __init__(self):
            self.consent_coverage = ConsentCoverage(
                "test:prepared-preview", Decimal("0")
            )

        def executor_deps_factory(self, _project_id, _request):
            return ExecutorDeps(consent_coverage=self.consent_coverage)

        def get(self, project_id):
            assert project_id == "test"
            return project

        def router_for(self, project):
            raise AssertionError(
                "Prepared estimates must not construct provider transport"
            )

    workspace = Workspace()
    yield (
        project,
        sheet,
        rows,
        ActionPreviewService(workspace),
        workspace.consent_coverage,
        before,
    )
    project.close()


@pytest.mark.parametrize("kind", ["find", "summary"])
def test_prepared_preview_quotes_actual_arguments_once_and_matches_execution_gate(
    preview_project, monkeypatch, kind
):
    from frisket.actions import system
    from frisket.execution import pricing_policy
    from frisket.server.services import action_previews

    project, sheet, rows, service, consent_coverage, before = preview_project
    handler = authored_find if kind == "find" else authored_summary
    definition = action(
        name=kind,
        title=kind,
        description=kind,
        category=ActionCategory.TEXT,
        run=handler,
    )
    registry = ActionRegistry([ActionNamespace("custom", actions=[definition])])
    monkeypatch.setattr(system, "ACTION_REGISTRY", registry)
    monkeypatch.setattr(
        action_previews, "NEW_ACTION_IDS", frozenset(registry.action_ids)
    )

    class CountingPolicy:
        policy_id = IDENTITY_PRICING_POLICY.policy_id
        calls = 0

        def rate(self, facts):
            self.calls += 1
            return IDENTITY_PRICING_POLICY.rate(facts)

    policy = CountingPolicy()
    monkeypatch.setattr(action_previews, "default_pricing_policy", lambda: policy)
    monkeypatch.setattr(pricing_policy, "default_pricing_policy", lambda: policy)
    params = {"selected_model": "anthropic/claude-haiku-4-5"}
    params.update(
        {"document": "body"}
        if kind == "find"
        else {"content": ["body"], "bucket": "bucket"}
    )
    request = {
        "action_id": "custom." + kind,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
        "params": params,
        "sheet_name": "Derived",
        "output_names": {"match" if kind == "find" else "summary": "Overview"},
        "idempotency_key": "estimate-" + kind,
    }
    result = service.estimate("test", request)
    ActionEstimateResult.model_validate(result)
    estimate = result["estimate"]
    assert result["action"] == {"kind": "custom." + kind}
    assert estimate["rows"] == 1
    assert estimate["cost"] > 0
    assert estimate["billed_cost"] > 0
    assert estimate["requires_confirmation"] is True
    assert policy.calls == 1

    bound = typed_action_for_request(request)
    if kind == "find":
        error = prepare_typed_find_admission(
            project, bound, consent_coverage=consent_coverage
        )
    else:
        refusal = run_typed_group_summary_action(
            project,
            "test",
            bound,
            deps=ExecutorDeps(consent_coverage=consent_coverage),
        )
        assert refusal.status == "needs_confirmation"
        error = refusal.errors[0]
    assert error.needs_confirmation
    assert error.details["promise_set_hash"] == estimate["promise_set_hash"]
    assert error.details["estimate"]["billed_cost"] == estimate["billed_cost"]
    assert policy.calls == 2
    for table, count in before.items():
        assert (
            project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        )
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1


@pytest.mark.parametrize("action_id", ["map.find", "reduce.group_summary"])
def test_prepared_preview_returns_actual_source_refusal_without_state(
    preview_project, action_id
):
    project, sheet, _, service, _consent_coverage, before = preview_project
    request = {
        "action_id": action_id,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": "missing" if action_id == "map.find" else ["missing"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Find the policy."
            if action_id == "map.find"
            else "Summarize the policy.",
        },
        "sheet_name": "Derived",
        "idempotency_key": "missing-" + action_id,
    }
    with pytest.raises(ActionPreviewRouteError) as refusal:
        service.estimate("test", request)
    assert refusal.value.status_code == 400
    assert refusal.value.bare_json
    assert refusal.value.content["code"] == "invalid_input_ref"
    assert refusal.value.content["field"] == "params.source"
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    for table, count in before.items():
        assert (
            project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        )
