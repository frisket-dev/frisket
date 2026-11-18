"""Unit proofs for returned-call delivery; public host tests own authorization."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
import copy
from types import SimpleNamespace

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse
from frisket.engine.executor.agent_model_calls import AccountedAgentRouter
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import mint_direct_effect_attempt
from frisket.execution.attempt import ATTEMPT_EXTRA


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("later call failed"), asyncio.CancelledError()]
)
async def test_returned_fact_precedes_later_failure_and_retains_its_identity(
    monkeypatch, failure, agent_context
):
    persisted = []

    class Store:
        def __init__(self, project):
            pass

        def write_returned_call_accounting(self, run_id, batch, **kwargs):
            persisted.extend(copy.deepcopy(batch))

    monkeypatch.setattr(
        "frisket.engine.executor.agent_model_calls.RunResultStore", Store
    )

    class Router:
        calls = 0

        async def complete_transport(self, request, **kwargs):
            self.calls += 1
            if self.calls == 2:
                assert len(persisted) == 1
                raise failure
            return LLMResponse(
                data=None,
                content="returned",
                model=request.model,
                provider="anthropic",
                tokens_in=3,
                tokens_out=2,
                cost=0.01,
            )

    accounting = {}
    ctx = agent_context
    router = AccountedAgentRouter(Router(), ctx, "anthropic/model", accounting)
    request = LLMRequest(model="anthropic/model", messages=[])
    response = await router.complete_transport(request)
    with pytest.raises(type(failure)):
        await router.complete_transport(request)
    assert accounting[ctx.extras["row_id"]]["cost"] == 0.01
    assert (
        accounting[ctx.extras["row_id"]]["model_calls"] == persisted[0]["model_calls"]
    )
    assert accounting[ctx.extras["row_id"]]["model_calls"][0]["id"].startswith(
        "agent_call_"
    )
    unseen = object()
    error = RuntimeError("structured failure")
    error.wire_calls = [response, unseen]
    router.retain_unrecorded_error_calls(error)
    assert error.wire_calls == [unseen]


@pytest.mark.asyncio
async def test_model_mismatch_refuses_before_transport():
    class Router:
        async def complete_transport(self, request, **kwargs):
            pytest.fail("A different model must not be contacted")

    router = AccountedAgentRouter(Router(), None, "anthropic/admitted", {})
    with pytest.raises(ValueError, match="admitted model"):
        await router.complete_transport(
            LLMRequest(model="anthropic/other", messages=[])
        )


@pytest.fixture
def agent_context(make_project):
    project = make_project("agent-call")
    sheet = project.add_sheet("data")
    [row_id] = project.add_rows(sheet, [{}], {})
    op_id = project.append_change("research.answer", {}, label="Agent call")
    run_id = RunResultStore(project).start_run(
        op_id, sheet, "research.answer", total_rows=1, row_ids=[row_id]
    )
    attempt = mint_direct_effect_attempt(
        project, action_kind="research.answer", run_id=run_id, scope=(row_id,)
    )
    yield SimpleNamespace(
        project=project,
        extras={"run_id": run_id, "row_id": row_id, ATTEMPT_EXTRA: attempt},
    )


@pytest.mark.asyncio
async def test_retry_then_agent_schema_repair_failure_records_each_leaf_once(
    monkeypatch, agent_context
):
    from frisket.ai.llm import LLMError, ModelRouter
    from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
    from frisket.execution.dispatch_control import dispatch_for_context
    from tests.ai.test_retry_returned_usage import ReturnedAdapter

    persisted = []

    class Store:
        def __init__(self, project):
            pass

        def write_returned_call_accounting(self, run_id, batch, **kwargs):
            persisted.extend(copy.deepcopy(batch))

    monkeypatch.setattr(
        "frisket.engine.executor.agent_model_calls.RunResultStore", Store
    )
    adapter = ReturnedAdapter(["paid_error", "invalid", "paid_error", "paid_error"])
    inner = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test-key"},
        cache=None,
        max_retries=1,
    )
    inner._adapters["anthropic"] = adapter
    ctx = agent_context
    accounting = {}
    router = AccountedAgentRouter(
        inner.for_execution(dispatch_for_context(ctx)),
        ctx,
        "anthropic/test",
        accounting,
    )
    with pytest.raises(LLMError) as caught:
        await StructuredCompleter(router).complete(
            StructuredRequest(
                model="anthropic/test",
                messages=[{"role": "user", "content": "answer"}],
                schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
                repair_attempts=1,
            )
        )
    facts = [call for batch in persisted for call in batch["model_calls"]]
    assert adapter.calls == len(facts) == len({call["id"] for call in facts}) == 4
    assert accounting[ctx.extras["row_id"]]["cost"] == pytest.approx(0.1)
    router.retain_unrecorded_error_calls(caught.value)
    assert caught.value.wire_calls == []
    await inner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap", [False, True])
async def test_error_handoff_preserves_current_transport_and_prior_agent_facts(
    agent_context, overlap
):
    from frisket.ai.models.metadata import ModelCallMeta
    from frisket.ops.integrations.hosted_error import HostedEngineError

    ctx = agent_context
    prior = ModelCallMeta.provider_call(
        capability="llm.complete",
        engine="anthropic/model",
        provider="anthropic",
        provider_kind="chat_api",
        model_ids=["model"],
        credential_source="local",
        provider_reported_cost_usd=0.01,
        provider_cost_usd=0.01,
        duration_ms=1,
        units={"tokens_in": 3, "tokens_out": 2},
        cost_source="provider",
        warnings=[],
    ).as_dict()
    current = {
        **prior,
        "id": prior["id"] if overlap else "current-transport-call",
        "provider_cost_usd": 0.02,
        "units": {"tokens_in": 5, "tokens_out": 4},
    }
    accounting = {
        ctx.extras["row_id"]: {
            "model_calls": [prior],
            "cost": 0.01,
            "tokens_in": 3,
            "tokens_out": 2,
        }
    }
    router = AccountedAgentRouter(None, ctx, "anthropic/model", accounting)
    error = HostedEngineError(
        "provider_failed",
        "Current provider call failed",
        accounting={
            "model_calls": [current],
            "cost": 0.02,
            "tokens_in": 5,
            "tokens_out": 4,
            "execution_receipts": [{"id": "receipt"}],
        },
    )
    router.retain_unrecorded_error_calls(error)
    assert error.accounting["model_calls"] == (
        [current] if overlap else [current, prior]
    )
    assert error.accounting["cost"] == pytest.approx(0.02 if overlap else 0.03)
    assert error.accounting["tokens_in"] == (5 if overlap else 8)
    assert error.accounting["execution_receipts"] == [{"id": "receipt"}]
