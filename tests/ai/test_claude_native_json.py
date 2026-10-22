"""Claude JSON output, exercised through real adapters without provider calls."""

import asyncio
import copy
import json

import httpx
import pytest

from frisket.ai.llm.adapters import AnthropicAdapter, _anthropic_schema
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import (
    StructuredCompleter,
    StructuredRequest,
    UnsupportedMechanismError,
)
from frisket.ai.llm.structured_probe import ProbeStore
from frisket.ai.llm.types import LLMRequest, SchemaViolation


SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 2},
        "notes": {"type": "string", "maxLength": 10},
    },
    "required": ["score"],
    "additionalProperties": False,
}


def wire(text, stop="end_turn"):
    return {
        "id": "msg-mock",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def run_complete(
    schema,
    outputs,
    *,
    model="anthropic/claude-sonnet-5-5",
    method="auto",
    probe_store=None,
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=outputs[min(len(bodies) - 1, len(outputs) - 1)])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = ModelRouter(keys={}, max_retries=0)
            router._client = client
            router._adapters["anthropic"] = AnthropicAdapter("test")
            return await StructuredCompleter(router, probe_store=probe_store).complete(
                StructuredRequest(
                    model=model,
                    schema=schema,
                    messages=[{"role": "user", "content": "Extract"}],
                    method=method,
                    repair_attempts=1,
                )
            )

    return asyncio.run(go()), bodies


@pytest.mark.parametrize("family", ["sonnet-5-5", "opus-5-5", "haiku-4-5-20251001"])
def test_native_json_wire_and_original_constraint_repair(family):
    original = copy.deepcopy(SCHEMA)
    result, bodies = run_complete(
        SCHEMA,
        [wire('{"score":1}'), wire('{"score":3}')],
        model=f"anthropic/claude-{family}",
    )
    assert result.data == {"score": 3}
    assert result.method_used == "native_strict"
    assert result.attempts == 2
    assert "minimum of 2" in result.violations[0]
    assert SCHEMA == original
    for body in bodies:
        assert "tools" not in body and "tool_choice" not in body
        if family in ("sonnet-5-5", "opus-5-5"):
            assert "temperature" not in body
        else:
            assert body["temperature"] == 0.0
        output = body["output_config"]["format"]
        assert output["type"] == "json_schema"
        lowered = output["schema"]
        assert lowered["required"] == ["score"]  # notes stays optional
        assert lowered["additionalProperties"] is False
        assert "minimum" not in lowered["properties"]["score"]
        assert "minimum" in lowered["properties"]["score"]["description"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "additionalProperties": {"type": "integer"}},
        {
            "type": "object",
            "properties": {"cats": {"type": "integer"}},
            "additionalProperties": True,
        },
    ],
)
def test_open_dictionary_uses_prose_and_preserves_dynamic_keys(schema):
    result, bodies = run_complete(schema, [wire('{"cats":2,"dogs":3}')])
    assert result.data == {"cats": 2, "dogs": 3}
    assert result.method_used == "prose_json"
    assert "output_config" not in bodies[0]
    assert "tools" not in bodies[0]
    assert json.loads(bodies[0]["system"].split("schema:\n", 1)[1]) == schema


def test_explicit_native_strict_uses_native_output():
    result, bodies = run_complete(SCHEMA, [wire('{"score":3}')], method="native_strict")
    assert result.method_used == "native_strict"
    assert "output_config" in bodies[0]


def test_recorded_prose_override_reaches_actual_claude_wire():
    model = "anthropic/claude-sonnet-5-5"
    store = ProbeStore(":memory:")
    try:
        baseline, native_bodies = run_complete(SCHEMA, [wire('{"score":3}')])
        store.record(model, "prose_json", True, "recorded override")
        result, prose_bodies = run_complete(
            SCHEMA, [wire('```json\n{"score":3}\n```')], probe_store=store
        )
        assert baseline.method_used == "native_strict"
        assert "output_config" in native_bodies[0]
        assert result.method_used == "prose_json"
        assert result.data == {"score": 3}
        assert "output_config" not in prose_bodies[0]
        assert "tools" not in prose_bodies[0]
        assert "Respond ONLY with JSON" in prose_bodies[0]["system"]
    finally:
        store.close()


def test_lowering_visits_schema_positions_without_rewriting_literal_data():
    schema = {
        "type": "object",
        "properties": {
            "minimum": {"type": "number", "minimum": 1},
            "nested": {
                "type": ["object", "null"],
                "properties": {"label": {"type": "string", "minLength": 3}},
            },
        },
        "$defs": {
            "list": {"type": "array", "minItems": 3, "items": {"type": "string"}}
        },
        "examples": [{"minimum": 1}],
    }
    original = copy.deepcopy(schema)
    lowered = _anthropic_schema(schema)
    assert schema == original
    assert "minimum" in lowered["properties"]
    assert "minimum" not in lowered["properties"]["minimum"]
    assert lowered["properties"]["nested"]["additionalProperties"] is False
    assert "minLength" not in lowered["properties"]["nested"]["properties"]["label"]
    assert "minItems" not in lowered["$defs"]["list"]
    assert lowered["examples"] == [{"minimum": 1}]


@pytest.mark.parametrize(
    "schema,method",
    [
        (SCHEMA, "native_tool"),
        ({"type": "object"}, "native_strict"),
        (
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "additionalProperties": True,
            },
            "native_strict",
        ),
    ],
)
def test_incompatible_explicit_mechanisms_refuse_before_wire(schema, method):
    with pytest.raises(UnsupportedMechanismError):
        run_complete(schema, [], method=method)


@pytest.mark.parametrize("text", ["I refuse", '{"score":', ""])
def test_native_invalid_text_has_bounded_repair(text):
    result, bodies = run_complete(SCHEMA, [wire(text), wire('{"score":4}')])
    assert result.data == {"score": 4}
    assert len(bodies) == 2


def test_adapter_rejects_explicit_forced_tool_before_wire():
    async def go():
        def handler(request):
            pytest.fail("unsupported mechanism reached the transport")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await AnthropicAdapter("test").complete(
                LLMRequest(
                    model="anthropic/claude-opus-5-5",
                    messages=[{"role": "user", "content": "Extract"}],
                    schema=SCHEMA,
                    mechanism="native_tool",
                ),
                client,
            )

    with pytest.raises(SchemaViolation, match="native_tool"):
        asyncio.run(go())
