#!/usr/bin/env python3
"""Refresh Frisket's model/capability catalog and matching pricing data.

LiteLLM supplies direct-provider aliases, capabilities and prices. OpenRouter's
public API supplies its active aliases, capabilities and prices. Deliberately
narrow family filters keep dated snapshots, fine-tunes and specialist endpoints
out of discovery proposals. Model additions require review; pricing refreshes
never change the approved runnable inventory.

Usage: uv run --no-project python scripts/dev/update_pricing.py --mode pricing
       uv run --no-project python scripts/dev/update_pricing.py --mode models
Commit the diff like a dependency bump — these numbers feed cost_actual.
"""

import argparse
import http.client
import json
import math
import re
import sys
import urllib.request
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from frisket.opendocrouter_catalog import (  # noqa: E402 -- standalone stdlib-only updater
    SOURCE_URL as OPENDOCROUTER_SOURCE,
    DocumentCatalog,
    parse_catalog,
    catalog_document,
)

LITELLM_SOURCE = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
OPENROUTER_SOURCE = "https://openrouter.ai/api/v1/models"
OUT = ROOT / "src/frisket/ai/llm/pricing_data.json"
MODEL_OUT = ROOT / "src/frisket/ai/llm/model_catalog.json"
MODEL_SCHEMA_VERSION = "frisket.model_catalog.v1"

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
CATALOG = json.loads(MODEL_OUT.read_text())

# These are product compatibility rules, not attempts to mirror provider
# inventories. Direct adapters support the current general text families below.
# OpenRouter remains bounded to the vendor families Frisket already presents.
_DIRECT_MODEL_PATTERNS = {
    "openai": re.compile(
        r"gpt-(?:[5-9](?:\.\d+)?)(?:-(?:mini|nano|luna|terra|sol|astra))?\Z"
    ),
    "anthropic": re.compile(
        r"claude-(?:haiku|sonnet|opus|fable|mythos)-(?:[4-9](?:-\d+)*)\Z"
    ),
    "gemini": re.compile(
        r"gemini-(?:[3-9](?:\.\d+)?)-(?:flash|flash-lite|pro)(?:-preview)?\Z"
    ),
}
_DIRECT_UPSTREAM_PROVIDERS = {
    "openai": frozenset({"openai"}),
    "anthropic": frozenset({"anthropic"}),
    "gemini": frozenset({"gemini"}),
}
_OPENROUTER_FAMILIES = (
    re.compile(r"qwen/qwen3(?:[.-].+)?\Z"),
    re.compile(r"z-ai/glm-5(?:[.-].+)?\Z"),
    re.compile(r"moonshotai/kimi-k3(?:[.-].+)?\Z"),
    re.compile(r"xiaomi/mimo-v2(?:[.-].+)?\Z"),
    re.compile(r"minimax/minimax-m3(?:[.-].+)?\Z"),
)
_SPECIALIST_MARKERS = (
    "-audio",
    "-coder",
    "-embedding",
    "-image",
    "-live",
    "-moderation",
    "-omni",
    "-rerank",
    "-search",
)
_SNAPSHOT = re.compile(r"(?:20\d{2}(?:-?\d{2}){1,2}|-\d{4}|-\d{2}-\d{2})(?:\Z|[-_:])")


def _supports_vision(entry: dict[str, Any]) -> bool:
    modalities = entry.get("supported_modalities")
    return entry.get("supports_vision") is True or (
        isinstance(modalities, list) and "image" in modalities
    )


def _direct_model(provider: str, model_id: str, entry: dict[str, Any]) -> bool:
    return (
        entry.get("mode") == "chat"
        and entry.get("litellm_provider") in _DIRECT_UPSTREAM_PROVIDERS[provider]
        and "input_cost_per_token" in entry
        and "output_cost_per_token" in entry
        and _DIRECT_MODEL_PATTERNS[provider].fullmatch(model_id) is not None
        and _SNAPSHOT.search(model_id) is None
    )


def _openrouter_model(entry: dict[str, Any]) -> bool:
    model_id = entry.get("id")
    if not isinstance(model_id, str):
        return False
    pricing = entry.get("pricing")
    architecture = entry.get("architecture")
    if (
        ":" in model_id
        or _SNAPSHOT.search(model_id)
        or any(marker in model_id for marker in _SPECIALIST_MARKERS)
        or not any(pattern.fullmatch(model_id) for pattern in _OPENROUTER_FAMILIES)
        or not isinstance(pricing, dict)
        or "prompt" not in pricing
        or "completion" not in pricing
        or not isinstance(architecture, dict)
    ):
        return False
    inputs = architecture.get("input_modalities")
    outputs = architecture.get("output_modalities")
    return (
        isinstance(inputs, list)
        and "text" in inputs
        and (not isinstance(outputs, list) or "text" in outputs)
    )


