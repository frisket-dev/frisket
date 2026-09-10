"""Prepared summary/find capabilities consume actual author arguments."""

from decimal import Decimal

import pytest
from helpers import replace_test_source_cell

from frisket.actions.core import ActionCategory, RegisteredAction, action
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
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionParams, ActionRequest, ModelRef
from frisket.engine.executor.find_action import prepare_find_action
from frisket.engine.executor.group_summary_action import (
    prepare_group_summary_action,
    run_typed_group_summary_action,
)
from frisket.engine.store import Project
from frisket.execution.consent_coverage import ConsentCoverage


class SummaryArgs(ActionParams):
    content: list[GroupSummaryColumn]
    bucket: GroupSummaryColumn
    chosen_model: ModelRef


def summarize_renamed(
    params: SummaryArgs, summarizer: GroupSummarizer
) -> PreparedGroupSummary:
    return summarizer.prepare(
        params.content,
        options=GroupSummaryOptions(
            group_by=params.bucket,
            model=params.chosen_model,
            instruction="Report common themes and outliers.",
        ),
    )


class FindingArgs(ActionParams):
    document: FindSourceColumn
    selected_model: ModelRef


def find_renamed(params: FindingArgs, scanner: FindScanner) -> PreparedFind:
    return scanner.prepare(
        params.document,
        options=FindOptions(
            model=params.selected_model, instruction="Find every mention of policy."
        ),
    )


@pytest.fixture
def source_project(tmp_path):
    project = Project.create(tmp_path / "source.frisket")
    sheet = project.add_sheet("Sources")
    body = project.add_column(sheet, "body", type="text")
    bucket = project.add_column(sheet, "bucket", type="text")
    rows = project.add_rows(
        sheet,
        [
            {"body": "First policy report.", "bucket": "A"},
            {"body": "Another policy report.", "bucket": "A"},
        ],
        {"body": body, "bucket": bucket},
    )
    yield project, sheet, rows
    project.close()


def _bound(handler, params, sheet, *, key, names=None):
    registered = RegisteredAction(
        "custom." + key,
        action(
            name=key,
            title=key,
            description=key,
            category=ActionCategory.TEXT,
            run=handler,
        ),
    )
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params=params,
            sheet_name="Derived",
            output_names=names or {},
            idempotency_key=key,
        ),
    )


def test_summary_actual_arguments_are_readonly_and_own_contributors(source_project):
    project, sheet, rows = source_project
    bound = _bound(
        summarize_renamed,
        {
            "content": ["body"],
            "bucket": "bucket",
            "chosen_model": "anthropic/claude-haiku-4-5",
        },
        sheet,
        key="summary",
        names={"summary": "Overview"},
    )
    prepared = prepare_group_summary_action(project, bound)
    assert prepared.operation.instruction == "Report common themes and outliers."
    assert prepared.operation.input_columns == ["body"]
    assert prepared.operation.group_by == "bucket"
    assert prepared.operation.summary_column_name == "Overview"
    assert prepared.resolved["groups"][0]["source_row_ids"] == rows
    assert len(project.columns(sheet)) == 2
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_find_actual_arguments_freeze_scan_and_names_without_publication(
    source_project,
):
    project, sheet, rows = source_project
    bound = _bound(
        find_renamed,
        {"document": "body", "selected_model": "anthropic/claude-haiku-4-5"},
        sheet,
        key="find",
        names={"match": "Occurrence"},
    )
    prepared = prepare_find_action(project, bound)
    assert prepared.operation.source_column == "body"
    assert prepared.operation.instruction == "Find every mention of policy."
    assert prepared.operation.output_names == {"match": "Occurrence"}
    assert [source.row_id for source in prepared.scan.sources] == rows
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1


def forged_summary(
    params: SummaryArgs, summarizer: GroupSummarizer
) -> PreparedGroupSummary:
    del params, summarizer
    return PreparedGroupSummary(2, 1)


def forged_find(params: FindingArgs, scanner: FindScanner) -> PreparedFind:
    del params, scanner
    return PreparedFind(2, 2)


@pytest.mark.parametrize("kind", ["summary", "find"])
def test_prepared_handles_must_belong_to_the_invocation(source_project, kind):
    project, sheet, _ = source_project
    if kind == "summary":
        handler, params, prepare = (
            forged_summary,
            {
                "content": ["body"],
                "bucket": "bucket",
                "chosen_model": "anthropic/claude-haiku-4-5",
            },
            prepare_group_summary_action,
        )
    else:
        handler, params, prepare = (
            forged_find,
            {"document": "body", "selected_model": "anthropic/claude-haiku-4-5"},
            prepare_find_action,
        )
    with pytest.raises(ValueError, match="invocation"):
        prepare(project, _bound(handler, params, sheet, key=kind))


