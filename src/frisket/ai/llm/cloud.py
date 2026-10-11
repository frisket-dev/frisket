"""Route-bound LLM completion through a connected Frisket Cloud account."""

from __future__ import annotations

from typing import Any

import httpx

from frisket.cloud_execution import (
    CloudExecutionRefused,
    cloud_execution_accounting,
    cloud_llm_execution,
)
from frisket.contracts.http.cloud_connection import (
    CloudLLMRequest,
    CloudLLMResult,
    CloudLLMToolCall,
    CloudLLMUsage,
)

from .types import LLMActualAccounting, LLMRequest, LLMResponse, LLMToolCall


def _tool_call(call: CloudLLMToolCall) -> LLMToolCall:
    result: LLMToolCall = {
        "name": call.name,
        "args": call.args,
        "id": call.id,
    }
    if call.provider_details is not None:
        result["provider_details"] = call.provider_details
    return result


class CloudLLMAdapter:
    """Translate one admitted local model call to the normalized Cloud port."""

    async def complete(
        self,
        request: LLMRequest,
        _client: httpx.AsyncClient,
    ) -> LLMResponse:
        transport, attempt = cloud_llm_execution(request.model)
        wire = CloudLLMRequest(
            model=request.model,
            messages=request.messages,
            schema_=request.schema,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            params=request.params,
            mechanism=request.mechanism,
            tools=request.tools,
            reasoning_policy=request.reasoning_policy,
        )
        response = await transport.execute_llm(attempt, wire)
        accounting = cloud_execution_accounting(
            attempt, response, capability="llm.complete"
        )
        result = response.result
        if not isinstance(result, CloudLLMResult):
            raise CloudExecutionRefused(
                "cloud_protocol_error", "Cloud LLM execution returned no LLM result"
            )
        if result.model != request.model or result.provider != attempt.payer.provider:
            raise CloudExecutionRefused(
                "route_changed", "Cloud LLM result differs from the admitted route"
            )

        [model_call] = accounting["model_calls"]
        expected_credential = model_call["credential_source"]
        if result.credential_source != expected_credential:
            raise CloudExecutionRefused(
                "route_changed", "Cloud LLM result differs from the admitted payer"
            )
        receipt = response.receipt
        if receipt is None or not isinstance(receipt.usage, CloudLLMUsage):
            raise CloudExecutionRefused(
                "cloud_protocol_error", "Cloud LLM execution returned invalid usage"
            )
        if (
            result.tokens_in != receipt.usage.tokens_in
            or result.tokens_out != receipt.usage.tokens_out
        ):
            raise CloudExecutionRefused(
                "cloud_protocol_error",
                "Cloud LLM result usage differs from its accounting receipt",
            )

        return LLMResponse(
            content=result.content,
            data=result.data,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost=result.cost,
            model=result.model,
            cost_source=result.cost_source,  # type: ignore[arg-type]
            cached=result.cached,
            provider=result.provider,
            credential_source=result.credential_source,
            raw=result.raw,
            tool_calls=(
                [_tool_call(call) for call in result.tool_calls]
                if result.tool_calls is not None
                else None
            ),
            output_tool_call=(
                _tool_call(result.output_tool_call)
                if result.output_tool_call is not None
                else None
            ),
            duration_ms=result.duration_ms,
            output_limited=result.output_limited,
            actual_accounting=LLMActualAccounting.one(
                cost=accounting["cost"],
                model_call=dict(model_call),
                receipt=dict(accounting["cloud_receipt"]),
            ),
        )


def validate_cloud_model_id(model: Any) -> str:
    """Return one clean provider-qualified model identity."""

    if (
        not isinstance(model, str)
        or model != model.strip()
        or any(character.isspace() for character in model)
    ):
        raise ValueError("Cloud model must be a clean qualified model id")
    provider, separator, name = model.partition("/")
    if not separator or not provider or not name:
        raise ValueError("Cloud model must be a clean qualified model id")
    return model
