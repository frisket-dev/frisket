from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from frisket.execution.dispatch_control import STANDALONE
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMError, LLMRequest, SchemaViolation


def req(model: str = "mock/model", schema: dict | None = None) -> LLMRequest:
    return LLMRequest(
        model=model, messages=[{"role": "user", "content": "hi"}], schema=schema
    )


# ---------------------------------------------------------------------------
# The class-level flip itself.
# ---------------------------------------------------------------------------


def test_schema_violation_is_never_retryable_at_construction():
    """Every SchemaViolation instance is retryable=False, unconditionally --
    no constructor argument can override it (there is none exposed), whether
    raised for an LLM-output fault or a plugin-structural one."""
    assert SchemaViolation("bad output").retryable is False
    assert SchemaViolation("bad output", raw_text="{oops").retryable is False
    assert (
        SchemaViolation("plugin-owned generated-map metadata is missing").retryable
        is False
    )


# ---------------------------------------------------------------------------
# A fake adapter for driving router._call_with_retry deterministically.
# ---------------------------------------------------------------------------


class _ScriptedFailThenSucceed:
    """First N calls raise the scripted exception; the next call succeeds."""

    def __init__(self, exc_factory, fail_times: int):
        self.exc_factory = exc_factory
        self.fail_times = fail_times
        self.calls = 0

    async def complete(self, request, client):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        from frisket.ai.llm.types import LLMResponse

        return LLMResponse(
            content="ok",
            data=None,
            tokens_in=1,
            tokens_out=1,
            cost=0.0,
            model=request.model,
        )


def _router_with(adapter, *, max_retries=3) -> ModelRouter:
    router = ModelRouter(keys={}, max_retries=max_retries)
    router._adapters["mock"] = adapter  # noqa: SLF001
    return router


# ---------------------------------------------------------------------------
# A retryable transport fault must be retried and recover. SchemaViolation can no
# longer demonstrate this post-flip (it's unconditionally retryable=False
# now) -- a generic retryable LLMError stands in for the mechanism
# SchemaViolation used to ride, pinning that the TRANSPORT RETRY MECHANISM
# itself (not SchemaViolation specifically) still recovers a transient fault.
# ---------------------------------------------------------------------------


def test_transport_retry_recovers_a_provably_pre_egress_connect_fault():
    """The generic transport-retry mechanism `_call_with_retry` implements
    still recovers a transient connection-establishment failure.  Unlike a
    malformed or lost provider response, ConnectError proves the request did
    not reach accepted work, so a second wire attempt cannot double-buy it."""
    adapter = _ScriptedFailThenSucceed(
        lambda: httpx.ConnectError(
            "connection refused",
            request=httpx.Request("POST", "https://provider.invalid/v1/complete"),
        ),
        fail_times=1,
    )
    router = _router_with(adapter, max_retries=3)

    async def run():
        return await router.for_execution(STANDALONE).complete(req())

    resp = asyncio.run(run())
    assert resp.content == "ok"
    assert adapter.calls == 2  # 1 failure + 1 recovery -- retry happened


def test_retry_is_a_new_purchase_and_preserves_unknown_prior_outcome():
    adapter = _ScriptedFailThenSucceed(
        lambda: LLMError("response body was lost", status=502, retryable=True),
        fail_times=1,
    )
    events = []

    class Policy:
        def prepare(self, request, credential_source):
            return request

        def before_live(self, request, credential_source):
            events.append(("admit", None))

        def failed_live(self, request, error, credential_source):
            events.append(("failed", error))

        def after_live(self, request, response, credential_source):
            events.append(("returned", response))

    router = _router_with(adapter, max_retries=1)
    router.model_call_policy = Policy()

    async def run():
        try:
            assert (
                await router.for_execution(STANDALONE).complete(req())
            ).content == "ok"
        finally:
            await router.aclose()

    asyncio.run(run())
    assert adapter.calls == 2
    assert [event for event, _ in events] == ["admit", "failed", "admit", "returned"]
    assert events[1][1].post_egress_ambiguous is True
    assert events[3][1].tokens_in == 1


def test_schema_violation_is_never_retried_by_transport_regardless_of_max_retries():
    """Post-flip companion: a SchemaViolation is raised on the FIRST attempt,
    with zero retries and zero backoff sleep, no matter how large
    `max_retries` is -- proving the transport genuinely never blind-retries
    it anymore (not just "eventually gives up faster")."""
    adapter = _ScriptedFailThenSucceed(
        lambda: SchemaViolation("bad json", raw_text="{oops"),
        fail_times=1,  # would recover on retry #2 IF retried -- it must not be
    )
    router = _router_with(adapter, max_retries=5)

    async def run():
        t0 = time.perf_counter()
        with pytest.raises(SchemaViolation):
            await router.for_execution(STANDALONE).complete(req())
        return time.perf_counter() - t0

    elapsed = asyncio.run(run())
    assert adapter.calls == 1  # no retry attempted at all
    assert elapsed < 0.5  # no backoff sleep (min(2**attempt*0.5, 8.0) would show)


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize(
    "status, transport_kind", [(502, None), (429, None), (None, "connect")]
)
def test_explicit_ambiguous_provider_outcome_survives_retry_exhaustion(
    retryable, status, transport_kind
):
    adapter = _ScriptedFailThenSucceed(
        lambda: LLMError(
            "Provider outcome needs reconciliation",
            status=status,
            retryable=retryable,
            transport_kind=transport_kind,
            post_egress_ambiguous=True,
        ),
        fail_times=1,
    )
    router = _router_with(adapter, max_retries=0)

    async def run():
        try:
            with pytest.raises(LLMError) as caught:
                await router.for_execution(STANDALONE).complete(req())
            assert caught.value.post_egress_ambiguous is True
            assert caught.value.retryable is retryable
            assert caught.value.status == status
        finally:
            await router.aclose()

    asyncio.run(run())
    assert adapter.calls == 1


@pytest.mark.parametrize(
    "exception_class, expected_kind",
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.WriteTimeout, "timeout"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadError, "read"),
        (httpx.WriteError, "write"),
        (httpx.RemoteProtocolError, "protocol"),
    ],
)
def test_provider_transport_diagnostics_keep_kind_at_retry_limit(
    exception_class, expected_kind
):
    adapter = _ScriptedFailThenSucceed(lambda: exception_class(""), fail_times=1)
    router = _router_with(adapter, max_retries=0)

    async def run():
        try:
            with pytest.raises(LLMError) as caught:
                await router.for_execution(STANDALONE).complete(req())
            assert caught.value.transport_kind == expected_kind
            assert caught.value.post_egress_ambiguous is True
        finally:
            await router.aclose()

    asyncio.run(run())
    assert adapter.calls == 1


def test_wrapped_timeout_keeps_finite_transport_kind():
    original = LLMError("Provider transport failed", retryable=True)
    original.__cause__ = httpx.ReadTimeout("")
    safe = ModelRouter._safe_llm_error(original, ())
    assert safe.transport_kind == "timeout"
