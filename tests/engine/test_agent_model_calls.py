"""Unit proofs for returned-call delivery; public host tests own authorization."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse
from frisket.engine.executor.agent_model_calls import AccountedAgentRouter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("later call failed"), asyncio.CancelledError()]
)
async def test_returned_fact_precedes_later_failure_and_retains_its_identity(
    monkeypatch, failure
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
    # This unit test only checks returned-fact delivery. Actual admission is
    # covered through the public action host, not manufactured here.
    monkeypatch.setattr(
        "frisket.engine.executor.agent_model_calls.require_agent_attempt",
        lambda ctx: SimpleNamespace(attempt_id="unit-accounting-writer"),
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
    ctx = SimpleNamespace(project=object(), extras={"run_id": 1, "row_id": 2})
    router = AccountedAgentRouter(Router(), ctx, "anthropic/model", accounting)
    request = LLMRequest(model="anthropic/model", messages=[])
    response = await router.complete_transport(request)
    with pytest.raises(type(failure)):
        await router.complete_transport(request)
    assert accounting[2]["cost"] == 0.01
    assert accounting[2]["model_calls"] == persisted[0]["model_calls"]
    assert accounting[2]["model_calls"][0]["id"].startswith("agent_call_")
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
