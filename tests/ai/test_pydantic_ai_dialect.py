from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic_ai import Agent

from frisket.ai.llm.adapters import AnthropicAdapter, OpenAICompatAdapter
from frisket.ai.llm.cache import request_key
from frisket.ai.llm.pricing import cost_of
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import FrisketRouterModel
from frisket.ai.llm.types import LLMRequest

# ---------------------------------------------------------------------------
# Two-tool fixture for the dialect-extension tests.
# ---------------------------------------------------------------------------

SEARCH_TOOL = {
    "name": "search",
    "description": "search the web",
    "parameters": {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    },
}
FETCH_TOOL = {
    "name": "fetch",
    "description": "fetch a url",
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AnthropicAdapter: multi-tool wire body + tool_choice=auto + named tool_use
# parsing (bypassing the single forced "emit" tool this dialect carried
# before the multi-tool dialect).
# ---------------------------------------------------------------------------


def _capturing_transport(bodies: list[dict]) -> tuple[httpx.MockTransport, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = bodies[min(len(seen) - 1, len(bodies) - 1)]
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), seen


def _anthropic_tool_use_wire(
    name: str, tool_input: dict, *, tool_id: str = "t1"
) -> dict:
    return {
        "id": "msg-mock",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _anthropic_text_wire(text: str) -> dict:
    return {
        "id": "msg-mock",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_anthropic_multi_tool_body_carries_all_tools_with_auto_choice():
    transport, seen = _capturing_transport(
        [_anthropic_tool_use_wire("search", {"q": "cats"})]
    )
    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "find cats"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert len(seen) == 1
    wire_tools = seen[0]["tools"]
    assert [t["name"] for t in wire_tools] == ["search", "fetch"]
    assert wire_tools[0]["input_schema"] == SEARCH_TOOL["parameters"]
    assert seen[0]["tool_choice"] == {"type": "auto"}


def test_anthropic_named_tool_use_round_trips_to_tool_calls():
    transport, _ = _capturing_transport(
        [_anthropic_tool_use_wire("fetch", {"url": "http://x"}, tool_id="call_9")]
    )
    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    resp = run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert resp.tool_calls == [
        {"name": "fetch", "args": {"url": "http://x"}, "id": "call_9"}
    ]
    assert resp.data is None  # no schema on this request -- data stays unused


def test_anthropic_no_tool_call_leaves_tool_calls_none():
    transport, _ = _capturing_transport([_anthropic_text_wire("done: found 2 cats")])
    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    resp = run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert resp.tool_calls is None
    assert resp.content == "done: found 2 cats"


def test_anthropic_forced_emit_tool_unaffected_by_tools_field():
    """`schema` still wins over `tools` -- structured-output contract
    (forced single "emit" tool) is unchanged by multi-tool support."""
    transport, seen = _capturing_transport(
        [
            {
                "id": "msg-mock",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "name": "emit", "input": {"name": "Ada"}}
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        ]
    )
    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "go"}],
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
        tools=[SEARCH_TOOL],  # ignored -- schema takes precedence
    )
    resp = run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert [t["name"] for t in seen[0]["tools"]] == ["emit"]
    assert seen[0]["tool_choice"] == {"type": "tool", "name": "emit"}
    assert resp.data == {"name": "Ada"}


# ---------------------------------------------------------------------------
# OpenAICompatAdapter: the same three properties, OpenAI-compat dialect.
# ---------------------------------------------------------------------------


def _openai_capturing_transport(
    bodies: list[dict],
) -> tuple[httpx.MockTransport, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = bodies[min(len(seen) - 1, len(bodies) - 1)]
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), seen


