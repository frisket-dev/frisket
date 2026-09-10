"""Opt-in live proof for an explicitly configured Ollama endpoint.

`tests/test_llm_live.py` replays committed cassettes for anthropic/gemini/
openai only; nothing in the default suite talks to a real Ollama daemon.
This suite is the live proof: opt-in, $0, against an explicitly selected
endpoint.

Gate semantics: without FRISKET_OLLAMA_LIVE=1 every test SKIPS (and the
`network` marker keeps it out of the default suite). WITH the flag, a missing
daemon or missing model is a FAILURE carrying the exact remediation command —
never a skip; a live proof that silently skips proves nothing.

The pinned models are deliberately tiny: small enough for CI CPU, and bad
enough at strict schemas that the structured-output repair path gets exercised
on real malformed output instead of simulated fixtures. Env overrides exist
for machine differences, but the pinned defaults are the contract.

    ollama pull qwen3:0.6b && ollama pull qwen2.5vl:3b
    FRISKET_OLLAMA_LIVE=1 uv run pytest -m network -q tests/ai/test_ollama_live.py

The explicit ``-m network`` opts into tests that contact the local server.
"""

import asyncio
import base64
import os
import struct
import zlib

import httpx
import pytest

from frisket.ai.llm import LLMError, LLMRequest, ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

LIVE = os.environ.get("FRISKET_OLLAMA_LIVE") == "1"
pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not LIVE, reason="live Ollama proof: set FRISKET_OLLAMA_LIVE=1 to run"
    ),
]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
TEXT_MODEL = os.environ.get("FRISKET_OLLAMA_TEXT_MODEL", "qwen3:0.6b")
VL_MODEL = os.environ.get("FRISKET_OLLAMA_VL_MODEL", "qwen2.5vl:3b")


_DAEMON_PROBE: dict[str, str | None] = {}


def _probe_daemon() -> str | None:
    """None when the daemon is up with both pinned models; else remediation text."""
    try:
        tags = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
    except Exception as e:  # noqa: BLE001 — any transport failure gets remediation
        return (
            f"FRISKET_OLLAMA_LIVE=1 but no Ollama daemon at {OLLAMA_URL} "
            f"({type(e).__name__}: {e}). Start it with `ollama serve` "
            "(or point OLLAMA_URL at a running daemon)."
        )
    present = {m.get("name", "") for m in tags.get("models", [])}
    missing = [
        m
        for m in (TEXT_MODEL, VL_MODEL)
        if m not in present and f"{m}:latest" not in present
    ]
    if missing:
        return (
            "Ollama daemon is up but required models are not pulled: "
            + ", ".join(missing)
            + ". Run: "
            + " && ".join(f"ollama pull {m}" for m in missing)
        )
    return None


def require_daemon() -> None:
    """In-test probe (not a fixture) so an absent daemon is a semantic FAILED
    with remediation text, never a setup ERROR — the admission convention."""
    if "result" not in _DAEMON_PROBE:
        _DAEMON_PROBE["result"] = _probe_daemon()
    if _DAEMON_PROBE["result"]:
        pytest.fail(_DAEMON_PROBE["result"])


def make_router() -> ModelRouter:
    return ModelRouter(
        cache=None,
        cache_mode="off",
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="live",
                display_name="Live Ollama",
                origin=OLLAMA_URL,
                source="env",
            ),
        ),
    )


def _run(coro):
    return asyncio.run(coro)


def test_text_structured_output():
    """A tiny local model produces schema-valid structured output end-to-end.

    The enum-constrained ask is easy enough that a 0.6B model answers it
    correctly; getting there through the strict schema is the machinery under
    test (openai-compat structured path + repair on malformed attempts)."""
    require_daemon()

    async def run():
        router = make_router()
        try:
            resp = await router.complete(
                LLMRequest(
                    model=f"ollama/@live/{TEXT_MODEL}",
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "Classify the topic of this sentence:\n\n"
                                "The Yankees won the baseball game 5-3 "
                                "last night."
                            ),
                        }
                    ],
                    schema={
                        "type": "object",
                        "properties": {
                            "topic": {
                                "type": "string",
                                "enum": ["sports", "politics", "weather"],
                            }
                        },
                        "required": ["topic"],
                    },
                    max_tokens=2000,
                )
            )
        finally:
            await router.aclose()
        assert resp.data is not None, "structured path returned no parsed data"
        assert resp.data["topic"] == "sports"

    _run(run())


def _solid_png(width: int = 64, height: int = 64, rgb=(255, 0, 0)) -> bytes:
    """A dependency-free solid-color PNG (stdlib only, deterministic)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def test_vision_image_structured_output():
    """An image part flows through the openai-compat data-url conversion
    (src/frisket/ai/llm/adapters.py:402-404) to a local VL model and comes back
    as schema-valid structured output naming the obvious color."""
    require_daemon()

    async def run():
        router = make_router()
        try:
            resp = await router.complete(
                LLMRequest(
                    model=f"ollama/@live/{VL_MODEL}",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "What single solid color fills this "
                                        "entire image?"
                                    ),
                                },
                                {
                                    "type": "image",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(_solid_png()).decode(),
                                },
                            ],
                        }
                    ],
                    schema={
                        "type": "object",
                        "properties": {"color": {"type": "string"}},
                        "required": ["color"],
                    },
                    max_tokens=2000,
                )
            )
        finally:
            await router.aclose()
        assert resp.data is not None, "structured path returned no parsed data"
        assert "red" in resp.data["color"].lower()

    _run(run())


def test_unknown_model_is_a_typed_error():
    """A model that isn't pulled surfaces as a typed LLMError, not a raw
    transport traceback or a hang — the error-remediation layer
    (onboard-error-remediation-v1) can only translate what arrives typed."""
    require_daemon()

    async def run():
        router = make_router()
        try:
            with pytest.raises(LLMError):
                await router.complete(
                    LLMRequest(
                        model="ollama/@live/frisket-definitely-not-a-model",
                        messages=[{"role": "user", "content": "hello"}],
                        max_tokens=32,
                    )
                )
        finally:
            await router.aclose()

    _run(run())