def test_reused_summary_runs_with_actual_action_identity_and_names(source_project):
    from test_reduce_group_summary_executor import _summary_router

    project, sheet, rows = source_project
    bound = _bound(
        summarize_renamed,
        {
            "content": ["body"],
            "bucket": "bucket",
            "chosen_model": "anthropic/claude-haiku-4-5",
        },
        sheet,
        key="summary",
        names={"group": "Bucket", "rows": "Contributors", "summary": "Overview"},
    )
    router, adapter = _summary_router()
    result = run_typed_group_summary_action(project, "test", bound, router)
    if result.status == "needs_confirmation":
        bound = BoundTypedActionRequest.bind(
            bound.action,
            bound.request.model_copy(
                update={"confirmation": result.errors[0].details["promise_set_hash"]}
            ),
        )
        result = run_typed_group_summary_action(project, "test", bound, router)
    assert result.status == "completed", result.model_dump()
    assert len(adapter.requests) == 1
    assert (
        project.db.execute(
            "SELECT action_kind FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()[0]
        == "custom.summary"
    )
    output_sheet = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    assert {column["name"] for column in project.columns(output_sheet)} == {
        "Bucket",
        "Contributors",
        "Overview",
    }


@pytest.mark.parametrize(
    "change", [None, "actual_instruction", "queue_snapshot", "source_during_scan"]
)
def test_find_queue_pins_actual_author_arguments_before_model_calls(
    source_project, monkeypatch, change
):
    from dataclasses import replace

    from frisket.actions import system
    from frisket.actions.core import ActionNamespace, ActionRegistry
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        run_action_run_job,
    )
    from frisket.engine.executor.find_action import (
        reserve_typed_find_action_job,
        run_typed_find_action_job,
    )
    from frisket.engine.store.receipts import ReceiptStore
    from test_map_find_runtime import _FindAdapter
    from frisket.ai.llm import ModelRouter

    project, sheet, rows = source_project
    actual_instruction = "Find every mention of policy."
    consent_coverage = ConsentCoverage("test:prepared-find", Decimal("0"))

    def authored_find(params: FindingArgs, scanner: FindScanner) -> PreparedFind:
        return scanner.prepare(
            params.document,
            options=FindOptions(
                model=params.selected_model, instruction=actual_instruction
            ),
        )

    bound = _bound(
        authored_find,
        {"document": "body", "selected_model": "anthropic/claude-haiku-4-5"},
        sheet,
        key="find",
        names={"match": "Occurrence"},
    )
    monkeypatch.setattr(
        system,
        "ACTION_REGISTRY",
        ActionRegistry([ActionNamespace("custom", actions=[bound.action.definition])]),
    )
    refusal = reserve_typed_find_action_job(
        project, "test", bound, consent_coverage=consent_coverage
    )
    assert refusal.status == "needs_confirmation"
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    bound = BoundTypedActionRequest.bind(
        bound.action,
        bound.request.model_copy(
            update={"confirmation": refusal.errors[0].details["promise_set_hash"]}
        ),
    )
    envelope = reserve_typed_find_action_job(
        project, "test", bound, consent_coverage=consent_coverage
    )
    assert isinstance(envelope, ActionJobEnvelope)
    admitted = envelope.resolved_snapshot
    assert admitted["operation"]["instruction"] == actual_instruction
    assert admitted["operation"]["source_column"] == "body"
    assert admitted["operation"]["output_names"] == {"match": "Occurrence"}
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
    if change == "actual_instruction":
        actual_instruction = "Find a different occurrence."
    elif change == "queue_snapshot":
        envelope = replace(
            envelope,
            resolved_snapshot={**admitted, "confirmation_hash": "different"},
        )

    class Adapter(_FindAdapter):
        async def complete(self, request, client):
            response = await super().complete(request, client)
            if change == "source_during_scan" and len(self.requests) == 1:
                body_column = project.db.execute(
                    "SELECT id FROM columns WHERE sheet_id=? AND name='body'",
                    (sheet,),
                ).fetchone()[0]
                replace_test_source_cell(
                    project,
                    row_id=rows[0],
                    column_id=body_column,
                    value="Changed during model call",
                )
            return response

    adapter = Adapter("unused", matches=[])
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    def execute(project, envelope):
        return run_typed_find_action_job(
            project, envelope, deps=ExecutorDeps(router=router)
        )

    result = run_action_run_job(
        project,
        {"action_job": envelope.to_json(), "job_id": 12},
        executor_lookup=lambda kind: execute if kind == "custom.find" else None,
    )
    terminal_receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    retained = next(
        item
        for item in terminal_receipt.evidence
        if item.ref.get("kind") == "find_admission"
    )
    assert retained.retention == "pinned"
    assert retained.ref["admission"] == admitted
    if change is None:
        assert result.status == "completed", result.model_dump()
        assert len(adapter.requests) == 2
        output_sheet = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert [column["name"] for column in project.columns(output_sheet)] == [
            "Occurrence"
        ]
        replay = reserve_typed_find_action_job(project, "test", bound)
        assert replay.receipt_id == result.receipt_id
        assert replay.status == "completed"
    else:
        assert result.status == "failed", result.model_dump()
        assert result.errors[0].code == (
            "action_job_snapshot_mismatch"
            if change == "queue_snapshot"
            else "source_changed"
        )
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        if change == "source_during_scan":
            assert len(adapter.requests) == 2
            assert (
                project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0]
                == 2
            )
            assert not any(
                item.ref.get("external_effect") == "none" for item in receipt.evidence
            )
        else:
            assert adapter.requests == []
            assert receipt.provider_use == []
            assert any(
                item.ref.get("external_effect") == "none" for item in receipt.evidence
            )
