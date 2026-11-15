"""Accounting persistence failures cannot make a purchased row retryable."""

import asyncio
from types import SimpleNamespace


from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.execution.purchases import PurchaseAdmission
from runner_test_helpers import run_with_output_claim
from tests.execution_composition_helpers import open_attempt_authority


def test_accounting_failure_halts_without_automatic_repurchase_and_retains_usage(
    tmp_path,
):
    project = Project.create(tmp_path / "recording.frisket")
    calls, failures = [], []

    async def complete(request, client):
        calls.append(request)
        return LLMResponse(
            content='{"answer":"Ada"}',
            data={"answer": "Ada"},
            model=request.model,
            tokens_in=10,
            tokens_out=3,
            cost=0.01,
        )

    def record(*args):
        if len(calls) == 2:
            raise RuntimeError("accounting store offline")

    policy = SimpleNamespace(
        prepare=lambda req, source: req,
        admit_purchase=lambda req: PurchaseAdmission("metered", "purchase-1"),
        record_purchase=record,
        fail_purchase=lambda request, admission, error: failures.append(error),
    )
    router = ModelRouter(
        keys={"openai": "fixture"},
        use_env_keys=False,
        model_call_policy=policy,
        cache_mode="off",
        max_retries=0,
    )
    router._adapters["openai"] = SimpleNamespace(complete=complete)
    try:
        sheet = project.add_sheet("Input")
        column = project.add_column(sheet, "text", type="text")
        project.add_rows(sheet, [{"text": "Ada"}, {"text": "Grace"}], {"text": column})
        request = ActionRequest(
            action_id="map.ask",
            scope=SheetRows(sheet_id=sheet),
            params={
                "source": ["text"],
                "question": "Who?",
                "model": "openai/gpt-4.1-mini",
            },
            output_names={"answer": "answer"},
            idempotency_key="recording-test",
        )
        plan = build_typed_map_rows_plan(
            project,
            BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request),
        )
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=open_attempt_authority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        from frisket.engine.runner.validation import ClaimsGate

        spec = plan.spec_dict()
        try:
            first = asyncio.run(
                run_with_output_claim(
                    runner, spec, program=plan.program, confirmed=True
                )
            )
        except ClaimsGate as gate:
            spec["consented_promise_set_hash"] = gate.promise_set_hash
            first = asyncio.run(
                run_with_output_claim(
                    runner, spec, program=plan.program, confirmed=True
                )
            )
        assert first.halted_code == "external_effect_reconciliation_required"
        assert len(calls) == 2
        assert len(failures) == 1
        assert failures[0].provider_result.data == {"answer": "Ada"}
        assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 2
        # Same-run recovery cannot re-open a sealed Change. A deliberate fresh
        # action remains allowed to purchase work again by user choice.
        from frisket.engine.store.result_generations import GenerationSealedError
        import pytest

        with pytest.raises(GenerationSealedError):
            asyncio.run(
                run_with_output_claim(
                    runner,
                    spec,
                    program=plan.program,
                    confirmed=True,
                    resume_run_id=first.run_id,
                )
            )
        assert len(calls) == 2
    finally:
        asyncio.run(router.aclose())
        project.close()
