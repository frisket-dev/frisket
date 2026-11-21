"""Sync local batches and async providers obey the same purchase lifecycle."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from frisket.execution.purchases import (
    PurchaseAdmission,
    PurchaseObservation,
    PurchaseRecordingError,
    PurchaseRequest,
    execute_purchase,
    execute_purchase_sync,
)


def run_purchase(sync, invoke, policy, dispatch):
    request = PurchaseRequest("test", "embedding", "test/model", "platform_key", {})
    kwargs = {
        "policy": policy,
        "dispatch": dispatch,
        "observe": lambda result: PurchaseObservation(result, {"items": len(result)}),
    }
    if sync:
        return execute_purchase_sync(request, invoke=invoke, **kwargs)

    async def async_invoke():
        return invoke()

    return asyncio.run(execute_purchase(request, invoke=async_invoke, **kwargs))


@pytest.mark.parametrize("sync", [False, True])
def test_returned_usage_is_recorded_even_when_execution_closed(sync):
    closed = False
    observed = []

    def check():
        if closed:
            raise asyncio.CancelledError

    def invoke():
        nonlocal closed
        closed = True
        return [[1, 2, 3]]

    policy = SimpleNamespace(
        admit_purchase=lambda request: PurchaseAdmission("metered", "purchase-1"),
        record_purchase=lambda request, admission, observation: observed.append(
            observation
        ),
    )
    result = run_purchase(sync, invoke, policy, SimpleNamespace(check=check))
    assert result == [[1, 2, 3]]
    assert observed == [PurchaseObservation(result, {"items": 1})]


@pytest.mark.parametrize("sync", [False, True])
def test_record_failure_carries_result_and_cannot_look_like_transport_retry(sync):
    calls = []
    result = [[1, 2, 3]]
    error = httpx.ReadError("accounting write failed")

    def invoke():
        calls.append(1)
        return result

    def record(*args):
        raise error

    policy = SimpleNamespace(
        admit_purchase=lambda request: PurchaseAdmission("metered", "purchase-1"),
        record_purchase=record,
    )
    with pytest.raises(PurchaseRecordingError) as caught:
        run_purchase(sync, invoke, policy, SimpleNamespace(check=lambda: None))
    assert calls == [1]
    assert caught.value.__cause__ is error
    assert caught.value.provider_result is result
    assert caught.value.observation.usage == {"items": 1}


@pytest.mark.parametrize("sync", [False, True])
def test_cancellation_after_admission_is_definitely_not_dispatched(sync):
    closed = False
    failed = []

    def check():
        if closed:
            raise asyncio.CancelledError

    def admit(request):
        nonlocal closed
        closed = True
        return PurchaseAdmission("metered", "purchase-1")

    policy = SimpleNamespace(
        admit_purchase=admit,
        fail_purchase=lambda request, admission, error: failed.append(error),
    )
    with pytest.raises(asyncio.CancelledError):
        run_purchase(
            sync,
            lambda: pytest.fail("must not send"),
            policy,
            SimpleNamespace(check=check),
        )
    assert len(failed) == 1
    assert failed[0].purchase_dispatched is False


# invariant: public router classes must not expose raw provider adapter getters
def test_public_router_does_not_hand_out_unaccounted_provider_adapters():
    from frisket.ai.llm.router import ModelRouter
    from frisket.ai.llm.types import ModelRouterTransport
    from frisket.operability.trace import TracingRouter

    for owner in (ModelRouter, ModelRouterTransport, TracingRouter):
        for retired in ("adapter_for", "adapter_for_model", "resolve_local_model"):
            assert not hasattr(owner, retired), (owner.__name__, retired)


@pytest.mark.parametrize("sync", [False, True])
def test_observation_failure_notifies_policy_with_known_llm_response(sync):
    from frisket.ai.llm import LLMResponse
    from frisket.execution.dispatch_control import STANDALONE

    result = LLMResponse(
        content="answer",
        data=None,
        model="openai/test",
        tokens_in=3,
        tokens_out=2,
        cost=0.01,
    )
    failed = []
    policy = SimpleNamespace(
        admit_purchase=lambda request: PurchaseAdmission("metered", "p1"),
        fail_purchase=lambda request, admission, error: failed.append(error),
    )

    def broken_observer(result):
        raise ValueError("observer bug")

    async def async_invoke():
        return result

    request = PurchaseRequest(
        "openai", "llm.complete", "openai/test", "platform_key", {}
    )
    with pytest.raises(PurchaseRecordingError) as caught:
        kwargs = dict(policy=policy, dispatch=STANDALONE, observe=broken_observer)
        if sync:
            execute_purchase_sync(request, invoke=lambda: result, **kwargs)
        else:
            asyncio.run(execute_purchase(request, invoke=async_invoke, **kwargs))
    assert failed == [caught.value]
    assert caught.value.observation.usage == {"tokens_in": 3, "tokens_out": 2}
    assert caught.value.observation.provider_cost_micro == 10_000
    assert caught.value.provider_result is result


@pytest.mark.parametrize("sync", [False, True])
def test_error_observer_failure_still_closes_purchase(sync):
    from frisket.execution.dispatch_control import STANDALONE

    original = RuntimeError("provider failed")
    observer_error = ValueError("observer bug")
    failed = []
    policy = SimpleNamespace(
        admit_purchase=lambda request: PurchaseAdmission("metered", "p1"),
        fail_purchase=lambda request, admission, error: failed.append(error),
    )

    def invoke():
        raise original

    async def async_invoke():
        return invoke()

    def observe_error(error):
        raise observer_error

    request = PurchaseRequest(
        "openai", "llm.complete", "openai/test", "platform_key", {}
    )
    with pytest.raises(RuntimeError) as caught:
        kwargs = dict(
            policy=policy,
            dispatch=STANDALONE,
            observe=lambda x: x,
            observe_error=observe_error,
        )
        if sync:
            execute_purchase_sync(request, invoke=invoke, **kwargs)
        else:
            asyncio.run(execute_purchase(request, invoke=async_invoke, **kwargs))
    assert caught.value is original
    assert failed == [original]
    assert original.purchase_observation_error is observer_error


def test_router_requires_composition_to_choose_policy():
    from frisket.ai.llm import ModelRouter

    with pytest.raises(TypeError, match="model_call_policy"):
        ModelRouter(use_env_keys=False)


@pytest.mark.parametrize("sync", [False, True])
def test_one_failed_invocation_records_one_observation_and_retains_it_on_write_error(
    sync,
):
    from frisket.execution.dispatch_control import STANDALONE

    original = ValueError("unusable provider result")
    observation = PurchaseObservation({"returned": True}, {"tokens_in": 17}, 42)
    recorded, failures = [], []

    def record(request, admission, value):
        recorded.append(value)
        raise RuntimeError("accounting temporarily unavailable")

    policy = SimpleNamespace(
        admit_purchase=lambda request: PurchaseAdmission("metered", "p1"),
        record_purchase=record,
        fail_purchase=lambda request, admission, error: failures.append(error),
    )

    def invoke():
        raise original

    async def async_invoke():
        return invoke()

    request = PurchaseRequest("provider", "llm.complete", "model", "platform_key", {})
    kwargs = dict(
        policy=policy,
        dispatch=STANDALONE,
        observe=lambda result: pytest.fail("No usable result"),
        observe_error=lambda error: observation,
    )
    with pytest.raises(ValueError) as caught:
        if sync:
            execute_purchase_sync(request, invoke=invoke, **kwargs)
        else:
            asyncio.run(execute_purchase(request, invoke=async_invoke, **kwargs))
    assert caught.value is original
    assert recorded == [observation]
    assert failures == [original]
    assert failures[0].observation is observation
