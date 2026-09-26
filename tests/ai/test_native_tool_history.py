from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic_ai import Agent

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import FrisketRouterModel


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
