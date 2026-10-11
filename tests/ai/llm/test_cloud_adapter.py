from __future__ import annotations

import asyncio
from typing import Any

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ai.models.accounting import wire_accounting_meta
from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionRefused,
    CloudLLMExecutionBinding,
    CloudPayerSubject,
    cloud_llm_execution_scope,
)
from frisket.contracts.http.cloud_connection import (
    CloudCapabilityRoute,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMToolCall,
    CloudLLMUsage,
    CloudTokenPrice,
)


MODEL = "anthropic/claude-sonnet-4-5"
OTHER_MODEL = "anthropic/claude-haiku-4-5"


def _attempt() -> CloudExecutionAttempt:
    return CloudExecutionAttempt(
        request_id="durable-llm-request-1",
        payer=CloudPayerSubject(
            user_id=7,
            account_id=41,
            lane="frisket_credits",
            provider="anthropic",
            engine=MODEL,
            route_revision="route-revision-1",
        ),
        quote_authority=None,
    )


def _response(
    *, account_id: int = 41, output_limited: bool = True
) -> CloudExecutionResponse:
    return CloudExecutionResponse(
        execution_id="cloud-execution-1",
        status="completed",
        route=CloudCapabilityRoute(
            capability="llm.complete",
            engine=MODEL,
            provider="anthropic",
            lane="frisket_credits",
            route_revision="route-revision-1",
            ready=True,
            blocker=None,
            price=CloudTokenPrice(
                kind="tokens",
                input_micro_per_million_tokens=3_000_000,
                output_micro_per_million_tokens=15_000_000,
            ),
            options=CloudLLMOptionSupport(
                kind="llm",
                structured_output=True,
                tools=True,
                vision=True,
                max_output_tokens=8192,
            ),
        ),
        result=CloudLLMResult(
            content='{"answer":"Ada"}',
            data={"answer": "Ada"},
            tokens_in=23,
            tokens_out=7,
            cost=0.012,
            model=MODEL,
            cost_source="pricing_data",
            cached=False,
            provider="anthropic",
            credential_source="platform_key",
            raw={"stop_reason": "tool_use"},
            tool_calls=[
                CloudLLMToolCall(
                    name="lookup",
                    args={"person": "Ada"},
                    id="tool-1",
                    provider_details={"signature": "provider-proof"},
                )
            ],
            output_tool_call=CloudLLMToolCall(
                name="emit",
                args={"answer": "Ada"},
                id="output-1",
                provider_details={"signature": "output-proof"},
            ),
            duration_ms=321,
            output_limited=output_limited,
        ),
        receipt=CloudExecutionReceipt(
            user_id=7,
            account_id=account_id,
            lane="frisket_credits",
            provider="anthropic",
            engine=MODEL,
            charge_micro=25_000,
            provider_cost_micro=12_000,
            usage=CloudLLMUsage(
                kind="llm.complete",
                tokens_in=23,
                tokens_out=7,
            ),
        ),
        error=None,
    )


class _CloudTransport:
    def __init__(self, response: CloudExecutionResponse | None = None) -> None:
        self.response = response or _response()
        self.calls: list[tuple[CloudExecutionAttempt, Any]] = []

    async def execute_llm(self, attempt: CloudExecutionAttempt, request: Any):
        self.calls.append((attempt, request))
        return self.response


class _DirectAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest, _client: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="direct",
            data=None,
            tokens_in=1,
            tokens_out=1,
            cost=0.001,
            model=request.model,
        )


def _router(direct: _DirectAdapter | None = None) -> ModelRouter:
    router = ModelRouter(use_env_keys=False, cache_mode="off", max_retries=0)
    if direct is not None:
        router._adapters["anthropic"] = direct  # noqa: SLF001 - coexistence proof
    return router


def test_cloud_adapter_round_trips_request_result_and_receipt_accounting():
    async def scenario() -> None:
        transport = _CloudTransport()
        attempt = _attempt()
        router = _router()
        router.register_cloud_adapter(MODEL)
        request = LLMRequest(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Return one name."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Who wrote the first algorithm?"},
                        {"type": "image", "media_type": "image/png", "data": "AA=="},
                    ],
                },
            ],
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
            max_tokens=777,
            temperature=0.25,
            params={"top_p": 0.8},
            mechanism="native_tool",
        )
        try:
            with cloud_llm_execution_scope(
                CloudLLMExecutionBinding(transport, attempt)
            ):
                result = await router.complete_transport(request)
        finally:
            await router.aclose()

        [(seen_attempt, wire)] = transport.calls
        assert seen_attempt is attempt
        assert wire.model == MODEL
        assert wire.messages == request.messages
        assert wire.schema_ == request.schema
        assert wire.max_tokens == 777
        assert wire.temperature == 0.25
        assert wire.params == {"top_p": 0.8}
        assert wire.mechanism == "native_tool"
        assert wire.tools is None
        assert wire.reasoning_policy is None

        assert result.content == '{"answer":"Ada"}'
        assert result.data == {"answer": "Ada"}
        assert result.tokens_in == 23
        assert result.tokens_out == 7
        assert result.cost == 0.012
        assert result.cost_source == "pricing_data"
        assert result.model == MODEL
        assert result.cached is False
        assert result.provider == "anthropic"
        assert result.credential_source == "platform_key"
        assert result.raw == {"stop_reason": "tool_use"}
        assert result.tool_calls == [
            {
                "name": "lookup",
                "args": {"person": "Ada"},
                "id": "tool-1",
                "provider_details": {"signature": "provider-proof"},
            }
        ]
        assert result.output_tool_call == {
            "name": "emit",
            "args": {"answer": "Ada"},
            "id": "output-1",
            "provider_details": {"signature": "output-proof"},
        }
        assert result.output_limited is True

        accounting = result.actual_accounting
        assert accounting is not None
        assert accounting.cost == 0.025
        [receipt] = accounting.receipts
        [model_call] = accounting.model_calls
        assert receipt["account_id"] == 41
        assert receipt["charge_micro"] == 25_000
        assert model_call["id"] == "cloud-execution-1"
        assert model_call["credential_source"] == "platform_key"
        assert model_call["provider_cost_usd"] == 0.012
        assert model_call["units"]["cloud_charge_micro"] == 25_000
        durable = wire_accounting_meta(MODEL, [result])
        assert durable["cost"] == 0.025
        assert durable["model_calls"] == [model_call]
        assert durable["execution_receipts"] == [receipt]

    asyncio.run(scenario())


