"""REAL remote embeddings (no fake gateway) — the live-demo proof for the remote path.

Like the native stack, remote embeddings were only ever exercised with fake gateways.
This burns a real call per provider whose key is present (skipped otherwise, so CI stays
deterministic) and asserts the LIVE dimension matches the catalog — which is exactly how
we caught the Gemini bug (catalog said 768/text-embedding-004; live is 3072/
gemini-embedding-001). Run with OPENAI_API_KEY / GEMINI_API_KEY exported.
"""

from __future__ import annotations

import os

import pytest

from frisket.ai.embeddings import EmbeddingGateway
from frisket.ai.embeddings.capabilities import resolve_embedding_capability
from frisket.ai.llm import ModelRouter

_CASES = [
    ("openai", "text-embedding-3-small", "OPENAI_API_KEY"),
    ("gemini", "gemini-embedding-001", "GEMINI_API_KEY"),
]


@pytest.mark.parametrize("provider,model,key_env", _CASES)
def test_remote_embedding_live_matches_catalog(provider, model, key_env):
    if not os.environ.get(key_env):
        pytest.skip(f"{key_env} not set — remote live check skipped")
    # a fresh gateway/router per provider (own httpx client) avoids the cross-loop
    # client reuse that bites a long-lived worker (tracked separately).
    gw = EmbeddingGateway(router=ModelRouter())
    res = gw.embed(["hello world"], provider=provider, model=model, modality="text")
    vec = res["vectors"][0]
    assert vec and res["actual_model_id"]
    cap = resolve_embedding_capability(modality="text", provider=provider, model=model)
    assert cap is not None
    # the LIVE dimension must equal what the catalog advertises (a wrong dim would
    # corrupt the embedding space at refresh).
    assert len(vec) == cap["dimensions"][0]


def test_openrouter_embedding_live():
    # OpenRouter is the custom-remote path (one key, many models via the OpenAI-shaped
    # adapter) — verified, not catalog-enumerated. Routes by its own model slug.
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — OpenRouter live check skipped")
    gw = EmbeddingGateway(router=ModelRouter())
    res = gw.embed(
        ["hello world"],
        provider="openrouter",
        model="openai/text-embedding-3-small",
        modality="text",
    )
    assert len(res["vectors"][0]) == 1536  # real vector through OpenRouter
