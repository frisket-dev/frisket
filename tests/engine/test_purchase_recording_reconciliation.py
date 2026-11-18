"""Accounting persistence failures cannot make a purchased row retryable."""

import asyncio
import pytest
from types import SimpleNamespace


from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import MapRunner
from frisket.execution.purchases import PurchaseAdmission
from runner_test_helpers import run_with_output_claim
from tests.execution_composition_helpers import open_attempt_authority


def test_accounting_failure_halts_without_automatic_repurchase_and_retains_usage(
    make_project,
):
    project = make_project("recording")
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
            allow_action_lifecycle_only_programs=True,
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


@pytest.mark.parametrize("engine", ["datalab", "docling"])
def test_native_document_accounting_failure_retains_one_returned_fact(
    make_project, monkeypatch, engine
):
    import json
    from frisket.execution.purchases import LocalPurchasePolicy
    from frisket.engine.store.runs import RunResultStore
    from tests.engine.test_document_convert import (
        _accepted_fact,
        _execute,
        _seed,
        _two_page_pdf,
    )
    from frisket.engine.store.media_blobs import media_cell

    calls = []

    class BrokenRecording(LocalPurchasePolicy):
        def record_purchase(self, request, admission, observation):
            raise RuntimeError("accounting database unavailable")

    async def datalab(*args, on_accepted, credential_source, **kwargs):
        calls.append(engine)
        on_accepted({"cost": None, "model_calls": [_accepted_fact(credential_source)]})
        return {"markdown": "returned", "page_count": 2}, 0.03

    async def sidecar(*args, **kwargs):
        calls.append(engine)
        return {"documents": [{"markdown": "returned", "ocr_used": [False, True]}]}

    monkeypatch.setenv("DATALAB_API_KEY", "fixture")
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway.test:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "fixture")
    monkeypatch.setattr("frisket.ops.integrations.datalab.datalab_convert", datalab)
    monkeypatch.setattr("frisket.ops._sidecar._sidecar_post_transport", sidecar)
    project = make_project("native-recording")
    router = ModelRouter(model_call_policy=BrokenRecording(), cache_mode="off")
    try:
        digest = project.add_blob(
            _two_page_pdf(), filename="file.pdf", mime="application/pdf"
        )
        sheet, _, _ = _seed(
            project,
            media_cell(digest, filename="file.pdf", mime="application/pdf"),
            coltype="file",
        )
        result = _execute(project, sheet, engine, router=router)
        assert result.status == "failed", result
        assert result.errors[0].code == "external_effect_reconciliation_required", (
            result.errors
        )
        assert calls == [engine]
        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == 1, facts
        assert json.loads(facts[0]["units"])["pages"] == 2
        if engine == "datalab":
            assert facts[0]["id"] == "datalab_accepted_ABC"
            assert facts[0]["provider_cost_usd"] == 0.03
    finally:
        asyncio.run(router.aclose())
        project.close()
