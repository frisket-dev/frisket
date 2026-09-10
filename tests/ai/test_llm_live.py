"""Success-path tests against the committed real-response cache.

Default mode is replay_strict: no keys needed, fails loudly on a miss.
To (re)populate after changing any prompt/schema here:
    set -a; source .secrets/frisket.env; set +a
    FRISKET_CACHE_REFRESH=1 FRISKET_CACHE_MODE=replay uv run pytest tests/test_llm_live.py
then commit tests/cache/llm_cache.db. That run spends real (sub-cent) money.
"""

import asyncio
import os

import pytest

from frisket.ai.llm import LLMRequest, ModelRouter, ResponseCache
from frisket.testing import llm_cache_path

CACHE_PATH = llm_cache_path()
MODE = os.environ.get("FRISKET_CACHE_MODE", "replay_strict")

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-10 relevance to food safety"},
        "justification": {"type": "string"},
    },
    "required": ["score", "justification"],
}

SNIPPET = (
    "The senator claimed that imported produce is routinely sprayed with "
    "chemicals banned for use domestically, calling for new inspections."
)


def make_router() -> ModelRouter:
    return ModelRouter(cache=ResponseCache(CACHE_PATH), cache_mode=MODE)


def classify_request(model: str) -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You score text snippets for topical relevance.",
            },
            {
                "role": "user",
                "content": f"Score this for food-safety relevance 0-10:\n\n{SNIPPET}",
            },
        ],
        schema=CLASSIFY_SCHEMA,
        max_tokens=300,
    )


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-haiku-4-5",  # native adapter, forced-tool structured output
        "gemini/gemini-2.5-flash-lite",  # openai-compat adapter, json_object fallback
        "openai/gpt-5-mini",  # openai-compat adapter, strict json_schema
    ],
)
def test_structured_classify(model):
    async def run():
        router = make_router()
        try:
            resp = await router.complete(classify_request(model))
        finally:
            await router.aclose()
        assert resp.data is not None
        assert isinstance(resp.data["score"], int)
        assert 0 <= resp.data["score"] <= 10
        assert resp.data["score"] >= 5, "snippet is clearly food-safety relevant"
        assert len(resp.data["justification"]) > 10
        if not resp.cached:
            assert resp.tokens_in > 0 and resp.cost > 0

    asyncio.run(run())


def test_plain_text_completion():
    async def run():
        router = make_router()
        try:
            resp = await router.complete(
                LLMRequest(
                    model="anthropic/claude-haiku-4-5",
                    messages=[
                        {
                            "role": "user",
                            "content": "Reply with exactly the word: frisket",
                        }
                    ],
                    max_tokens=10,
                )
            )
        finally:
            await router.aclose()
        assert "frisket" in (resp.content or "").lower()

    asyncio.run(run())
