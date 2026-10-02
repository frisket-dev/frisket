#!/usr/bin/env python3
"""Refresh src/frisket/ai/llm/pricing_data.json from LiteLLM's community price
table — the closest thing to a pricing API that exists (OpenAI/Anthropic/
Google publish no machine-readable price lists; LiteLLM's JSON is the
de-facto registry, ~2.8k models).

Usage: uv run python scripts/dev/update_pricing.py
The scheduled workflow validates this data and auto-merges its pricing-only PR.
"""

import json
import math
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

LITELLM_SOURCE = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
OPENROUTER_SOURCE = "https://openrouter.ai/api/v1/models"
OUT = ROOT / "src/frisket/ai/llm/pricing_data.json"

# OpenRouter's model API has no MAI-Transcribe 2 row (verified 2026-09-06),
# so retain the reviewed $0.10/hour provider rate explicitly rather than
# guessing a conversion from unrelated text metadata.
MAINTAINED_AUDIO_OVERRIDES: dict[str, dict[str, float | str]] = {
    "microsoft/mai-transcribe-2": {
        "per_second": 0.1 / 3600,
        "source": "https://openrouter.ai/microsoft/mai-transcribe-2",
    },
}

# The reviewed picker catalog is the pricing allow-list.
CATALOG = json.loads((ROOT / "src/frisket/ai/llm/model_catalog.json").read_text())


def _rate(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("pricing rates must be numbers, not booleans")
    rate = float(value)
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("pricing rates must be non-negative and finite")
    return rate


def _rates(input_per_token: object, output_per_token: object) -> list[float]:
    return [
        round(_rate(input_per_token) * 1e6, 4),
        round(_rate(output_per_token) * 1e6, 4),
    ]


def validate_price_table(data: object) -> None:
    """Reject broken catalogs, not legitimate changes in price magnitude."""
    if not isinstance(data, dict):
        raise ValueError("pricing data must be an object")
    text, audio = data.get("text"), data.get("audio")
    if not isinstance(text, dict) or not text or not isinstance(audio, dict):
        raise ValueError(
            "pricing data requires nonempty text prices and an audio object"
        )
    for model, rates in text.items():
        if (
            not isinstance(model, str)
            or not model
            or not isinstance(rates, list)
            or len(rates) != 2
        ):
            raise ValueError("invalid text pricing entry")
        for rate in rates:
            if not isinstance(rate, (int, float)):
                raise ValueError("pricing rates must be numeric")
            _rate(rate)
    for model, rates in audio.items():
        if (
            not isinstance(model, str)
            or not model
            or not isinstance(rates, dict)
            or set(rates)
            not in ({"per_second"}, {"input_per_token", "output_per_token"})
        ):
            raise ValueError("invalid audio pricing entry")
        for rate in rates.values():
            if not isinstance(rate, (int, float)):
                raise ValueError("pricing rates must be numeric")
            _rate(rate)


def build_price_table(
    litellm: dict[str, Any],
    openrouter: dict[str, Any],
    *,
    catalog: dict[str, Any] = CATALOG,
    updated: date | None = None,
) -> dict[str, Any]:
    text: dict[str, list[float]] = {}
    for provider, entries in catalog["providers"].items():
        if provider in {"ollama", "openrouter"}:
            continue
        for catalog_entry in entries:
            name = catalog_entry["id"]
            entry = litellm.get(name)
            if not entry or "input_cost_per_token" not in entry:
                print(
                    f"WARNING: {name} missing from LiteLLM — left out; estimates "
                    "surface as unknown",
                    file=sys.stderr,
                )
                continue
            text[name] = _rates(
                entry["input_cost_per_token"], entry["output_cost_per_token"]
            )

    openrouter_by_id = {
        entry["id"]: entry for entry in openrouter.get("data", []) if entry.get("id")
    }
    for catalog_entry in catalog["providers"].get("openrouter", []):
        name = catalog_entry["id"]
        pricing = (openrouter_by_id.get(name) or {}).get("pricing") or {}
        if "prompt" not in pricing or "completion" not in pricing:
            print(
                f"WARNING: {name} missing from OpenRouter — left out; estimates "
                "surface as unknown",
                file=sys.stderr,
            )
            continue
        # Keep the provider's nested model slug. model_price strips only the
        # outer Frisket provider from openrouter/qwen/qwen3-8b.
        text[name] = _rates(pricing["prompt"], pricing["completion"])

    # Bare transcription models bill per second or by returned token usage.
    audio: dict[str, dict] = {}
    for name, entry in litellm.items():
        if entry.get("mode") != "audio_transcription" or "/" in name:
            continue
        if "input_cost_per_token" in entry and "output_cost_per_token" in entry:
            audio[name] = {
                "input_per_token": entry["input_cost_per_token"],
                "output_per_token": entry["output_cost_per_token"],
            }
        elif "input_cost_per_second" in entry:
            audio[name] = {"per_second": entry["input_cost_per_second"]}

    for name, override in MAINTAINED_AUDIO_OVERRIDES.items():
        audio[name] = {"per_second": override["per_second"]}

    return {
        "sources": {
            "litellm": LITELLM_SOURCE,
            "openrouter": OPENROUTER_SOURCE,
        },
        "updated": (updated or date.today()).isoformat(),
        "text": text,
        "audio": dict(sorted(audio.items())),
    }


def main() -> None:
    litellm = json.loads(urllib.request.urlopen(LITELLM_SOURCE, timeout=30).read())
    request = urllib.request.Request(
        OPENROUTER_SOURCE,
        headers={"User-Agent": "frisket-pricing-refresh/1"},
    )
    openrouter = json.loads(urllib.request.urlopen(request, timeout=30).read())
    output = build_price_table(litellm, openrouter)
    validate_price_table(output)
    OUT.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(
        f"wrote {OUT}: {len(output['text'])} text, {len(output['audio'])} audio models"
    )


if __name__ == "__main__":
    main()
