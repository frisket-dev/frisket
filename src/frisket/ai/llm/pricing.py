"""Per-model pricing and cost estimation.

Prices live in pricing_data.json, refreshed (never hand-edited) via
`uv run python scripts/dev/update_pricing.py` from LiteLLM's community price
table and OpenRouter's public model catalog. There is NO fallback price anywhere: an unknown model prices as
None ("don't know"), estimates surface it as unknown (the cost gate then
demands explicit confirmation), and actuals never fabricate a number.

Canonical ``ollama/@<endpoint>/<model>`` identities are the deliberate
exception: Frisket sends no request through its provider credentials, so its
provider cost is structurally known to be zero. This says nothing about the
operator's hardware cost; access to operator-owned compute is an authorization
concern, not a provider-pricing guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from frisket.local_model_ids import bare_model_name, parse_local_model_id

_data = json.loads((Path(__file__).parent / "pricing_data.json").read_text())

# (input $/M, output $/M)
PRICES: dict[str, tuple[float, float]] = {
    model: (float(pin), float(pout)) for model, (pin, pout) in _data["text"].items()
}

# speech models (the bespoke /audio/transcriptions call bypasses the router):
# {"per_second": x} or {"input_per_token": x, "output_per_token": y}
AUDIO_PRICES: dict[str, dict] = _data["audio"]

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


def model_pricing(model: str) -> ModelPricing:
    """Return text rates and the fact that justifies them.

    Keep the discriminator beside the price lookup: downstream accounting must
    carry this value, never reconstruct provenance from a zero dollar amount.
    """
    try:
        parse_local_model_id(model)
    except ValueError:
        pass
    else:
        return ModelPricing((0.0, 0.0), "free_local", None)
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


def model_price(model: str) -> tuple[float, float] | None:
    """(input $/M, output $/M) for a text model, or None when unknown."""
    return model_pricing(model).price


def model_cost_source(model: str) -> ModelCostSource:
    """Why a text model's provider cost is known, or ``unknown``."""
    return model_pricing(model).cost_source


def audio_price(model: str) -> dict | None:
    """Pricing entry for a speech model, or None when unknown — None means
    "don't know", and callers report exactly that, never a substitute."""
    try:
        parse_local_model_id(model)
    except ValueError:
        pass
    else:
        return {"per_second": 0.0}
    return AUDIO_PRICES.get(bare_model_name(model))


def cost_of(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Actual cost of a call, or None when the model is unpriced."""
    cost, _source = cost_of_with_source(model, tokens_in, tokens_out)
    return cost


def cost_of_with_source(
    model: str, tokens_in: int, tokens_out: int
) -> tuple[float | None, ModelCostSource]:
    """Actual text cost paired with its pricing provenance."""
    pricing = model_pricing(model)
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


def estimate_run_cost(
    model: str, input_texts: list[str], est_output_tokens_per_row: int = 200
) -> float | None:
    tokens_in = sum(estimate_tokens(t) for t in input_texts)
    tokens_out = est_output_tokens_per_row * len(input_texts)
    return cost_of(model, tokens_in, tokens_out)
