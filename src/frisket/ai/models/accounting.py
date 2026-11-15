"""Neutral model-call accounting payloads shared by non-row LLM effects."""

from __future__ import annotations

from typing import Any

from frisket.ai.llm import LLMResponse
from frisket.ai.llm.types import wire_responses as expand_wire_responses
from frisket.ai.models.metadata import (
    ModelCallMeta,
    PROVIDER_KIND,
    model_calls_cost_actual,
)
from frisket.ai.models.usage_counts import model_call_token_counts, sum_known_counts
from frisket.local_model_ids import bare_model_name


def wire_accounting_meta(
    model_name: str, wire_responses: list[LLMResponse]
) -> dict[str, Any]:
    """Build durable provider facts and aggregate usage from returned wires."""

    wire_responses = expand_wire_responses(wire_responses)
    model_id = bare_model_name(model_name)
    calls: list[dict[str, Any]] = []
    execution_receipts: list[dict[str, Any]] = []
    for wire in wire_responses:
        units = {
            "tokens_in": wire.tokens_in if wire.usage_available else None,
            "tokens_out": wire.tokens_out if wire.usage_available else None,
        }
        if wire.output_limited:
            units["output_limited"] = True
        if wire.cached:
            call = ModelCallMeta.cache_hit(
                capability="llm.complete",
                engine=model_name,
                provider=wire.provider,
                provider_kind=PROVIDER_KIND.get(wire.provider, "platform_api"),
                model_ids=[model_id or model_name],
                units=units,
                warnings=[],
            ).as_dict()
        elif wire.actual_accounting is not None:
            calls.extend(dict(call) for call in wire.actual_accounting.model_calls)
            execution_receipts.extend(
                dict(receipt) for receipt in wire.actual_accounting.receipts
            )
            continue
        else:
            call = ModelCallMeta.provider_call(
                capability="llm.complete",
                engine=model_name,
                provider=wire.provider,
                provider_kind=PROVIDER_KIND.get(wire.provider, "platform_api"),
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
        "tokens_in": sum_known_counts(
            wire.tokens_in if wire.usage_available else None for wire in wire_responses
        ),
        "tokens_out": sum_known_counts(
            wire.tokens_out if wire.usage_available else None for wire in wire_responses
        ),
        "cost": cost,
        "model_calls": calls,
    }
    if execution_receipts:
        result["execution_receipts"] = execution_receipts
    return result


def merge_accounting_meta(*parts: dict[str, Any] | None) -> dict[str, Any] | None:
    """Combine retained accounting by durable fact identity, never minting calls.

    An existing transport error goes first: its current authoritative fact wins
    when a capability also retained an earlier observation of the same call.
    """
    present = [part for part in parts if part is not None]
    if not present:
        return None
    if len(present) == 1:
        return dict(present[0])
    calls_by_id = {}
    receipts = []
    for part in present:
        for call in part.get("model_calls", ()):
            calls_by_id.setdefault(call["id"], call)
        for receipt in part.get("execution_receipts", ()):
            if receipt not in receipts:
                receipts.append(receipt)
    calls = list(calls_by_id.values())
    merged = {
        **model_call_token_counts(calls),
        "cost": model_calls_cost_actual(calls),
        "model_calls": calls,
    }
    if receipts:
        merged["execution_receipts"] = receipts
    return merged
