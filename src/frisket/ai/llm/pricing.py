"""Per-model pricing and cost estimation.

Prices live in pricing_data.json, refreshed (never hand-edited) via
`uv run python scripts/dev/update_pricing.py` from LiteLLM's community price
table and OpenRouter's public model catalog. There is NO fallback price anywhere: an unknown model prices as
None ("don't know"), estimates surface it as unknown (the cost gate then
demands explicit confirmation), and actuals never fabricate a number.

Canonical ``ollama/@<endpoint>/<model>`` identities name a model server.
Its terms come only from ``model_servers``, the configured servers of the
workspace the call belongs to, passed by the caller. A server whose origin is
this machine (loopback) is free: no request leaves the machine. Any other
server may forward to a paid provider, so its models are unknown unless the
person entered a price for the model; that entered price is the rate. A
model server absent from ``model_servers`` is unknown, never free.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.opendocrouter_catalog import parse_catalog, install_catalog
from frisket.local_model_ids import bare_model_name, parse_local_model_id

logger = logging.getLogger(__name__)

ModelServers = Sequence[LocalModelEndpointConfig]

_data = json.loads((Path(__file__).parent / "pricing_data.json").read_text())

# (input $/M, output $/M)
PRICES: dict[str, tuple[float, float]] = {
    model: (float(pin), float(pout)) for model, (pin, pout) in _data["text"].items()
}

# speech models (the bespoke /audio/transcriptions call bypasses the router):
# {"per_second": x} or {"input_per_token": x, "output_per_token": y}
AUDIO_PRICES: dict[str, dict] = _data["audio"]


def install_pricing_data(data: dict[str, Any]) -> None:
    """Replace normalized prices, retaining the last valid document catalog."""
    text = data.get("text")
    audio = data.get("audio")
    if not isinstance(text, dict) or not isinstance(audio, dict):
        raise ValueError("pricing data must contain text and audio objects")
    parsed_text: dict[str, tuple[float, float]] = {}
    for model, rates in text.items():
        if not isinstance(model, str) or not isinstance(rates, list) or len(rates) != 2:
            raise ValueError("invalid text pricing entry")
        parsed = (float(rates[0]), float(rates[1]))
        if any(not math.isfinite(rate) or rate < 0 for rate in parsed):
            raise ValueError("pricing rates must be non-negative and finite")
        parsed_text[model] = parsed
    parsed_audio: dict[str, dict] = {}
    for model, rates in audio.items():
        if not isinstance(model, str) or not isinstance(rates, dict):
            raise ValueError("invalid audio pricing entry")
        keys = set(rates)
        if keys == {"per_second"}:
            parsed = {"per_second": float(rates["per_second"])}
        elif keys == {"input_per_token", "output_per_token"}:
            parsed = {
                "input_per_token": float(rates["input_per_token"]),
                "output_per_token": float(rates["output_per_token"]),
            }
        else:
            raise ValueError("invalid audio pricing entry")
        if any(not math.isfinite(rate) or rate < 0 for rate in parsed.values()):
            raise ValueError("pricing rates must be non-negative and finite")
        parsed_audio[model] = parsed
    documents = None
    if "opendocrouter" in data:
        try:
            documents = parse_catalog(data["opendocrouter"])
        except ValueError as exc:
            logger.warning(
                "OpenDocRouter pricing validation failed (%s); preserving the "
                "previous complete snapshot",
                exc,
            )
    global PRICES, AUDIO_PRICES
    PRICES = parsed_text
    AUDIO_PRICES = parsed_audio
    if documents is not None:
        install_catalog(documents)


ModelCostSource = Literal["free_local", "pricing_data", "unknown"]


@dataclass(frozen=True)
class ModelPricing:
    """One text-rate lookup, including the facts that justify the value."""

    price: tuple[float, float] | None
    cost_source: ModelCostSource
    pricing_key: str | None


def _pricing_key(model: str, catalog_key: str) -> str:
    provider, separator, _tail = model.partition("/")
    return f"{provider}/{catalog_key}.tokens" if separator else f"{catalog_key}.tokens"


def model_pricing(model: str, model_servers: ModelServers = ()) -> ModelPricing:
    """Return text rates and the fact that justifies them.

    Keep the discriminator beside the price lookup: downstream accounting must
    carry this value, never reconstruct provenance from a zero dollar amount.
    """
    try:
        endpoint_id, server_model = parse_local_model_id(model)
    except ValueError:
        pass
    else:
        return _model_server_pricing(model, endpoint_id, server_model, model_servers)
    bare = bare_model_name(model)
    if model.partition("/")[0] == "openrouter":
        # OpenRouter suffixes select distinct variants whose rates can differ.
        # Only a catalog row for the complete provider-wire identity is proof.
        price = PRICES.get(bare)
        if price is not None:
            return ModelPricing(price, "pricing_data", _pricing_key(model, bare))
        return ModelPricing(None, "unknown", None)
    # A family key can prefix a newer sibling (``gpt-5`` vs ``gpt-5.6-sol``).
    # Prefer the most specific match regardless of catalog/JSON ordering.
    for key in sorted(PRICES, key=len, reverse=True):
        if bare.startswith(key):
            return ModelPricing(PRICES[key], "pricing_data", _pricing_key(model, key))
    return ModelPricing(None, "unknown", None)


def _model_server(
    endpoint_id: str, model_servers: ModelServers
) -> LocalModelEndpointConfig | None:
    return next(
        (server for server in model_servers if server.endpoint_id == endpoint_id),
        None,
    )


def _model_server_pricing(
    model: str, endpoint_id: str, server_model: str, model_servers: ModelServers
) -> ModelPricing:
    server = _model_server(endpoint_id, model_servers)
    if server is None:
        return ModelPricing(None, "unknown", None)
    if server.on_this_machine:
        return ModelPricing((0.0, 0.0), "free_local", None)
    price = next(
        (
            entry.price
            for entry in server.models
            if entry.name == server_model and entry.price is not None
        ),
        None,
    )
    if price is None:
        return ModelPricing(None, "unknown", None)
    # An entered rate has no catalog row; its identity is its value, so a
    # changed price is a different rate and voids consent given to the old one.
    pin, pout = price
    return ModelPricing(price, "pricing_data", f"{model}.tokens:{pin!r}/{pout!r}")


def model_server_on_this_machine(model: str, model_servers: ModelServers) -> bool:
    """Whether ``model`` names a model on a configured server at a loopback
    origin."""
    try:
        endpoint_id, _server_model = parse_local_model_id(model)
    except ValueError:
        return False
    server = _model_server(endpoint_id, model_servers)
    return server is not None and server.on_this_machine


def model_price(
    model: str, model_servers: ModelServers = ()
) -> tuple[float, float] | None:
    """(input $/M, output $/M) for a text model, or None when unknown."""
    return model_pricing(model, model_servers).price


def model_cost_source(model: str, model_servers: ModelServers = ()) -> ModelCostSource:
    """Why a text model's provider cost is known, or ``unknown``."""
    return model_pricing(model, model_servers).cost_source


