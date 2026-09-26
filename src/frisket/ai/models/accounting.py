"""Neutral model-call accounting payloads shared by non-row LLM effects."""

from __future__ import annotations

from typing import Any

from frisket.ai.llm import LLMResponse
from frisket.ai.models.metadata import ModelCallMeta
from frisket.local_model_ids import bare_model_name


def wire_accounting_meta(
    model_name: str, wire_responses: list[LLMResponse]
) -> dict[str, Any]:
    """Build durable provider facts and aggregate usage from returned wires."""

    model_id = bare_model_name(model_name)
    calls: list[dict[str, Any]] = []
    for wire in wire_responses:
        units = {"tokens_in": wire.tokens_in, "tokens_out": wire.tokens_out}
        if wire.output_limited:
            units["output_limited"] = True
        if wire.cached:
            call = ModelCallMeta.cache_hit(
                capability="llm.complete",
                engine=model_name,
                provider=wire.provider,
                provider_kind="chat_api",
                model_ids=[model_id or model_name],
                units=units,
                warnings=[],
            ).as_dict()
        else:
            call = ModelCallMeta.provider_call(
                capability="llm.complete",
                engine=model_name,
                provider=wire.provider,
                provider_kind="chat_api",
                model_ids=[model_id or model_name],
                credential_source=wire.credential_source,
                provider_reported_cost_usd=wire.cost,
                provider_cost_usd=wire.cost,
                units=units,
                cost_source=wire.cost_source,
                warnings=[],
                duration_ms=wire.duration_ms,
            ).as_dict()
        calls.append(call)
    live_calls = [wire for wire in wire_responses if not wire.cached]
    if not live_calls:
        cost: float | None = 0.0
    elif any(wire.cost is None for wire in live_calls):
        cost = None
    else:
        cost = sum(wire.cost for wire in live_calls)
    return {
        "tokens_in": sum(wire.tokens_in for wire in wire_responses),
        "tokens_out": sum(wire.tokens_out for wire in wire_responses),
        "cost": cost,
        "model_calls": calls,
    }
