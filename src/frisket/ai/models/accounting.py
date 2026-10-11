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
    execution_receipts: list[dict[str, Any]] = []
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
        elif wire.actual_accounting is not None:
            call = dict(wire.actual_accounting.model_call)
            execution_receipts.append(dict(wire.actual_accounting.receipt))
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
    live_costs = [
        (
            wire.actual_accounting.cost
            if wire.actual_accounting is not None
            else wire.cost
        )
        for wire in wire_responses
        if not wire.cached
    ]
    if not live_costs:
        cost: float | None = 0.0
    elif any(item is None for item in live_costs):
        cost = None
    else:
        cost = sum(item for item in live_costs if item is not None)
    result = {
        "tokens_in": sum(wire.tokens_in for wire in wire_responses),
        "tokens_out": sum(wire.tokens_out for wire in wire_responses),
        "cost": cost,
        "model_calls": calls,
    }
    if execution_receipts:
        result["execution_receipts"] = execution_receipts
    return result
