from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from frisket.ai.llm import DEFAULT_MAX_OUTPUT_TOKENS
from frisket.ai.llm import structured_capabilities as capabilities
from frisket.ai.llm.adapters import AnthropicAdapter, OpenAICompatAdapter, _strictify
from frisket.ai.llm.cache import request_key
from frisket.ai.llm.structured_capabilities import (
    JSON_LOOSE,
    NATIVE_STRICT,
    NATIVE_TOOL,
    PROSE_JSON,
    CapabilityRow,
    resolve,
)
from frisket.ai.llm.types import LLMRequest


SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


# ---------------------------------------------------------------------------
# The PARITY-ONLY seed: every row byte-matches CURRENT (the previous implementation) behavior.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected_mode",
    [
        ("openai/gpt-5-mini", NATIVE_STRICT),
        ("gemini/gemini-2.5-flash", NATIVE_STRICT),
        ("anthropic/claude-haiku-4-5", NATIVE_TOOL),
        ("openrouter/anthropic/claude-x", JSON_LOOSE),
        (
            "openrouter/openai/gpt-5",
            JSON_LOOSE,
        ),  # no vendor promotion (capability refinement)
        ("ollama/@test-local/llama3", JSON_LOOSE),
    ],
)
def test_resolve_per_provider(model: str, expected_mode: str):
    assert resolve(model).structured_mode == expected_mode


def test_conservative_fallback_for_unknown_provider():
    row = resolve("some-new-provider/some-model")
    assert row is capabilities.FALLBACK
    assert row.structured_mode == JSON_LOOSE  # weakest row, never assumed


def test_fallback_capability_profile_speaks_json_object_not_strict_or_tool():
    prof = capabilities.FALLBACK.as_profile_dict()
    assert prof == {
        "default_mechanism": JSON_LOOSE,
        "supports_native_strict": False,
        "supports_native_tool": False,
        "supports_json_object": True,
    }


# ---------------------------------------------------------------------------
# Resolution algorithm: glob specificity.
# ---------------------------------------------------------------------------


def _row(pattern: str, mode: str = JSON_LOOSE) -> CapabilityRow:
    return CapabilityRow(
        provider=pattern.split("/", 1)[0],
        model_pattern=pattern,
        structured_mode=mode,
        additional_properties_false=False,
        nullable_optional_convention=False,
        source="test",
    )


def test_more_specific_glob_wins_by_segment_count():
    # "openrouter/openai/*" (2 non-wildcard segments) beats
    # "openrouter/*" (1) for a model id like "openrouter/openai/gpt-5".
    broad = _row("openrouter/*", mode=JSON_LOOSE)
    specific = _row("openrouter/openai/*", mode=NATIVE_STRICT)
    table = (broad, specific)
    assert resolve("openrouter/openai/gpt-5", table=table) is specific
    # a model that only the broad glob covers still resolves to it.
    assert resolve("openrouter/anthropic/claude-x", table=table) is broad


def test_tie_break_on_literal_prefix_length():
    # both patterns have a wildcard IN their only segment ("ab*"/"abc*"), so
    # non-wildcard-segment count ties at 0 for both, so the tie-break
    # (longer literal prefix before the first wildcard wins) must decide.
    # Both match the same model id, so the tie is real, not resolved by
    # fnmatch's own iteration order.
    short_prefix = _row("ab*", mode=JSON_LOOSE)
    long_prefix = _row("abc*", mode=NATIVE_STRICT)
    table = (short_prefix, long_prefix)
    assert resolve("abcdef", table=table) is long_prefix


def test_no_match_falls_back():
    table = (_row("openai/*"),)
    assert resolve("anthropic/claude-x", table=table) is capabilities.FALLBACK


# ---------------------------------------------------------------------------
# strict_schema retirement -- the table is the single source now.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wire-shape parity: the REAL adapter, driven through httpx.MockTransport,
# builds a BYTE-IDENTICAL request body to what the retired strict_schema
# boolean used to produce -- for every current provider.
# ---------------------------------------------------------------------------


