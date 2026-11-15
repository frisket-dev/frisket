"""Every returned retry purchase survives success, error, Stop and cache replay."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
from contextlib import closing

import pytest

from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter
from frisket.ai.llm.cache import ResponseCache
from frisket.ai.llm.structured import (
    FrisketRouterModel,
    StructuredCompleter,
    StructuredRequest,
)
from frisket.ai.llm.types import wire_responses
from frisket.ai.models.accounting import wire_accounting_meta
from frisket.execution.dispatch_control import STANDALONE
from frisket.operability.trace import RowTrace, TracingRouter


class ReturnedAdapter:
    api_key = "test-key"

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def complete(self, req, client):
        del client
        self.calls += 1
        outcome = next(self.outcomes)
        response = LLMResponse(
            content='{"answer":"ok"}',
            data={"answer": "ok"},
            tokens_in=self.calls * 10,
            tokens_out=self.calls,
            cost=self.calls / 100,
            model=req.model,
        )
        if outcome == "invalid":
            response.data = {"answer": 123}
            response.content = '{"answer":123}'
        if outcome == "unknown":
            raise LLMError(
                "unknown final response", retryable=False, post_egress_ambiguous=True
            )
        if outcome == "paid_error":
            error = LLMError("malformed completed response", retryable=True)
            error.wire_calls = [response]
            raise error
        return response


def router_for(adapter, **kwargs):
    router = ModelRouter(
        model_call_policy=kwargs.pop("model_call_policy", LOCAL_PURCHASE_POLICY),
        keys={"anthropic": "test-key"},
        max_retries=1,
        **kwargs,
    )
    router._adapters["anthropic"] = adapter
    return router


@pytest.mark.parametrize("final", ["paid_error", "unknown"])
def test_retry_error_keeps_each_known_returned_fact(final):
    async def run():
        adapter = ReturnedAdapter(["paid_error", final])
        router = router_for(adapter)
        with pytest.raises(LLMError) as caught:
            await router.for_execution(STANDALONE).complete_transport(
                LLMRequest(model="anthropic/test", messages=[])
            )
        calls = caught.value.wire_calls
        assert [(r.tokens_in, r.tokens_out) for r in calls] == (
            [(10, 1), (20, 2)] if final == "paid_error" else [(10, 1)]
        )
        assert caught.value.post_egress_ambiguous is (final == "unknown")
        assert all(
            r.provider == "anthropic" and r.duration_ms is not None for r in calls
        )
        accounting = wire_accounting_meta("anthropic/test", calls)
        assert len(accounting["model_calls"]) == len(calls)
        assert accounting["cost"] == pytest.approx(
            0.03 if final == "paid_error" else 0.01
        )
        await router.aclose()

    asyncio.run(run())


def test_retry_success_expands_once_and_cache_replay_has_no_historical_purchases(
    tmp_path,
):
    async def run():
        with closing(ResponseCache(tmp_path / "calls.sqlite")) as cache:
            adapter = ReturnedAdapter(["paid_error", "ok"])
            router = router_for(adapter, cache=cache, cache_mode="replay")
            row = RowTrace(row_id=1, trace_id="retries")
            bound = TracingRouter(router.for_execution(STANDALONE), row)
            req = LLMRequest(model="anthropic/test", messages=[])
            response = await bound.complete_transport(req)
            leaves = wire_responses([response])
            assert len(leaves) == 2
            assert wire_responses([*leaves, response]) == leaves
            accounting = wire_accounting_meta(req.model, [response])
            assert accounting["tokens_in"] == 30
            assert accounting["cost"] == pytest.approx(0.03)
            assert len(accounting["model_calls"]) == 2
            trace = bound.record()
            assert trace["calls"][0]["tokens_in"] == 30
            assert len(trace["calls"][0]["returned_calls"]) == 2
            cached = await bound.complete_transport(req)
            assert cached.cached and cached.prior_wire_calls == ()
            assert adapter.calls == 2
            replay = wire_accounting_meta(req.model, [cached])
            assert replay["cost"] == 0
            assert len(replay["model_calls"]) == 1
            assert replay["model_calls"][0]["cache"]["hit"]
            await router.aclose()

    asyncio.run(run())


def test_retry_stop_retains_known_response_without_a_second_dispatch():
    async def run():
        adapter = ReturnedAdapter(["paid_error"])
        router = router_for(adapter)

        class StopAfterReturn:
            def check(self):
                if adapter.calls:
                    raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError) as stopped:
            await router.for_execution(StopAfterReturn()).complete_transport(
                LLMRequest(model="anthropic/test", messages=[])
            )
        assert adapter.calls == 1
        assert [(r.tokens_in, r.tokens_out) for r in stopped.value.wire_calls] == [
            (10, 1)
        ]
        await router.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("cache_mode", ["replay", "fresh"])
@pytest.mark.parametrize("outcomes", [["ok"], ["paid_error", "ok"]])
def test_cache_write_failure_preserves_response_and_every_purchase(
    tmp_path, caplog, cache_mode, outcomes
):
    async def run():
        with closing(ResponseCache(tmp_path / "calls.sqlite")) as cache:
            # Reads still work, but a real SQLite cache write now fails.
            cache.db.execute("PRAGMA query_only = ON")
            adapter = ReturnedAdapter(outcomes)
            router = router_for(adapter, cache=cache, cache_mode=cache_mode)
            try:
                response = await router.for_execution(STANDALONE).complete_transport(
                    LLMRequest(model="anthropic/test", messages=[])
                )
                assert response.data == {"answer": "ok"}
                assert not response.cached
                assert adapter.calls == len(outcomes)
                accounting = wire_accounting_meta("anthropic/test", [response])
                assert len(accounting["model_calls"]) == len(outcomes)
                assert accounting["cost"] == pytest.approx(
                    0.01 if len(outcomes) == 1 else 0.03
                )
                assert cache.count() == 0
                assert not cache.db.in_transaction
            finally:
                await router.aclose()

    asyncio.run(run())
    assert "could not cache model response" in caplog.text


def test_structured_retry_and_repair_preserve_three_distinct_wires():
    async def run():
        adapter = ReturnedAdapter(["paid_error", "invalid", "ok"])
        router = router_for(adapter)
        result = await StructuredCompleter(router.for_execution(STANDALONE)).complete(
            StructuredRequest(
                model="anthropic/test",
                messages=[{"role": "user", "content": "answer"}],
                schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                repair_attempts=1,
            )
        )
        assert result.data == {"answer": "ok"}
        assert adapter.calls == len(result.wire_calls) == 3
        assert result.response.tokens_in == 60
        assert result.response.cost == pytest.approx(0.06)
        assert (
            len(
                wire_accounting_meta("anthropic/test", result.wire_calls)["model_calls"]
            )
            == 3
        )
        await router.aclose()

    asyncio.run(run())


def test_agent_shim_calls_each_returned_response_callback_once():
    from pydantic_ai import Agent

    async def run():
        adapter = ReturnedAdapter(["paid_error", "ok"])
        router = router_for(adapter)
        seen = []

        async def observe(response):
            seen.append(response)

        model = FrisketRouterModel(
            router.for_execution(STANDALONE), "anthropic/test", on_response=observe
        )
        await Agent(model, output_type=str).run("answer")
        assert len(seen) == len(model.wire_calls) == 2
        assert [(r.tokens_in, r.tokens_out) for r in seen] == [(10, 1), (20, 2)]
        await router.aclose()

    asyncio.run(run())
