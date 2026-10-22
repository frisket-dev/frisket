"""Reasoning-model sampling through desktop and hosted router construction."""

import asyncio
import json

import httpx
import pytest

from frisket.ai.llm import LLMRequest, ModelRouter


@pytest.mark.parametrize("credentials", ["direct", "org_byok", "platform_key"])
@pytest.mark.parametrize(
    "model,temperature_expected",
    [
        ("gpt-6-luna", False),
        ("gpt-6-astra", False),
        ("gpt-6.1-sol", False),
        ("gpt-5-mini", False),
        ("gpt-4.1-mini", True),
    ],
)
def test_router_omits_custom_temperature_for_reasoning_models(
    monkeypatch, credentials, model, temperature_expected
):
    # GPT-6 Luna defaults to medium reasoning; sampling parameters require
    # reasoning=none. This router preserves the existing GPT-5 omission policy.
    # https://developers.openai.com/api/docs/guides/latest-model
    monkeypatch.setenv("OPENAI_API_KEY", "test-platform")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "choices": [
                    {"message": {"content": '{"answer":3}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async def go():
        if credentials == "platform_key":
            # Standalone hosted worker construction uses environment keys.
            router = ModelRouter(env_key_source="platform_key", max_retries=0)
        elif credentials == "org_byok":
            # Hosted per-organization router supplies explicit key provenance.
            router = ModelRouter(
                keys={"openai": "test-org"},
                key_sources={"openai": "org_byok"},
                env_key_source="platform_key",
                max_retries=0,
            )
        else:
            router = ModelRouter(
                keys={"openai": "test-local"}, use_env_keys=False, max_retries=0
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router._client = client
            return await router.complete(
                LLMRequest(
                    model=f"openai/{model}",
                    messages=[{"role": "user", "content": "Extract the answer"}],
                    schema={
                        "type": "object",
                        "properties": {"answer": {"type": "integer"}},
                        "required": ["answer"],
                    },
                    temperature=0.0,
                )
            )

    response = asyncio.run(go())
    assert response.data == {"answer": 3}
    assert len(bodies) == 1
    assert ("temperature" in bodies[0]) is temperature_expected
    assert bodies[0]["max_completion_tokens"] > 0
    assert bodies[0]["response_format"]["type"] == "json_schema"
