"""Native records, genuine dictionaries, and nullable absence on real wire shapes."""

import asyncio
import json

import httpx
import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.adapters import _strictify
from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
from frisket.ops.extraction import extract_response_schema


def complete(model, schema, data, *, params=None):
    bodies = []
    provider = model.split("/", 1)[0]

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if provider == "anthropic":
            content = (
                [{"type": "tool_use", "name": "emit", "input": data, "id": "emit-1"}]
                if "tools" in body
                else [{"type": "text", "text": json.dumps(data)}]
            )
            payload = {
                "id": "msg-1",
                "content": content,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        else:
            payload = {
                "id": "chat-1",
                "choices": [
                    {"message": {"content": json.dumps(data)}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        return httpx.Response(200, json=payload)

    async def go():
        router = ModelRouter(keys={provider: "test"}, use_env_keys=False, max_retries=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router._client = client
            return await StructuredCompleter(router).complete(
                StructuredRequest(
                    model=model,
                    schema=schema,
                    params=params or {},
                    messages=[{"role": "user", "content": "Extract"}],
                )
            )

    return asyncio.run(go()), bodies[0]


@pytest.mark.parametrize(
    "model,mode",
    [
        ("openai/gpt-6-luna", "native_strict"),
        ("gemini/gemini-2.5-flash", "native_strict"),
        ("anthropic/claude-sonnet-5-5", "native_strict"),
        ("anthropic/claude-sonnet-4-5", "native_tool"),
    ],
)
def test_closed_nullable_records_keep_native_enforcement(model, mode):
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "page": {"type": ["integer", "null"]},
            "snippet": {"type": ["string", "null"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    result, body = complete(
        model, schema, {"name": "Ada", "page": None, "snippet": None}
    )
    assert result.data == {"name": "Ada", "page": None, "snippet": None}
    assert result.method_used == mode
    if model.startswith("anthropic/"):
        if mode == "native_tool":
            assert body["tool_choice"] == {"type": "tool", "name": "emit"}
            assert "output_config" not in body
        else:
            assert "output_config" in body and "tool_choice" not in body
            assert "temperature" not in body
    else:
        sent = body["response_format"]["json_schema"]["schema"]
        assert sent["required"] == ["name", "page", "snippet"]
        assert sent["properties"]["page"]["type"] == ["integer", "null"]


def test_optional_nonnullable_field_keeps_omission_semantics_with_prose():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    result, body = complete("openai/gpt-6-luna", schema, {"name": "Ada"})
    assert result.data == {"name": "Ada"}
    assert result.method_used == "prose_json"
    assert "response_format" not in body


def test_optional_nullable_reference_stays_native():
    schema = {
        "type": "object",
        "properties": {"page": {"$ref": "#/$defs/page"}},
        "$defs": {"page": {"type": ["integer", "null"]}},
        "additionalProperties": False,
    }
    result, body = complete("openai/gpt-6-luna", schema, {"page": None})
    assert result.data == {"page": None}
    assert result.method_used == "native_strict"
    assert "response_format" in body


@pytest.mark.parametrize(
    "model,mode",
    [
        ("openai/gpt-6-luna", "native_strict"),
        ("gemini/gemini-2.5-flash", "native_strict"),
        ("anthropic/claude-sonnet-5-5", "native_strict"),
        ("anthropic/claude-sonnet-4-5", "native_tool"),
    ],
)
def test_actual_grounded_extract_schema_stays_native_with_box_definitions(model, mode):
    schema = extract_response_schema(
        [{"name": "person", "schema": {"type": "string"}}], grounding_enabled=True
    )
    assert "EvidenceBox" in schema["$defs"]
    data = {
        "person": {
            "value": "Ada",
            "evidence": [
                {
                    "source": "document",
                    "page": 2,
                    "snippet": "Ada",
                    "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4},
                }
            ],
        }
    }
    result, body = complete(model, schema, data)
    assert result.data == data
    assert result.method_used == mode
    if model.startswith("anthropic/"):
        assert ("output_config" in body) is (mode == "native_strict")
        assert ("tools" in body) is (mode == "native_tool")
    else:
        assert body["response_format"]["type"] == "json_schema"


def test_strictify_never_rewrites_enum_default_or_example_objects():
    literal = {"type": "object", "properties": {"x": {"type": "string"}}}
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": ["object", "null"],
                "enum": [literal, None],
                "default": literal,
            }
        },
        "required": ["value"],
        "examples": [literal],
    }
    sent = _strictify(schema)
    assert sent["properties"]["value"]["enum"] == [literal, None]
    assert sent["properties"]["value"]["default"] == literal
    assert sent["examples"] == [literal]
    assert "additionalProperties" not in literal


@pytest.mark.parametrize("model", ["openai/gpt-6-luna", "openrouter/openai/gpt-6-luna"])
def test_reasoning_sampling_omitted_on_direct_and_openrouter_routes(model):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    _, body = complete(model, schema, {"answer": 3}, params={"top_p": 0.7})
    assert "temperature" not in body
    assert "top_p" not in body
