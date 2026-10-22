"""Dynamic JSON answers must survive strict-provider wire shaping."""

import asyncio
import json

import httpx
import pytest

from frisket.ai.llm import LLMRequest, ModelRouter
from frisket.ai.llm.structured import (
    StructuredCompleter,
    StructuredRequest,
    UnsupportedMechanismError,
)
from frisket.ai.llm.types import SchemaViolation


ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"anyOf": [{"type": "object"}, {"type": "null"}]},
        "answer_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "answer_confidence"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "model",
    ["openai/gpt-6-luna", "gemini/gemini-2.5-flash", "anthropic/claude-sonnet-5-5"],
)
def test_json_answer_and_confidence_preserve_dynamic_keys_and_local_bounds(model):
    provider = model.split("/", 1)[0]
    requests = []
    values = [
        {"answer": {"city": "Leeds", "count": 4}, "answer_confidence": 2},
        {"answer": {"city": "Leeds", "count": 4}, "answer_confidence": 0.8},
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        text = json.dumps(values[len(requests) - 1])
        response = (
            {
                "id": "msg-mock",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
            if provider == "anthropic"
            else {
                "id": "chatcmpl-mock",
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        return httpx.Response(200, json=response)

    async def go():
        router = ModelRouter(keys={provider: "test"}, use_env_keys=False, max_retries=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router._client = client
            return await StructuredCompleter(router).complete(
                StructuredRequest(
                    model=model,
                    schema=ASK_SCHEMA,
                    messages=[{"role": "user", "content": "Answer as JSON"}],
                    repair_attempts=1,
                )
            )

    result = asyncio.run(go())
    assert result.data == values[1]
    assert result.method_used == "prose_json"
    assert result.attempts == 2
    assert "maximum of 1" in result.violations[0]
    for body in requests:
        assert "response_format" not in body
        assert "output_config" not in body
        assert "tools" not in body


def test_openai_explicit_strict_refuses_open_answer_before_wire():
    async def go():
        def handler(request):
            pytest.fail("unrepresentable schema reached the provider")

        router = ModelRouter(keys={"openai": "test"}, use_env_keys=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router._client = client
            return await router.complete_transport(
                LLMRequest(
                    model="openai/gpt-6-luna",
                    schema=ASK_SCHEMA,
                    mechanism="native_strict",
                    messages=[{"role": "user", "content": "Answer as JSON"}],
                )
            )

    with pytest.raises(SchemaViolation, match="open JSON dictionaries"):
        asyncio.run(go())

    async def complete():
        return await StructuredCompleter(
            ModelRouter(keys={}, use_env_keys=False)
        ).complete(
            StructuredRequest(
                model="openai/gpt-6-luna",
                schema=ASK_SCHEMA,
                method="native_strict",
                messages=[{"role": "user", "content": "Answer as JSON"}],
            )
        )

    with pytest.raises(UnsupportedMechanismError, match="open JSON dictionaries"):
        asyncio.run(complete())
