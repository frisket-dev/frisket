"""Unit proofs for returned-call delivery; public host tests own authorization."""

import asyncio
import copy
from contextlib import closing
from types import SimpleNamespace

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse
from frisket.engine.executor.agent_model_calls import AccountedAgentRouter
from frisket.engine.store import Project
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
def agent_context(tmp_path):
    with closing(Project.create(tmp_path / "agent-call.frisket")) as project:
        sheet = project.add_sheet("data")
        [row_id] = project.add_rows(sheet, [{}], {})
        op_id = project.append_op("research.answer", {}, label="Agent call")
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