def _openai_tool_call_wire_raw(
    name: str, arguments: str, *, call_id: str = "call_1"
) -> dict:
    return {
        "id": "chat-mock",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _openai_tool_call_wire(name: str, args: dict, *, call_id: str = "call_1") -> dict:
    return _openai_tool_call_wire_raw(name, json.dumps(args), call_id=call_id)


def _openai_text_wire(text: str) -> dict:
    return {
        "id": "chat-mock",
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_openai_multi_tool_body_carries_all_tools_with_auto_choice():
    transport, seen = _openai_capturing_transport(
        [_openai_tool_call_wire("search", {"q": "cats"})]
    )
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5",
        messages=[{"role": "user", "content": "find cats"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    wire_tools = seen[0]["tools"]
    assert [t["function"]["name"] for t in wire_tools] == ["search", "fetch"]
    assert wire_tools[0]["type"] == "function"
    assert wire_tools[0]["function"]["parameters"] == SEARCH_TOOL["parameters"]
    assert seen[0]["tool_choice"] == "auto"


def test_openai_named_tool_calls_round_trip():
    transport, _ = _openai_capturing_transport(
        [_openai_tool_call_wire("fetch", {"url": "http://x"}, call_id="call_9")]
    )
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    resp = run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert resp.tool_calls == [
        {
            "name": "fetch",
            "args": '{"url": "http://x"}',
            "id": "call_9",
        }
    ]


def test_openai_no_tool_call_leaves_tool_calls_none():
    transport, _ = _openai_capturing_transport(
        [_openai_text_wire("done: found 2 cats")]
    )
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    resp = run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert resp.tool_calls is None
    assert resp.content == "done: found 2 cats"


def test_openai_explicit_null_tool_calls_text_response_succeeds():
    wire = _openai_text_wire("done: found 2 cats")
    wire["choices"][0]["message"]["tool_calls"] = None
    transport, _ = _openai_capturing_transport([wire])
    adapter = OpenAICompatAdapter("k", "https://api.openai.com/v1")
    req = LLMRequest(
        model="openai/gpt-5",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    resp = run(adapter.complete(req, httpx.AsyncClient(transport=transport)))
    assert resp.tool_calls is None
    assert resp.content == "done: found 2 cats"


def _openai_router_with_transport(
    bodies: list[dict],
) -> tuple[ModelRouter, list[dict]]:
    transport, seen = _openai_capturing_transport(bodies)
    router = ModelRouter(keys={"openai": "k"}, max_retries=0)
    router._client = httpx.AsyncClient(transport=transport)
    return router, seen


def test_openai_malformed_tool_arguments_repair_retains_all_paid_wire_calls():
    malformed = '{"q":'
    router, seen = _openai_router_with_transport(
        [
            _openai_tool_call_wire_raw("search", malformed, call_id="bad"),
            _openai_tool_call_wire("search", {"q": "cats"}, call_id="good"),
            _openai_text_wire("done: found cats"),
        ]
    )
    model_id = "openai/gpt-5"
    model = FrisketRouterModel(router, model_id)
    agent = Agent(model, output_type=str, retries=1)
    calls: list[str] = []

    @agent.tool_plain
    def search(q: str) -> str:
        calls.append(q)
        return "cat results"

    result = agent.run_sync("find cats")

    assert result.output == "done: found cats"
    assert calls == ["cats"]
    assert len(seen) == 3
    assert model.attempts == 3
    assert len(model.wire_calls) == 3
    assert model.wire_calls[0].tool_calls == [
        {"name": "search", "args": malformed, "id": "bad"}
    ]
    assert sum(call.tokens_in for call in model.wire_calls) == 30
    assert sum(call.tokens_out for call in model.wire_calls) == 15
    one_call_cost = cost_of(model_id, 10, 5)
    assert one_call_cost is not None
    assert sum(call.cost for call in model.wire_calls) == pytest.approx(
        one_call_cost * 3
    )


# ---------------------------------------------------------------------------
# The shim dispatch loop: a real pydantic-ai Agent with 2 @agent.tool_plain
# functions, driven through FrisketRouterModel over the REAL AnthropicAdapter
# wire body (not the PoC's mock convention) -- the PoC's INV3 fixture, for
# real this time.
# ---------------------------------------------------------------------------


def _anthropic_router_with_transport(
    bodies: list[dict],
) -> tuple[ModelRouter, list[dict]]:
    transport, seen = _capturing_transport(bodies)
    router = ModelRouter(keys={"anthropic": "k"}, max_retries=0)
    router._client = httpx.AsyncClient(transport=transport)
    return router, seen


def test_shim_dispatches_two_tool_loop_through_real_anthropic_wire():
    router, seen = _anthropic_router_with_transport(
        [
            _anthropic_tool_use_wire("search", {"q": "cats"}, tool_id="c1"),
            _anthropic_tool_use_wire("fetch", {"url": "http://x"}, tool_id="c2"),
            _anthropic_text_wire("done: found 2 cats"),
        ]
    )
    model = FrisketRouterModel(router, "anthropic/claude-x")
    agent = Agent(model, output_type=str)
    calls: list[str] = []

    @agent.tool_plain
    def search(q: str) -> str:
        calls.append(f"search({q})")
        return "result-list"

    @agent.tool_plain
    def fetch(url: str) -> str:
        calls.append(f"fetch({url})")
        return "page-body"

    result = agent.run_sync("find cats")
    assert calls == ["search(cats)", "fetch(http://x)"]
    assert "done" in result.output
    # every wire body after the FIRST carries BOTH tool defs (tool_choice=auto,
    # not a forced single tool) -- the multi-tool dialect win, proven end-to-end.
    assert len(seen) == 3
    for body in seen:
        assert {t["name"] for t in body["tools"]} == {"search", "fetch"}
        assert body["tool_choice"] == {"type": "auto"}


# ---------------------------------------------------------------------------
# Cache-key parity (cache.py's the multi-tool dialect constraint, this mint's own note): `tools`
# only joins the key when actually set, so every unchanged (tools=None)
# request's hash is untouched by this stage.
# ---------------------------------------------------------------------------


def test_cache_key_unchanged_when_tools_is_none():
    req_before = LLMRequest(
        model="anthropic/claude-x", messages=[{"role": "user", "content": "go"}]
    )
    # byte-identical to a request built before `tools` existed -- there is no
    # legacy object to compare against, so this pins that the key formula
    # ignores an unset `tools` field entirely (no "tools" key ever enters the
    # canonical dict for this request).
    key1 = request_key(req_before, "1")
    req_again = LLMRequest(
        model="anthropic/claude-x", messages=[{"role": "user", "content": "go"}]
    )
    assert request_key(req_again, "1") == key1


def test_cache_key_differs_when_tools_set():
    base = LLMRequest(
        model="anthropic/claude-x", messages=[{"role": "user", "content": "go"}]
    )
    with_tools = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL],
    )
    assert request_key(base, "1") != request_key(with_tools, "1")


def test_cache_key_differs_across_distinct_tool_sets():
    one_tool = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL],
    )
    two_tools = LLMRequest(
        model="anthropic/claude-x",
        messages=[{"role": "user", "content": "go"}],
        tools=[SEARCH_TOOL, FETCH_TOOL],
    )
    assert request_key(one_tool, "1") != request_key(two_tools, "1")
