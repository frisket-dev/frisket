"""The curated model catalog is current, priced, generated, and wire-safe."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.model_catalog import MODEL_CATALOG, MODEL_ENTRIES
from frisket.ai.llm.pricing import (
    PRICES,
    audio_price,
    cost_of_with_source,
    model_cost_source,
    model_price,
    model_pricing,
)
from frisket.ai.llm.types import LLMRequest

ROOT = Path(__file__).resolve().parents[2]


def test_current_frontier_families_are_available_and_priced() -> None:
    expected = {
        "anthropic": {"claude-sonnet-5", "claude-fable-5"},
        "openai": {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"},
        "gemini": {
            "gemini-3.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
        },
    }
    for provider, model_ids in expected.items():
        assert model_ids <= set(MODEL_CATALOG[provider])
    for provider, entries in MODEL_ENTRIES.items():
        if provider == "ollama":
            continue
        assert all(model_price(f"{provider}/{entry['id']}") for entry in entries)


def test_pricing_uses_specific_model_before_family_prefix() -> None:
    assert model_price("openai/gpt-5.6-sol") == (4.0, 20.0)
    assert model_price("openai/gpt-5") == (1.25, 10.0)
    assert model_pricing("openai/gpt-5-future-snapshot").pricing_key == (
        "openai/gpt-5.tokens"
    )


def test_canonical_local_identity_is_zero_before_provider_stripping() -> None:
    assert model_price("ollama/@desk/gpt-5") == (0.0, 0.0)
    assert audio_price("ollama/@desk/whisper-1") == {"per_second": 0.0}

    # The same bare names keep their hosted catalog prices. Locality belongs
    # to the canonical endpoint-qualified identity, never the model substring.
    assert model_price("openai/gpt-5") == (1.25, 10.0)
    assert audio_price("openai/whisper-1") == {"per_second": 0.0001}


def test_every_canonical_local_model_is_known_zero_without_catalog_entry() -> None:
    model = "frisket-new-unlisted-model"
    assert model_price(f"ollama/@desk/{model}") == (0.0, 0.0)
    assert audio_price(f"ollama/@desk/{model}") == {"per_second": 0.0}
    assert model_price(f"openai/{model}") is None
    assert audio_price(f"openai/{model}") is None
    assert model_price("ollama/@desk/qwen3:8b") == (0.0, 0.0)
    assert model_price("openrouter/qwen/qwen3-8b") == (0.117, 0.455)


def test_text_cost_carries_local_pinned_and_unknown_source_by_value() -> None:
    assert cost_of_with_source("ollama/@desk/unlisted", 10, 5) == (
        0.0,
        "free_local",
    )
    pinned_cost, pinned_source = cost_of_with_source("openai/gpt-5", 10, 5)
    assert pinned_cost is not None and pinned_cost > 0
    assert pinned_source == "pricing_data"
    assert cost_of_with_source("openai/frisket-unpriced", 10, 5) == (
        None,
        "unknown",
    )


def test_openrouter_open_weight_model_is_curated_remote_and_priced() -> None:
    entries = {entry["id"]: entry for entry in MODEL_ENTRIES["openrouter"]}
    assert entries["qwen/qwen3-8b"]["label"] == (
        "Qwen 3 8B — open weights; requests go to OpenRouter"
    )
    assert model_price("openrouter/qwen/qwen3-8b") == (0.117, 0.455)
    assert model_cost_source("openrouter/qwen/qwen3-8b") == "pricing_data"
    assert model_price("ollama/@desk/qwen3:8b") == (0.0, 0.0)

    assert entries["minimax/minimax-m3"]["label"] == (
        "MiniMax M3 — hosted multimodal OCR via OpenRouter"
    )
    assert model_price("openrouter/minimax/minimax-m3") == (0.3, 1.2)
    assert model_cost_source("openrouter/minimax/minimax-m3") == "pricing_data"


@pytest.mark.parametrize(
    "suffix",
    [":free", "-thinking", "-extra", ":thinking", ":nitro"],
)
def test_openrouter_variant_requires_an_exact_pricing_identity(suffix: str) -> None:
    model = f"openrouter/qwen/qwen3-8b{suffix}"

    assert model_price(model) is None
    assert model_cost_source(model) == "unknown"
    assert cost_of_with_source(model, 10, 5) == (None, "unknown")


def test_openrouter_exact_variant_rate_does_not_price_its_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PRICES, "qwen/qwen3-8b:free", (0.01, 0.02))

    assert model_price("openrouter/qwen/qwen3-8b:free") == (0.01, 0.02)
    assert model_cost_source("openrouter/qwen/qwen3-8b:free") == "pricing_data"
    assert model_pricing("openrouter/qwen/qwen3-8b:free").pricing_key == (
        "openrouter/qwen/qwen3-8b:free.tokens"
    )
    assert model_price("openrouter/qwen/qwen3-8b:free-extra") is None


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai/gpt-5-future-snapshot", (1.25, 10.0)),
        ("anthropic/claude-haiku-4-5-future-snapshot", (1.0, 5.0)),
        ("gemini/gemini-3.6-flash-future-snapshot", (0.75, 3.75)),
    ],
)
def test_direct_provider_family_prefix_pricing_is_unchanged(
    model: str, expected: tuple[float, float]
) -> None:
    assert model_price(model) == expected
    assert model_cost_source(model) == "pricing_data"


def test_web_fallback_is_in_sync_with_canonical_catalog(tmp_path: Path) -> None:
    subprocess.run(
        ["python3", str(ROOT / "scripts/dev/sync_model_catalog.py"), "--check"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


def _request(model: str) -> LLMRequest:
    return LLMRequest(model=model, messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "anthropic/claude-sonnet-5"),
        ("anthropic", "anthropic/claude-fable-5"),
        ("gemini", "gemini/gemini-3.5-flash-lite"),
        ("gemini", "gemini/gemini-3.6-flash"),
    ],
)
async def test_new_families_omit_deprecated_temperature(
    provider: str, model: str
) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "stop_reason": "end_turn",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    router = ModelRouter(keys={provider: "k"}, use_env_keys=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await router._adapters[provider].complete(_request(model), client)  # noqa: SLF001
    assert "temperature" not in seen
