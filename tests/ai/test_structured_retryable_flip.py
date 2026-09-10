from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMError, LLMRequest, SchemaViolation

SRC_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "frisket"


def req(model: str = "mock/model", schema: dict | None = None) -> LLMRequest:
    return LLMRequest(
        model=model, messages=[{"role": "user", "content": "hi"}], schema=schema
    )


# ---------------------------------------------------------------------------
# The grep guard is the flip's safety precondition.
# ---------------------------------------------------------------------------


def _llmrequest_schema_kw(call: ast.Call) -> ast.expr | None:
    """The `schema=` keyword VALUE node of an `LLMRequest(...)` call, or None
    if it isn't set (dataclass default `schema=None`)."""
    for kw in call.keywords:
        if kw.arg == "schema":
            return kw.value
    return None


def _is_llmrequest_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "LLMRequest"
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
        return await router.complete(req())

    resp = asyncio.run(run())
    assert resp.content == "ok"
    assert adapter.calls == 2  # 1 failure + 1 recovery -- retry happened


def test_post_egress_ambiguous_failure_is_not_blind_retried():
    """A retryable flag alone cannot prove another paid call is safe.

    The adapter accepted the request, then lost the response body.  A second
    identical call may buy the same completion twice, so the router must
    return the ambiguous failure to the row-effect checkpoint instead of
    hiding it behind an automatic retry.
    """
    adapter = _ScriptedFailThenSucceed(
        lambda: LLMError(
            "provider accepted work but the response body was lost",
            status=502,
            retryable=True,
        ),
        fail_times=1,
    )
    router = _router_with(adapter, max_retries=3)

    async def run():
        with pytest.raises(LLMError, match="response body was lost"):
            await router.complete(req())

    asyncio.run(run())
    assert adapter.calls == 1


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
            await router.complete(req())
        return time.perf_counter() - t0

    elapsed = asyncio.run(run())
    assert adapter.calls == 1  # no retry attempted at all
    assert elapsed < 0.5  # no backoff sleep (min(2**attempt*0.5, 8.0) would show)
