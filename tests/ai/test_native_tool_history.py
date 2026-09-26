from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent

from frisket.ai.llm.cache import request_key
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import FrisketRouterModel
from frisket.ai.llm.types import LLMRequest


def _openai_tool_call(call_id: str, sheet_id: int) -> dict[str, Any]:
    return {
        "id": "chat-tool",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "extra_content": {
                                "google": {
                                    "thought_signature": f"synthetic-{call_id}-signature"
                                }
                            },
                            "function": {
                                "name": "read_rows",
                                "arguments": json.dumps(
                                    {"sheet_id": sheet_id, "limit": 2}
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _openai_text(text: str) -> dict[str, Any]:
    return {
        "id": "chat-answer",
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }


def _anthropic_tool_call(call_id: str, sheet_id: int) -> dict[str, Any]:
    return {
        "id": "msg-tool",
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": "read_rows",
                "input": {"sheet_id": sheet_id, "limit": 2},
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _anthropic_text(text: str) -> dict[str, Any]:
    return {
        "id": "msg-answer",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }


def _openai_parallel_tool_calls() -> dict[str, Any]:
    wire = _openai_tool_call("read-call-7", 1)
    second = _openai_tool_call("read-call-8", 2)
    wire["choices"][0]["message"]["tool_calls"].extend(
        second["choices"][0]["message"]["tool_calls"]
    )
    return wire


def _anthropic_parallel_tool_calls() -> dict[str, Any]:
    wire = _anthropic_tool_call("read-call-7", 1)
    second = _anthropic_tool_call("read-call-8", 2)
    wire["content"].extend(second["content"])
    return wire


def _anthropic_emit(call_id: str, answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg-emit",
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": "emit",
                "input": answer,
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


async def _run_tool_rounds(
    provider: str, responses: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    router = ModelRouter(keys={provider: "test-key"}, max_retries=0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router._client = client  # noqa: SLF001
    try:
        model_id = (
            "gemini/gemini-3-flash-preview"
            if provider == "gemini"
            else "anthropic/claude-sonnet-4-5"
        )
        agent = Agent(FrisketRouterModel(router, model_id), output_type=str)

        @agent.tool_plain
        def read_rows(sheet_id: int, limit: int = 2) -> dict[str, Any]:
            return {
                "sheet_id": sheet_id,
                "rows": [{"headline": f"Sheet {sheet_id} permit evidence"}][:limit],
            }

        result = await agent.run("What happened to the permit?")
        return result.output, requests
    finally:
        await client.aclose()


class _CountAnswer(BaseModel):
    count: int


async def _run_schema_repair(
    provider: str, responses: list[dict[str, Any]]
) -> tuple[_CountAnswer, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    router = ModelRouter(keys={provider: "test-key"}, max_retries=0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router._client = client  # noqa: SLF001
    try:
        model_id = (
            "openai/gpt-5" if provider == "openai" else "anthropic/claude-sonnet-4-5"
        )
        agent = Agent(
            FrisketRouterModel(router, model_id),
            output_type=_CountAnswer,
            retries=1,
        )
        result = await agent.run("How many permits are there?")
        return result.output, requests
    finally:
        await client.aclose()


def test_gemini_followup_request_preserves_openai_native_tool_history() -> None:
    answer, requests = asyncio.run(
        _run_tool_rounds(
            "gemini",
            [
                _openai_tool_call("read-call-7", 1),
                _openai_tool_call("read-call-8", 2),
                _openai_text("The permit decision was deferred."),
            ],
        )
    )

    assert answer == "The permit decision was deferred."
    assert len(requests) == 3
    native_history = [
        message
        for message in requests[2]["messages"]
        if message.get("tool_calls") or message["role"] == "tool"
    ]
    assert [message["role"] for message in native_history] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    for pair, (call_id, sheet_id) in zip(
        (native_history[:2], native_history[2:]),
        (("read-call-7", 1), ("read-call-8", 2)),
        strict=True,
    ):
        assistant, result = pair
        [tool_call] = assistant["tool_calls"]
        assert tool_call["id"] == call_id
        assert tool_call["extra_content"] == {
            "google": {"thought_signature": f"synthetic-{call_id}-signature"}
        }
        assert tool_call["function"]["name"] == "read_rows"
        assert json.loads(tool_call["function"]["arguments"]) == {
            "sheet_id": sheet_id,
            "limit": 2,
        }
        assert result["tool_call_id"] == call_id
        assert f"Sheet {sheet_id} permit evidence" in result["content"]


def test_anthropic_followup_request_preserves_native_tool_history() -> None:
    answer, requests = asyncio.run(
        _run_tool_rounds(
            "anthropic",
            [
                _anthropic_tool_call("read-call-7", 1),
                _anthropic_tool_call("read-call-8", 2),
                _anthropic_text("The permit was deferred."),
            ],
        )
    )

    assert answer == "The permit was deferred."
    assert len(requests) == 3
    native_history = [
        message
        for message in requests[2]["messages"]
        if isinstance(message["content"], list)
        and any(
            part.get("type") in {"tool_use", "tool_result"}
            for part in message["content"]
        )
    ]
    assert [message["role"] for message in native_history] == [
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    for pair, (call_id, sheet_id) in zip(
        (native_history[:2], native_history[2:]),
        (("read-call-7", 1), ("read-call-8", 2)),
        strict=True,
    ):
        assistant, result = pair
        [tool_use] = assistant["content"]
        [tool_result] = result["content"]
        assert tool_use == {
            "type": "tool_use",
            "id": call_id,
            "name": "read_rows",
            "input": {"sheet_id": sheet_id, "limit": 2},
        }
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == call_id
        assert f"Sheet {sheet_id} permit evidence" in tool_result["content"]


def test_parallel_calls_and_results_stay_in_the_same_provider_turn() -> None:
    _, openai_requests = asyncio.run(
        _run_tool_rounds(
            "gemini",
            [
                _openai_parallel_tool_calls(),
                _openai_text("Both sheets contain permit evidence."),
            ],
        )
    )
    openai_history = openai_requests[1]["messages"]
    assistant = next(message for message in openai_history if message.get("tool_calls"))
    assistant_index = openai_history.index(assistant)
    results = openai_history[assistant_index + 1 : assistant_index + 3]
    assert [call["id"] for call in assistant["tool_calls"]] == [
        "read-call-7",
        "read-call-8",
    ]
    assert [result["role"] for result in results] == ["tool", "tool"]
    assert [result["tool_call_id"] for result in results] == [
        "read-call-7",
        "read-call-8",
    ]

    _, anthropic_requests = asyncio.run(
        _run_tool_rounds(
            "anthropic",
            [
                _anthropic_parallel_tool_calls(),
                _anthropic_text("Both sheets contain permit evidence."),
            ],
        )
    )
    anthropic_history = anthropic_requests[1]["messages"]
    assistant = next(
        message
        for message in anthropic_history
        if message["role"] == "assistant"
        and isinstance(message["content"], list)
        and any(part.get("type") == "tool_use" for part in message["content"])
    )
    assistant_index = anthropic_history.index(assistant)
    result = anthropic_history[assistant_index + 1]
    assert [part["id"] for part in assistant["content"]] == [
        "read-call-7",
        "read-call-8",
    ]
    assert result["role"] == "user"
    assert [part["tool_use_id"] for part in result["content"]] == [
        "read-call-7",
        "read-call-8",
    ]


def test_tool_validation_retry_is_an_error_result_for_the_original_call() -> None:
    invalid = _anthropic_tool_call("invalid-read-call", 1)
    invalid["content"][0]["input"] = {"sheet_id": "not-an-integer", "limit": 2}
    answer, requests = asyncio.run(
        _run_tool_rounds(
            "anthropic",
            [invalid, _anthropic_text("I need a valid sheet identifier.")],
        )
    )

    assert answer == "I need a valid sheet identifier."
    messages = requests[1]["messages"]
    assistant = next(
        message
        for message in messages
        if message["role"] == "assistant"
        and isinstance(message["content"], list)
        and any(part.get("type") == "tool_use" for part in message["content"])
    )
    result = messages[messages.index(assistant) + 1]
    [tool_use] = assistant["content"]
    [tool_result] = result["content"]
    assert tool_use["id"] == "invalid-read-call"
    assert tool_result["tool_use_id"] == "invalid-read-call"
    assert tool_result["is_error"] is True


def test_schema_repair_uses_only_the_provider_native_history_shape() -> None:
    anthropic_answer, anthropic_requests = asyncio.run(
        _run_schema_repair(
            "anthropic",
            [
                _anthropic_emit("emit-call-1", {"count": "not-an-integer"}),
                _anthropic_emit("emit-call-2", {"count": 2}),
            ],
        )
    )
    assert anthropic_answer == _CountAnswer(count=2)
    anthropic_messages = anthropic_requests[1]["messages"]
    emit = next(
        message
        for message in anthropic_messages
        if message["role"] == "assistant"
        and isinstance(message["content"], list)
        and any(part.get("type") == "tool_use" for part in message["content"])
    )
    error = anthropic_messages[anthropic_messages.index(emit) + 1]
    [emit_call] = emit["content"]
    [emit_result] = error["content"]
    assert emit_call["name"] == "emit"
    assert emit_call["id"] == "emit-call-1"
    assert emit_result["tool_use_id"] == "emit-call-1"
    assert emit_result["is_error"] is True

    openai_answer, openai_requests = asyncio.run(
        _run_schema_repair(
            "openai",
            [
                _openai_text('{"count": "not-an-integer"}'),
                _openai_text('{"count": 2}'),
            ],
        )
    )
    assert openai_answer == _CountAnswer(count=2)
    repair = openai_requests[1]
    assert "response_format" in repair
    assert not any(
        message.get("tool_calls") or message["role"] == "tool"
        for message in repair["messages"]
    )
    assert repair["messages"][-2] == {
        "role": "assistant",
        "content": '{"count": "not-an-integer"}',
    }
    assert repair["messages"][-1]["role"] == "user"


def _legacy_v3_request_key(request: LLMRequest, recipe_version: str) -> str:
    canonical: dict[str, Any] = {
        "v": 3,
        "model": request.model,
        "recipe_version": recipe_version,
        "messages": request.messages,
        "schema": request.schema,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "params": request.params,
        "mechanism": request.mechanism,
    }
    if request.tools is not None:
        canonical["tools"] = request.tools
    if request.reasoning_policy is not None:
        canonical["reasoning_policy"] = request.reasoning_policy
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_native_history_requests_do_not_replay_legacy_response_shapes() -> None:
    requests = [
        LLMRequest(
            model="gemini/gemini-3-flash-preview",
            messages=[{"role": "user", "content": "Read the permit rows."}],
            tools=[
                {
                    "name": "read_rows",
                    "description": "Read selected rows.",
                    "parameters": {
                        "type": "object",
                        "properties": {"sheet_id": {"type": "integer"}},
                        "required": ["sheet_id"],
                    },
                }
            ],
        ),
        LLMRequest(
            model="anthropic/claude-sonnet-4-5",
            messages=[{"role": "user", "content": "Count the permits."}],
            schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            mechanism="native_tool",
        ),
    ]

    legacy_collisions = [
        request.model
        for request in requests
        if request_key(request, "project_qa.v1")
        == _legacy_v3_request_key(request, "project_qa.v1")
    ]
    assert legacy_collisions == []