def test_cloud_adapter_preserves_tools_and_provider_details():
    async def scenario() -> None:
        transport = _CloudTransport()
        attempt = _attempt()
        router = _router()
        router.register_cloud_adapter(MODEL)
        request = LLMRequest(
            model=MODEL,
            messages=[{"role": "user", "content": "Look up Ada."}],
            tools=[
                {
                    "name": "lookup",
                    "description": "Look up a person",
                    "parameters": {
                        "type": "object",
                        "properties": {"person": {"type": "string"}},
                        "required": ["person"],
                    },
                }
            ],
        )
        try:
            with cloud_llm_execution_scope(
                CloudLLMExecutionBinding(transport, attempt)
            ):
                result = await router.complete_transport(request)
        finally:
            await router.aclose()

        [(_seen_attempt, wire)] = transport.calls
        assert wire.schema_ is None
        assert wire.tools == request.tools
        assert result.tool_calls is not None
        assert result.tool_calls[0]["provider_details"] == {
            "signature": "provider-proof"
        }

    asyncio.run(scenario())


def test_public_structured_completion_preserves_cloud_receipt():
    async def scenario() -> None:
        transport = _CloudTransport(_response(output_limited=False))
        attempt = _attempt()
        router = _router()
        router.register_cloud_adapter(MODEL)
        try:
            with cloud_llm_execution_scope(
                CloudLLMExecutionBinding(transport, attempt)
            ):
                result = await router.complete(
                    LLMRequest(
                        model=MODEL,
                        messages=[{"role": "user", "content": "Name the person."}],
                        schema={
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                        },
                        mechanism="native_tool",
                    )
                )
        finally:
            await router.aclose()

        assert result.data == {"answer": "Ada"}
        assert result.actual_accounting is not None
        assert result.actual_accounting.cost == 0.025
        assert [call["id"] for call in result.actual_accounting.model_calls] == [
            "cloud-execution-1"
        ]
        assert len(result.actual_accounting.receipts) == 1

    asyncio.run(scenario())


def test_cloud_registration_is_exact_model_and_never_falls_back_when_unbound():
    async def scenario() -> None:
        direct = _DirectAdapter()
        router = _router(direct)
        router.register_cloud_adapter(MODEL)
        try:
            assert router.adapter_for_model(MODEL) is not direct
            assert router.adapter_for_model(OTHER_MODEL) is direct
            assert router.adapter_for("anthropic") is direct
            with pytest.raises(CloudExecutionRefused) as error:
                await router.complete_transport(
                    LLMRequest(
                        model=MODEL,
                        messages=[{"role": "user", "content": "hello"}],
                    )
                )
            assert error.value.code == "cloud_execution_unbound"
            assert direct.calls == 0
        finally:
            await router.aclose()

    asyncio.run(scenario())


def test_cloud_registration_rejects_duplicates_and_invalid_model_ids():
    router = _router()
    router.register_cloud_adapter(MODEL)

    with pytest.raises(ValueError, match="already registered"):
        router.register_cloud_adapter(MODEL)
    for invalid in ("anthropic", "anthropic/", "/model", " anthropic/model"):
        with pytest.raises(ValueError, match="qualified"):
            router.register_cloud_adapter(invalid)


def test_cloud_adapter_refuses_receipt_from_another_account():
    async def scenario() -> None:
        transport = _CloudTransport(_response(account_id=99))
        attempt = _attempt()
        router = _router()
        router.register_cloud_adapter(MODEL)
        try:
            with cloud_llm_execution_scope(
                CloudLLMExecutionBinding(transport, attempt)
            ):
                with pytest.raises(CloudExecutionRefused) as error:
                    await router.complete_transport(
                        LLMRequest(
                            model=MODEL,
                            messages=[{"role": "user", "content": "hello"}],
                        )
                    )
            assert error.value.code == "route_changed"
        finally:
            await router.aclose()

    asyncio.run(scenario())
