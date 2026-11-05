from __future__ import annotations

import asyncio
import json

import httpx
from pydantic_ai import Agent

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import FrisketRouterModel


def test_openai_agent_replays_reasoning_and_parallel_tool_calls() -> None:
    requests = []
    outputs = [
        {
            "id": "resp-1",
            "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "read",
                    "arguments": '{"number":1}',
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "fc-2",
                    "call_id": "call-2",
                    "name": "read",
                    "arguments": '{"number":2}',
                    "status": "completed",
                },
            ],
        },
        {
            "id": "resp-2",
            "status": "completed",
            "usage": {"input_tokens": 14, "output_tokens": 3},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done"}],
                }
            ],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=outputs[len(requests) - 1])

    async def run():
        router = ModelRouter(keys={"openai": "test-key"}, max_retries=0)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router._client = client
        model = FrisketRouterModel(router, "openai/gpt-6-luna", max_tokens=321)
        agent = Agent(model, output_type=str)

        @agent.tool_plain
        def read(number: int) -> str:
            return f"value {number}"

        try:
            result = await agent.run("Read both")
            return result.output, model.wire_calls
        finally:
            await client.aclose()

    answer, calls = asyncio.run(run())
    assert answer == "Done"
    assert [(c.tokens_in, c.tokens_out) for c in calls] == [(10, 5), (14, 3)]
    assert [path for path, _ in requests] == ["/v1/responses", "/v1/responses"]
    first, second = [body for _, body in requests]
    assert first["max_output_tokens"] == 321
    assert first["store"] is False
    assert first["tools"][0]["name"] == "read"
    assert first["tools"][0]["strict"] is False
    assert "reasoning" not in first
    history = second["input"]
    assert [
        item["type"]
        for item in history
        if item.get("type") in {"reasoning", "function_call", "function_call_output"}
    ] == [
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert (
        next(item for item in history if item.get("type") == "reasoning")[
            "encrypted_content"
        ]
        == "opaque"
    )
    results = {
        item["call_id"]: item["output"]
        for item in history
        if item.get("type") == "function_call_output"
    }
    assert results == {"call-1": "value 1", "call-2": "value 2"}


def test_openai_tool_continuation_without_tools_uses_responses() -> None:
    from frisket.ai.llm.adapters import OpenAICompatAdapter
    from frisket.ai.llm.types import LLMRequest

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "usage": {},
            },
        )

    async def run():
        adapter = OpenAICompatAdapter("test-key", "https://api.openai.com/v1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.complete(
                LLMRequest(
                    model="openai/gpt-6-luna",
                    messages=[
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"id": "call-1", "name": "read", "args": "{}"}
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call-1",
                            "tool_name": "read",
                            "content": "result",
                        },
                    ],
                ),
                client,
            )

    asyncio.run(run())
    path, body = requests[0]
    assert path == "/v1/responses"
    assert "tools" not in body
    assert [item["type"] for item in body["input"]] == [
        "function_call",
        "function_call_output",
    ]


def test_openai_response_failure_and_limit_do_not_return_an_answer() -> None:
    import pytest
    from frisket.ai.llm.structured import OutputLimitReached
    from frisket.ai.llm.types import LLMError

    async def run(status, details):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": status,
                    "incomplete_details": details,
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "partial"}],
                        }
                    ],
                    "usage": {"input_tokens": 7, "output_tokens": 9},
                },
            )

        router = ModelRouter(keys={"openai": "test-key"}, max_retries=0)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router._client = client
        model = FrisketRouterModel(router, "openai/gpt-6-luna")
        agent = Agent(model, output_type=str)

        @agent.tool_plain
        def read() -> str:
            return "result"

        try:
            with pytest.raises(
                OutputLimitReached if status == "incomplete" else LLMError
            ):
                await agent.run("read")
            return model.wire_calls
        finally:
            await client.aclose()

    calls = asyncio.run(run("incomplete", {"reason": "max_output_tokens"}))
    assert [(call.tokens_in, call.tokens_out) for call in calls] == [(7, 9)]
    asyncio.run(run("failed", None))


def test_openai_without_tools_remains_on_chat_completions() -> None:
    from frisket.ai.llm.adapters import OpenAICompatAdapter
    from frisket.ai.llm.types import LLMRequest

    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    async def run():
        adapter = OpenAICompatAdapter("test-key", "https://api.openai.com/v1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.complete(
                LLMRequest(
                    model="openai/gpt-6-luna",
                    messages=[{"role": "user", "content": "hello"}],
                ),
                client,
            )

    assert asyncio.run(run()).content == "done"
    assert paths == ["/v1/chat/completions"]


def test_responses_tool_request_keeps_image_and_pdf_content() -> None:
    import base64

    from frisket.ai.llm.adapters import OpenAICompatAdapter
    from frisket.ai.llm.types import LLMRequest

    bodies = []
    pdf = base64.b64encode(b"%PDF-1.4\nsynthetic").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "usage": {},
            },
        )

    async def run():
        adapter = OpenAICompatAdapter("test-key", "https://api.openai.com/v1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.complete(
                LLMRequest(
                    model="openai/gpt-6-luna",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "inspect"},
                                {
                                    "type": "image",
                                    "media_type": "image/png",
                                    "data": "YWJj",
                                    "detail": "high",
                                },
                                {
                                    "type": "document",
                                    "media_type": "application/pdf",
                                    "data": pdf,
                                    "filename": "source.pdf",
                                },
                            ],
                        }
                    ],
                    tools=[{"name": "read"}],
                ),
                client,
            )

    asyncio.run(run())
    assert bodies[0]["input"][0]["content"] == [
        {"type": "input_text", "text": "inspect"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,YWJj",
            "detail": "high",
        },
        {
            "type": "input_file",
            "file_data": "data:application/pdf;base64," + pdf,
            "filename": "source.pdf",
        },
    ]


def test_failed_openai_tool_responses_record_provider_usage_once() -> None:
    import pytest

    from frisket.ai.llm.types import LLMError

    async def run(status, output):
        requests = []
        accounted = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "resp-failed",
                    "status": status,
                    "incomplete_details": {"reason": "content_filter"}
                    if status == "incomplete"
                    else None,
                    "output": output,
                    "usage": {"input_tokens": 700, "output_tokens": 90},
                },
            )

        router = ModelRouter(
            keys={"openai": "test-key"},
            key_sources={"openai": "project_key"},
            max_retries=2,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        router._client = client

        async def on_response(response):
            accounted.append(response)

        model = FrisketRouterModel(router, "openai/gpt-6-luna", on_response=on_response)
        agent = Agent(model, output_type=str)

        @agent.tool_plain
        def read() -> str:
            return "result"

        try:
            with pytest.raises(LLMError) as caught:
                await agent.run("read")
            assert len(requests) == 1
            assert (
                len(accounted)
                == len(model.wire_calls)
                == len(caught.value.wire_calls)
                == 1
            )
            call = accounted[0]
            assert (call.tokens_in, call.tokens_out) == (700, 90)
            assert call.provider == "openai"
            assert call.credential_source == "project_key"
            assert call.cost is not None and call.cost > 0
            assert call.duration_ms is not None
        finally:
            await client.aclose()

    for status, output in [
        ("failed", []),
        ("incomplete", []),
        ("completed", []),
        ("completed", None),
    ]:
        asyncio.run(run(status, output))
