"""Exercise real adapters and repair/accounting with malformed provider replies."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
import pytest

from frisket.ai.llm.cache import ResponseCache, request_key
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import (
    LLMError,
    LLMRequest,
    OutputLimitReached,
    SchemaViolation,
)

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}
MODELS = {
    "openrouter": "openrouter/minimax/minimax-m3",
    "openai": "openai/gpt-5-mini",
    "gemini": "gemini/gemini-3.5-flash-lite",
    "anthropic": "anthropic/claude-sonnet-5-5",
}
SECRET = "fixture-credential-do-not-expose"


class UsagePolicy:
    def __init__(self):
        self.responses = []

    def prepare(self, request, credential_source):
        return request

    def before_live(self, request, credential_source):
        pass

    def after_live(self, request, response, credential_source):
        self.responses.append(response)


@asynccontextmanager
async def provider(*replies, name="openrouter", cache=None):
    bodies = []
    policy = UsagePolicy()

    def respond(request):
        bodies.append(json.loads(request.content))
        content, finish = replies[min(len(bodies) - 1, len(replies) - 1)]
        if finish == "http-error":
            return httpx.Response(400, json={"error": {"message": content}})
        if name == "anthropic":
            wire = {
                "id": f"response-{len(bodies)}",
                "content": [{"type": "text", "text": content}],
                "stop_reason": "max_tokens" if finish == "length" else "end_turn",
                "usage": {"input_tokens": 42, "output_tokens": 17},
            }
        else:
            wire = {
                "id": f"response-{len(bodies)}",
                "choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 17},
            }
        return httpx.Response(200, json=wire)

    router = ModelRouter(
        keys={name: SECRET},
        use_env_keys=False,
        cache=cache,
        model_call_policy=policy,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router._client = client
        yield router, bodies, policy


def request(name="openrouter", **kwargs):
    return LLMRequest(
        model=MODELS[name],
        messages=[{"role": "user", "content": "Summarize this."}],
        schema=SCHEMA,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MODELS)
@pytest.mark.parametrize(
    "content", ["]]]}}]]", '{"summary":"cut off', '{"summary":"prefix"}']
)
async def test_output_limit_precedes_parsing_and_never_repairs(name, content):
    async with provider((content, "length"), name=name) as (router, bodies, policy):
        with pytest.raises(OutputLimitReached) as raised:
            await router.complete(request(name))
    assert len(bodies) == len(policy.responses) == len(raised.value.wire_calls) == 1
    call = raised.value.wire_calls[0]
    assert call.content == content
    assert call.output_limited
    assert (call.tokens_in, call.tokens_out) == (42, 17)
    assert call.provider == name
    assert call.credential_source == "local"
    assert call.duration_ms is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MODELS)
async def test_malformed_then_repaired_keeps_both_calls_and_does_not_cache_failure(
    tmp_path, name
):
    cache = ResponseCache(tmp_path / "cache.db")
    try:
        async with provider(
            ("]]]}}]]", "stop"),
            ('{"summary":"Recovered"}', "stop"),
            name=name,
            cache=cache,
        ) as (router, bodies, policy):
            result = await router.complete(request(name))
        assert result.data == {"summary": "Recovered"}
        assert len(bodies) == len(policy.responses) == 2
        assert (result.tokens_in, result.tokens_out) == (84, 34)
        assert result.cost == pytest.approx(sum(r.cost for r in policy.responses))
        assert [r.raw["id"] for r in policy.responses] == ["response-1", "response-2"]
        assert policy.responses[0].content == "]]]}}]]"
        assert all(
            r.provider == name and r.duration_ms is not None for r in policy.responses
        )
        assert cache.get(request_key(request(name))) is None
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_explicit_retry_after_output_limit_reaches_provider(tmp_path):
    cache = ResponseCache(tmp_path / "cache.db")
    try:
        async with provider(
            ('{"summary":"cut off', "length"),
            ('{"summary":"Recovered"}', "stop"),
            cache=cache,
        ) as (router, bodies, policy):
            with pytest.raises(OutputLimitReached):
                await router.complete(request())
            assert cache.get(request_key(request())) is None
            result = await router.complete(request())
        assert len(bodies) == len(policy.responses) == 2
        assert result.data == {"summary": "Recovered"}
        assert not result.cached
        assert cache.get(request_key(request())).data == result.data
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_exhausted_malformed_replies_preserve_usage_and_redact_credentials():
    async with provider((f"]]]}}]] {SECRET}", "stop")) as (router, bodies, policy):
        with pytest.raises(SchemaViolation) as raised:
            await router.complete(request())
    assert len(bodies) == len(policy.responses) == len(raised.value.wire_calls) == 2
    assert sum(call.tokens_out for call in raised.value.wire_calls) == 34
    serialized = json.dumps([asdict(call) for call in raised.value.wire_calls])
    assert SECRET not in serialized
    assert SECRET not in raised.value.raw_text
    assert "[REDACTED]" in serialized
    assert [call.raw["id"] for call in raised.value.wire_calls] == [
        "response-1",
        "response-2",
    ]


@pytest.mark.asyncio
async def test_oversized_invalid_data_cannot_break_error_redaction():
    content = json.dumps({"summary": [SECRET] * 300})
    async with provider((content, "stop")) as (router, bodies, policy):
        with pytest.raises(SchemaViolation) as raised:
            await router.complete(request())
    assert len(bodies) == len(policy.responses) == len(raised.value.wire_calls) == 2
    assert sum(call.tokens_out for call in raised.value.wire_calls) == 34
    serialized = json.dumps([asdict(call) for call in raised.value.wire_calls])
    assert SECRET not in serialized
    assert "[TRUNCATED]" in serialized
    assert [call.raw["id"] for call in raised.value.wire_calls] == [
        "response-1",
        "response-2",
    ]


@pytest.mark.asyncio
async def test_repair_transport_failure_retains_prior_paid_malformed_call():
    async with provider(
        ("]]]}", "stop"), ("provider refused repair", "http-error")
    ) as (router, bodies, policy):
        with pytest.raises(LLMError) as raised:
            await router.complete(request())
    assert len(bodies) == 2
    assert raised.value.status == 400
    assert len(policy.responses) == len(raised.value.wire_calls) == 1
    assert raised.value.wire_calls[0].tokens_out == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MODELS)
@pytest.mark.parametrize("mechanism", [None, "prose_json", "native_strict"])
async def test_only_openrouter_format_requests_require_parameter_support(
    name, mechanism
):
    async with provider(('{"summary":"OK"}', "stop"), name=name) as (router, bodies, _):
        await router.complete(request(name, mechanism=mechanism))
    if name == "openrouter" and mechanism != "prose_json":
        assert bodies[0]["provider"] == {"require_parameters": True}
    else:
        assert "provider" not in bodies[0]


@pytest.mark.asyncio
async def test_openrouter_plain_text_is_not_restricted_to_json_endpoints():
    async with provider(("A plain answer", "stop")) as (router, bodies, _):
        result = await router.complete(
            LLMRequest(model=MODELS["openrouter"], messages=[])
        )
    assert result.content == "A plain answer"
    assert "provider" not in bodies[0]
    assert "response_format" not in bodies[0]


@pytest.mark.asyncio
async def test_plain_text_output_limit_preserves_existing_cache_behavior(tmp_path):
    cache = ResponseCache(tmp_path / "cache.db")
    try:
        req = LLMRequest(model=MODELS["openrouter"], messages=[])
        async with provider(("A partial answer", "length"), cache=cache) as (
            router,
            bodies,
            policy,
        ):
            first = await router.complete(req)
            replay = await router.complete(req)
        assert len(bodies) == len(policy.responses) == 1
        assert first.content == replay.content == "A partial answer"
        assert replay.cached
        assert replay.output_limited
    finally:
        cache.close()


@pytest.mark.parametrize("name", MODELS)
@pytest.mark.parametrize("schema", [None, SCHEMA])
def test_cache_invalidates_only_openrouter_schema_calls(name, schema):
    req = LLMRequest(model=MODELS[name], messages=[], schema=schema)
    prior_payload = {
        "v": 3,
        "model": req.model,
        "recipe_version": "1",
        "messages": [],
        "schema": schema,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "params": req.params,
        "mechanism": None,
    }
    prior_key = hashlib.sha256(
        json.dumps(prior_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    assert (request_key(req) != prior_key) == (
        name == "openrouter" and schema is not None
    )