def _model_label(provider: str, model_id: str) -> str:
    if provider == "openai":
        return "GPT-" + model_id.removeprefix("gpt-").replace("-", " ").title()
    if provider == "anthropic":
        model_id = re.sub(r"(?<=\d)-(?=\d)", ".", model_id)
    words = model_id.replace("-", " ").split()
    names = {"gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}
    return " ".join(names.get(word, word.capitalize()) for word in words)


def _ordered_entries(
    provider: str,
    discovered: dict[str, dict[str, Any]],
    previous: dict[str, Any],
) -> list[dict[str, Any]]:
    old = {
        entry.get("id"): entry
        for entry in previous.get("providers", {}).get(provider, [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    ordered_ids = [model_id for model_id in old if model_id in discovered]
    ordered_ids.extend(sorted(set(discovered) - set(ordered_ids)))
    result = []
    for model_id in ordered_ids:
        fresh = discovered[model_id]
        prior = old.get(model_id, {})
        result.append(
            {
                "id": model_id,
                "label": prior.get("label") or fresh["label"],
                "vision": fresh["vision"],
            }
        )
    return result


def build_model_catalog(
    litellm: dict[str, Any],
    openrouter: dict[str, Any],
    *,
    previous: dict[str, Any] = CATALOG,
    updated: date | None = None,
) -> dict[str, Any]:
    """Build the bounded runnable roster from upstream capability metadata."""
    # Reuse the public provider names already in the OpenRouter response.
    # Only exact IDs match; do not infer aliases across providers.
    names = {
        entry["id"]: entry["name"].split(": ", 1)[-1]
        for entry in openrouter.get("data", [])
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and isinstance(entry.get("name"), str)
        and entry["name"].strip()
    }
    providers: dict[str, list[dict[str, Any]]] = {}
    for provider in ("anthropic", "openai", "gemini"):
        discovered = {}
        for upstream_id, entry in litellm.items():
            if not isinstance(upstream_id, str) or not isinstance(entry, dict):
                continue
            if provider == "gemini":
                prefix = "gemini/"
                if not upstream_id.startswith(prefix):
                    continue
                model_id = upstream_id.removeprefix(prefix)
            else:
                model_id = upstream_id
            if not _direct_model(provider, model_id, entry):
                continue
            discovered[model_id] = {
                "label": names.get(
                    f"{'google' if provider == 'gemini' else provider}/{model_id}"
                )
                or _model_label(provider, model_id),
                "vision": _supports_vision(entry),
            }
        providers[provider] = _ordered_entries(provider, discovered, previous)

    discovered_openrouter = {
        entry["id"]: {
            "label": entry.get("name") or entry["id"],
            "vision": "image" in entry["architecture"]["input_modalities"],
        }
        for entry in openrouter.get("data", [])
        if isinstance(entry, dict) and _openrouter_model(entry)
    }
    providers["openrouter"] = _ordered_entries(
        "openrouter", discovered_openrouter, previous
    )
    providers["ollama"] = [
        dict(entry) for entry in previous.get("providers", {}).get("ollama", [])
    ]
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "updated": (updated or date.today()).isoformat(),
        "providers": providers,
    }


def _rate(value: object) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid provider rate: {value!r}")
    return result


def _rates(input_per_token: object, output_per_token: object) -> list[float]:
    return [
        round(_rate(float(input_per_token) * 1e6), 4),
        round(_rate(float(output_per_token) * 1e6), 4),
    ]


def build_price_table(
    litellm: dict[str, Any],
    openrouter: dict[str, Any],
    *,
    catalog: dict[str, Any] = CATALOG,
    updated: date | None = None,
    opendocrouter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text: dict[str, list[float]] = {}
    for provider, entries in catalog["providers"].items():
        if provider in {"ollama", "openrouter"}:
            continue
        for catalog_entry in entries:
            name = catalog_entry["id"]
            entry = litellm.get(f"gemini/{name}" if provider == "gemini" else name)
            if not entry or not all(
                key in entry
                for key in ("input_cost_per_token", "output_cost_per_token")
            ):
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
                "input_per_token": _rate(entry["input_cost_per_token"]),
                "output_per_token": _rate(entry["output_cost_per_token"]),
            }
        elif "input_cost_per_second" in entry:
            audio[name] = {"per_second": _rate(entry["input_cost_per_second"])}

    for name, override in MAINTAINED_AUDIO_OVERRIDES.items():
        audio[name] = {"per_second": override["per_second"]}

    result = {
        "sources": {
            "litellm": LITELLM_SOURCE,
            "openrouter": OPENROUTER_SOURCE,
        },
        "updated": (updated or date.today()).isoformat(),
        "text": text,
        "audio": dict(sorted(audio.items())),
    }

    if opendocrouter is not None:
        result["opendocrouter"] = catalog_document(parse_catalog(opendocrouter))
        result["sources"]["opendocrouter"] = OPENDOCROUTER_SOURCE
    return result


def _fetch_opendocrouter_snapshot() -> dict[str, Any]:
    try:
        candidate = json.loads(
            urllib.request.urlopen(OPENDOCROUTER_SOURCE, timeout=30).read()
        )
        return catalog_document(parse_catalog(candidate))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        try:
            previous = json.loads(OUT.read_text())
            snapshot = catalog_document(parse_catalog(previous["opendocrouter"]))
        except (OSError, KeyError, TypeError, ValueError) as previous_exc:
            raise RuntimeError(
                "OpenDocRouter pricing refresh failed and no valid previous "
                "snapshot is available"
            ) from previous_exc
        print(
            "WARNING: OpenDocRouter pricing refresh failed "
            f"({exc}); preserving the previous complete snapshot",
            file=sys.stderr,
        )
        return snapshot


def approved_document_prices(previous: dict, fresh: dict) -> dict:
    """Overlay rates without changing reviewed OCR identities or capabilities."""
    approved = parse_catalog(previous)
    incoming = parse_catalog(fresh)
    rates = {model.id: model for model in (*incoming.models, *incoming.retired_models)}
    old_models = (*approved.models, *approved.retired_models)
    if any(model.id not in rates for model in old_models):
        # One global price version describes the snapshot. Avoid claiming a new
        # version while carrying some rates from a different generation.
        print(
            "WARNING: OpenDocRouter omitted approved models; preserving the "
            "previous complete snapshot until model review",
            file=sys.stderr,
        )
        return catalog_document(approved)

    def priced(model):
        fresh_model = rates[model.id]
        return replace(
            model,
            max_charge_per_page_usd=fresh_model.max_charge_per_page_usd,
            avg_charge_per_page_usd=fresh_model.avg_charge_per_page_usd,
            input_per_million=fresh_model.input_per_million,
            cached_input_per_million=fresh_model.cached_input_per_million,
            output_per_million=fresh_model.output_per_million,
        )

    return catalog_document(
        DocumentCatalog(
            price_version=incoming.price_version,
            models=tuple(priced(model) for model in approved.models),
            retired_models=tuple(priced(model) for model in approved.retired_models),
        )
    )


def document_inventory(snapshot: dict) -> dict:
    """Executable metadata only; rate changes must not open discovery PRs."""
    return {
        section: [
            {key: row[key] for key in ("id", "name", "version", "max_sync_pages")}
            for row in snapshot.get(section, [])
        ]
        for section in ("data", "retired_data")
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pricing", "models"), default="pricing")
    args = parser.parse_args(argv)
    previous_catalog = json.loads(MODEL_OUT.read_text())
    previous_prices = json.loads(OUT.read_text())
    litellm = json.loads(urllib.request.urlopen(LITELLM_SOURCE, timeout=30).read())
    request = urllib.request.Request(
        OPENROUTER_SOURCE,
        headers={"User-Agent": "frisket-pricing-refresh/1"},
    )
    openrouter = json.loads(urllib.request.urlopen(request, timeout=30).read())
    opendocrouter = _fetch_opendocrouter_snapshot()
    catalog = previous_catalog
    if args.mode == "models":
        catalog = build_model_catalog(litellm, openrouter, previous=previous_catalog)
        if catalog["providers"] == previous_catalog["providers"] and document_inventory(
            opendocrouter
        ) == document_inventory(previous_prices["opendocrouter"]):
            print("No model changes; pricing refresh is handled separately")
            return
    else:
        opendocrouter = approved_document_prices(
            previous_prices["opendocrouter"], opendocrouter
        )
    output = build_price_table(
        litellm, openrouter, catalog=catalog, opendocrouter=opendocrouter
    )

    # Validate/build both documents before writing. A failed CI generation is
    # never committed; no runtime service reads these working-tree files.
    catalog_json = json.dumps(catalog, indent=2, allow_nan=False) + "\n"
    prices_json = json.dumps(output, indent=2, allow_nan=False) + "\n"
    if args.mode == "models":
        MODEL_OUT.write_text(catalog_json)
        print(f"wrote {MODEL_OUT}")
    OUT.write_text(prices_json)
    print(
        f"wrote {OUT}: {len(output['text'])} text, {len(output['audio'])} audio prices"
    )


if __name__ == "__main__":
    main()
