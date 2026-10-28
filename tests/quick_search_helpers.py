"""Offline model transport shared by Quick Search integration tests."""

import json

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.ai.llm.adapters import AnthropicAdapter


def patch_quick_search_model(monkeypatch):
    calls = []

    async def complete(_adapter, request, client):
        calls.append(request)
        assert request.tools is None, "Quick Search must not start a tool loop"
        data = {"answer": "An answer supported by the search snippets."}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=20,
            tokens_out=10,
            cost=0.001,
            model=request.model,
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "quick-search-test-key")
    monkeypatch.setattr(AnthropicAdapter, "complete", complete)
    return calls


def quick_search_router():
    return ModelRouter(
        keys={"anthropic": "quick-search-test-key"},
        cache=None,
        cache_mode="off",
    )