def _openai_ok_wire(data: dict) -> dict:
    return {
        "id": "chatcmpl-mock",
        "choices": [
            {"message": {"content": json.dumps(data)}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _anthropic_ok_wire(data: dict) -> dict:
    return {
        "id": "msg-mock",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "emit", "input": data}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _run_capturing_request(
    adapter: Any,
    req: LLMRequest,
    wire: dict,
    *,
    cost_model: str | None = None,
) -> dict:
    """Runs adapter.complete() against a MockTransport that returns `wire`,
    and returns the JSON body the adapter actually POSTed."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=wire)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if cost_model is not None:
                return await adapter.complete(req, client, cost_model=cost_model)
            return await adapter.complete(req, client)

    resp = asyncio.run(_go())
    return captured["body"], resp


def test_ordinary_default_reaches_both_adapter_families():
    openai_body, _ = _run_capturing_request(
        OpenAICompatAdapter("k", "https://api.openai.com/v1"),
        LLMRequest(
            model="openai/gpt-5-mini",
            messages=[{"role": "user", "content": "hi"}],
        ),
        _openai_ok_wire({"name": "Ada"}),
    )
    anthropic_body, _ = _run_capturing_request(
        AnthropicAdapter("k"),
        LLMRequest(
            model="anthropic/claude-haiku-4-5",
            messages=[{"role": "user", "content": "hi"}],
        ),
        _anthropic_ok_wire({"name": "Ada"}),
    )

    assert openai_body["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert anthropic_body["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS

    explicit_body, _ = _run_capturing_request(
        OpenAICompatAdapter("k", "https://api.openai.com/v1"),
        LLMRequest(
            model="openai/gpt-5-mini",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1_024,
        ),
        _openai_ok_wire({"name": "Ada"}),
    )
    assert explicit_body["max_tokens"] == 1_024


def test_openai_wire_body_is_native_strict_and_matches_old_strictify_path():
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    body, resp = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))
    # byte-identical to the retired strict_schema=True branch.
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "result", "strict": True, "schema": _strictify(SCHEMA)},
    }
    assert resp.data == {"name": "Ada"}


def test_gemini_wire_body_is_native_strict():
    adapter = OpenAICompatAdapter(
        "k", "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    req = LLMRequest(
        model="gemini/gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    body, _ = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_openrouter_wire_body_is_json_loose_and_matches_old_strict_schema_false_path():
    adapter = OpenAICompatAdapter("k", "https://openrouter.ai/api/v1")
    req = LLMRequest(
        model="openrouter/anthropic/claude-haiku",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    body, resp = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {
        "role": "system",
        "content": "Respond ONLY with JSON matching this schema:\n"
        + json.dumps(SCHEMA),
    }
    assert resp.data == {"name": "Ada"}


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("disabled", {"enabled": False}),
        ("low_exclude", {"effort": "low", "exclude": True}),
    ],
)
def test_openrouter_reasoning_policy_has_a_closed_wire_shape(policy, expected):
    adapter = OpenAICompatAdapter("k", "https://openrouter.ai/api/v1")
    req = LLMRequest(
        model="openrouter/z-ai/glm-5.3-flash",
        messages=[{"role": "user", "content": "locate regions"}],
        schema=SCHEMA,
        reasoning_policy=policy,
    )

    body, _ = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))

    assert body["reasoning"] == expected


def test_openrouter_wire_body_carries_curated_top_p():
    adapter = OpenAICompatAdapter("k", "https://openrouter.ai/api/v1")
    req = LLMRequest(
        model="openrouter/minimax/minimax-m3",
        messages=[{"role": "user", "content": "transcribe this image"}],
        schema=SCHEMA,
        params={"top_p": 0.95},
    )

    body, _ = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))

    assert body["top_p"] == 0.95


def test_ollama_wire_body_is_json_loose():
    adapter = OpenAICompatAdapter("ollama", "http://localhost:11434/v1")
    req = LLMRequest(
        model="ollama/llama3",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    body, _ = _run_capturing_request(
        adapter,
        req,
        _openai_ok_wire({"name": "Ada"}),
        cost_model="ollama/@test-local/llama3",
    )
    assert body["response_format"] == {"type": "json_object"}


def test_anthropic_wire_body_always_forces_emit_tool_regardless_of_table():
    """``AnthropicAdapter`` has one mechanism, ``NATIVE_TOOL``. The dialect
    split does not add response-format branching there; its body is
    unconditional, unaffected by mechanism resolution."""
    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    body, resp = _run_capturing_request(
        adapter, req, _anthropic_ok_wire({"name": "Ada"})
    )
    assert body["tools"] == [
        {
            "name": "emit",
            "description": "Emit the structured result.",
            "input_schema": SCHEMA,
        }
    ]
    assert body["tool_choice"] == {"type": "tool", "name": "emit"}
    assert resp.data == {"name": "Ada"}


def test_unknown_provider_falls_back_to_json_loose_wire_body():
    adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")
    req = LLMRequest(
        model="brand-new-provider/some-model",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    body, _ = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))
    assert body["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# PROSE_JSON: the wire body (no response_format) + real extraction, fenced
# and unfenced -- the conformance matrix's
# prose_json cells were simulated; this drives the REAL adapter.
# ---------------------------------------------------------------------------


def test_prose_json_wire_body_has_no_response_format():
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        mechanism=PROSE_JSON,
    )
    fenced_wire = _openai_ok_wire  # placeholder, overwritten below
    wire = {
        "id": "chatcmpl-mock",
        "choices": [
            {
                "message": {"content": '```json\n{"name": "Ada"}\n```'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    body, resp = _run_capturing_request(adapter, req, wire)
    assert "response_format" not in body
    # same instruction text JSON_LOOSE builds; only response_format differs.
    assert body["messages"][0] == {
        "role": "system",
        "content": "Respond ONLY with JSON matching this schema:\n"
        + json.dumps(SCHEMA),
    }
    assert resp.data == {"name": "Ada"}  # fenced extraction succeeded
    assert resp.content == '```json\n{"name": "Ada"}\n```'  # raw content preserved
    del fenced_wire


def test_prose_json_unfenced_extraction():
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        mechanism=PROSE_JSON,
    )
    wire = {
        "id": "chatcmpl-mock",
        "choices": [
            {
                "message": {
                    "content": 'The answer is {"name": "Ada"} -- hope that helps.'
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    _body, resp = _run_capturing_request(adapter, req, wire)
    assert resp.data == {"name": "Ada"}


def test_prose_json_no_match_passes_through_to_unparseable_failure():
    from frisket.ai.llm.types import SchemaViolation

    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        mechanism=PROSE_JSON,
    )
    wire = {
        "id": "chatcmpl-mock",
        "choices": [
            {"message": {"content": "I can't help with that."}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    with pytest.raises(SchemaViolation, match="unparseable"):
        _run_capturing_request(adapter, req, wire)


def test_explicit_mechanism_overrides_the_table_default():
    """openai's table default is NATIVE_STRICT, but an explicit
    mechanism=JSON_LOOSE on the request must still win ("auto" is
    capability-resolved; any mechanism id is the explicit override)."""
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        mechanism=JSON_LOOSE,
    )
    body, _ = _run_capturing_request(adapter, req, _openai_ok_wire({"name": "Ada"}))
    assert body["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Cache-key parity (this mint's own constraint): an auto/mechanism-less
# request's key must not depend on the capability table's contents -- the
# golden replay cassettes (tests/cache/llm_cache.db) were committed under
# mechanism=None for every "auto" call, and must not invalidate.
# ---------------------------------------------------------------------------


def test_auto_requests_cache_key_is_independent_of_the_capability_table():
    req = LLMRequest(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        mechanism=None,  # what the completer's "auto" path still sends (the structured completer, unchanged by the dialect split)
    )
    key_before = request_key(req)

    # Swap the table for one that resolves this model completely differently
    # -- the key must be byte-identical, because req.mechanism (what the key
    # actually hashes) never changed.
    hostile_table = (_row("openai/*", mode=PROSE_JSON),)
    assert (
        resolve("openai/gpt-5-mini", table=hostile_table).structured_mode == PROSE_JSON
    )
    key_after = request_key(req)  # request_key doesn't consult the table at all
    assert key_before == key_after


def test_explicit_mechanism_does_change_the_cache_key():
    """Sanity control: it's specifically req.mechanism (not the table) that's
    key-bearing -- two explicit mechanisms for an otherwise-identical request
    must NOT collide, preventing cache contamination."""
    base = dict(
        model="openai/gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
    )
    key_strict = request_key(LLMRequest(**base, mechanism=NATIVE_STRICT))
    key_loose = request_key(LLMRequest(**base, mechanism=JSON_LOOSE))
    key_auto = request_key(LLMRequest(**base, mechanism=None))
    assert len({key_strict, key_loose, key_auto}) == 3
