"""Real typed row dispatch preserves unusable reasons and optional inputs."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
from pydantic import BaseModel
import pytest
import test_execution_lifecycle_recovery as recovery

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Row,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import MapRunner
from frisket.engine.runner.row_inputs import read_source_page
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from runner_test_helpers import run_with_output_claim
from test_result_generation_store import _start_claimed_run, _declare, _write, _seal
from frisket.engine.store.transactions import write_transaction

work = recovery.work


class RequiredInput(ActionParams):
    source: ColumnRef[int]


class OptionalInput(ActionParams):
    source: ColumnRef[int | None]


class NullableInteger(ColumnRef[int | None]):
    pass


class InheritedOptionalInput(ActionParams):
    source: NullableInteger


class Output(BaseModel):
    answer: str


@pytest.mark.parametrize(
    "state", ["invalid", "pending", "failed", "not_completed", "null"]
)
@pytest.mark.parametrize("mode", ["required", "optional", "inherited_optional"])
def test_dispatch_distinguishes_required_unavailable_from_optional_input(
    make_project, state, mode
):
    required = mode == "required"
    project = make_project("availability")
    sheet = project.add_sheet("source")
    source = project.add_column(
        sheet, "source", type="integer", ai_generated=state != "invalid"
    )
    [row] = project.add_rows(
        sheet,
        [{"source": "bad integer"}] if state == "invalid" else [{}],
        {"source": source},
    )
    if state != "invalid":
        run = _start_claimed_run(
            project,
            sheet_id=sheet,
            output_column_id=source,
            row_ids=[row],
            label="upstream",
        )
        _declare(ResultGenerationStore(project), run, source, write_mode="create")
        if state != "pending":
            _write(
                project,
                run,
                [
                    {
                        "row_id": row,
                        "column_id": source,
                        "value": None,
                        "outcome": "empty"
                        if state == "null"
                        else state
                        if state == "not_completed"
                        else "model_error",
                        "publication_effect": "publish_null"
                        if state == "null"
                        else "publish_error",
                        "error": None if state == "null" else "upstream unavailable",
                        "error_code": None
                        if state == "null"
                        else "upstream_unavailable",
                    }
                ],
            )
            _seal(ResultGenerationStore(project), run, source)
    page = read_source_page(project, sheet, [source], [row])
    assert "validity" in page.columns[source][1][row]
    assert "validity" not in page.cell(sheet, source, row)[1]
    calls = []

    def required_handler(params: RequiredInput, row: Row) -> RowResult[Output]:
        calls.append(params.source.read(row))
        return RowResult(output=Output(answer="handled"))

    def optional_handler(params: OptionalInput, row: Row) -> RowResult[Output]:
        calls.append(params.source.read(row))
        return RowResult(output=Output(answer="handled"))

    def inherited_optional_handler(
        params: InheritedOptionalInput, row: Row
    ) -> RowResult[Output]:
        calls.append(params.source.read(row))
        return RowResult(output=Output(answer="handled"))

    registry = ActionRegistry(
        [
            ActionNamespace(
                "probe",
                actions=[
                    action(
                        name="availability",
                        title="Availability",
                        description="Check input availability",
                        category=ActionCategory.TEXT,
                        run=map_rows(
                            required_handler
                            if required
                            else inherited_optional_handler
                            if mode == "inherited_optional"
                            else optional_handler
                        ),
                    )
                ],
            )
        ]
    )
    request = ActionRequest(
        action_id="probe.availability",
        scope=SheetRows(sheet_id=sheet, row_ids=[row]),
        params={"source": "source"},
        idempotency_key="availability",
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(registry.get(request.action_id), request),
    )
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY, keys={}, use_env_keys=False
    )
    runner = MapRunner(
        project,
        router,
        authority=UnroutedOnlyAuthority(project),
        allow_action_lifecycle_only_programs=True,
    )
    progress = asyncio.run(
        run_with_output_claim(
            runner, plan.spec_dict(), program=plan.program, confirmed=True
        )
    )
    assert calls == ([] if required else [None])
    output = next(
        column
        for column in project.columns(sheet, include_hidden=True)
        if column["name"] == "answer"
    )
    values, refs = project.get_values_with_refs(sheet, output["id"], row_ids=[row])
    assert refs[row]["outcome"] == ("not_completed" if required else "completed")
    assert values[row] == (None if required else "handled")
    saved = project.db.execute(
        "SELECT outcome,error_code FROM results WHERE run_id=?", (progress.run_id,)
    ).fetchone()
    assert saved["outcome"] == ("not_completed" if required else "ok")
    if required:
        assert saved["error_code"] == "input_" + (
            "missing" if state == "null" else state
        )
    assert not project.db.execute(
        "SELECT 1 FROM model_calls WHERE run_id=?", (progress.run_id,)
    ).fetchone()
    asyncio.run(router.aclose())


@pytest.mark.parametrize("mixed", [False, True])
def test_model_does_not_buy_answer_when_one_required_source_is_invalid(work, mixed):
    project, sheet, source, _rows, calls, _adapter, run = work
    invalid = project.add_column(sheet, "invalid", type="integer")
    [row] = project.add_rows(
        sheet,
        [{"source": "usable context", "invalid": "bad integer"}],
        {"source": source, "invalid": invalid},
    )
    result = run(
        "map.ask",
        {
            "source": ["source", "invalid"] if mixed else ["invalid"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "describe",
        },
        output_names={"answer": "Availability answer"},
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row]},
    )
    assert calls == []
    assert result.run_id is not None
    saved = project.db.execute(
        "SELECT outcome,error_code FROM results WHERE run_id=?", (result.run_id,)
    ).fetchone()
    assert tuple(saved) == ("not_completed", "input_invalid")
    assert (
        project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()[0]
        == 0.0
    )
    assert not project.db.execute(
        "SELECT 1 FROM effect_checkpoints WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
        (result.run_id,),
    ).fetchone()


def test_unavailable_current_input_does_not_replace_a_returned_paid_checkpoint(
    make_project, monkeypatch
):
    import test_map_row_effect_checkpoint as effects

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = make_project("returned-unavailable")
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test"},
        cache_mode="off",
    )
    try:
        sheet = project.add_sheet("source")
        column = project.add_column(sheet, "text")
        project.add_rows(sheet, [{"text": "billable source"}], {"text": column})
        provider = effects._ReturnedProviderCall()
        router._adapters["anthropic"] = provider
        runner = effects._runner(project, router)
        spec, program = effects._bound(effects._spec(sheet))
        confirmed = effects._confirmed_spec(runner, spec, program)
        run_id, token = effects._prepare_claimed_run(runner, confirmed, program)

        def crash(*args, **kwargs):
            raise RuntimeError("injected after return")

        runner.run_store.write_results_in_transaction = crash
        with pytest.raises(RuntimeError, match="injected after return"):
            asyncio.run(
                runner.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=token,
                )
            )
        checkpoint = project.db.execute(
            "SELECT id,payload FROM effect_checkpoints WHERE family='row_effect'"
        ).fetchone()
        assert checkpoint is not None
        attempt = project.db.execute(
            "SELECT id FROM execution_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        effects._age_silent_attempt(project, attempt)
        with write_transaction(project.db):
            project.set_column_type(column, "integer")
        resumed = asyncio.run(
            effects._runner(project, router).run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=token,
            )
        )
        assert resumed.halted_code == "external_effect_reconciliation_required"
        assert provider.calls == 1
        retained = project.db.execute(
            "SELECT state,payload FROM effect_checkpoints WHERE id=?",
            (checkpoint["id"],),
        ).fetchone()
        assert tuple(retained) == ("returned", checkpoint["payload"])
        assert not project.db.execute(
            "SELECT 1 FROM results WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        asyncio.run(router.aclose())
        project.close()