def audio_price(model: str, model_servers: ModelServers = ()) -> dict | None:
    """Pricing entry for a speech model, or None when unknown — None means
    "don't know", and callers report exactly that, never a substitute."""
    try:
        parse_local_model_id(model)
    except ValueError:
        pass
    else:
        return (
            {"per_second": 0.0}
            if model_server_on_this_machine(model, model_servers)
            else None
        )
    return AUDIO_PRICES.get(bare_model_name(model))


def cost_of(
    model: str, tokens_in: int, tokens_out: int, model_servers: ModelServers = ()
) -> float | None:
    """Actual cost of a call, or None when the model is unpriced."""
    cost, _source = cost_with_pricing(
        model_pricing(model, model_servers), tokens_in, tokens_out
    )
    return cost


def cost_of_with_source(
    model: str, tokens_in: int, tokens_out: int
) -> tuple[float | None, ModelCostSource]:
    """Actual text cost paired with its pricing provenance."""
    return cost_with_pricing(model_pricing(model), tokens_in, tokens_out)


def cost_with_pricing(
    pricing: ModelPricing, tokens_in: int, tokens_out: int
) -> tuple[float | None, ModelCostSource]:
    """Actual text cost under one already-resolved pricing."""
    if pricing.price is None:
        return None, pricing.cost_source
    pin, pout = pricing.price
    return (
        tokens_in * pin / 1e6 + tokens_out * pout / 1e6,
        pricing.cost_source,
    )


def estimate_tokens(text: str) -> int:
    """Crude but stable: ~4 chars/token. Used only for pre-run estimates."""
    return max(1, len(text) // 4)


def estimate_output_tokens(
    call: Any,
    *,
    spec: Mapping[str, Any] | None = None,
) -> int:
    """Estimate realistic output tokens for one rendered LLM call.

    Uses a per-field baseline (150 + 50/field) plus a modest
    allowance when grounded citations, justifications, or reasoning are
    requested, bounded by the call's wire ``max_tokens``.
    """
    fields: Any = spec.get("fields") if isinstance(spec, Mapping) else None
    schema: Any = getattr(call, "schema", None)
    raw_props: Any = schema.get("properties") if isinstance(schema, Mapping) else None
    props: Mapping[str, Any] | None = (
        raw_props if isinstance(raw_props, Mapping) else None
    )
    if isinstance(fields, (list, tuple)) and fields:
        field_count = len(fields)
    elif props:
        field_count = len(props)
    else:
        field_count = 1

    est_out = 150 + 50 * max(1, field_count)

    wants_explanation = False
    if isinstance(spec, Mapping):
        grounding: Any = spec.get("grounding")
        wants_explanation = (
            bool(spec.get("include_sources"))
            or bool(spec.get("include_justification"))
            or bool(spec.get("include_reasoning"))
            or (isinstance(grounding, Mapping) and bool(grounding.get("enabled")))
        )
    if not wants_explanation and props:
        for prop_name, prop_schema in props.items():
            if isinstance(prop_name, str) and prop_name.endswith(
                ("_justification", "_reasoning")
            ):
                wants_explanation = True
                break
            if isinstance(prop_schema, Mapping):
                nested_props: Any = prop_schema.get("properties")
                if isinstance(nested_props, Mapping) and "evidence" in nested_props:
                    wants_explanation = True
                    break

    if wants_explanation:
        est_out += 100 * max(1, field_count)

    max_tokens = getattr(call, "max_tokens", None)
    return max(1, min(int(max_tokens), est_out)) if max_tokens is not None else est_out


def estimate_run_cost(
    model: str, input_texts: list[str], est_output_tokens_per_row: int = 200
) -> float | None:
    tokens_in = sum(estimate_tokens(t) for t in input_texts)
    tokens_out = est_output_tokens_per_row * len(input_texts)
    return cost_of(model, tokens_in, tokens_out)
