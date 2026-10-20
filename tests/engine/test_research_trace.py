"""Research diagnostics remain bounded, complete across calls, and fail open."""

from __future__ import annotations

import asyncio
import gzip
import json

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.runner.row_execution import AdaptiveThrottle, execute_row
from frisket.engine.store import Project
from frisket.operability.trace import (
    MAX_ROW_BYTES,
    RowTrace,
    TraceWriter,
    TracingRouter,
    read_trace_row,
    read_trace_row_retries,
)
from frisket.ops.base import OpContext, Recipe


class _Inner:
    def secret_values_for_model(self, model):  # noqa: ANN001
        return ("selected-model-credential",)

    async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
        return LLMResponse(
            content=None,
            data=None,
            tokens_in=2,
            tokens_out=1,
            model=req.model,
            cost=0,
            raw={"opaque": "must not replace ordinary response text"},
            tool_calls=[
                {
                    "name": "search",
                    "id": "call-1",
                    "args": {"query": "selected-search-credential"},
                    "provider_details": {"opaque": "hidden"},
                }
            ],
        )


def test_disk_redaction_protects_keys_in_independently_bounded_parts():
    secret = "opaque-credential-without-pattern"
    record = {
        "kind": "row",
        "row_id": 1,
        "api_key": secret,
        "calls": [
            {"model": "test/model", "request": {"api_key": secret, "messages": []}}
        ],
    }
    serialized = TraceWriter._record_line(record)
    assert secret not in serialized
    assert json.loads(serialized)["calls"][0]["request"]["api_key"] == "[REDACTED]"


def test_complete_request_and_tool_calls_are_redacted_without_opaque_details(tmp_path):
    row = RowTrace(row_id=1, trace_id="trace")
    router = TracingRouter(_Inner(), row)
    router.add_secret_values(["selected-search-credential"])
    router.set_research(
        {
            "steps": [
                {
                    "tool": "fetch",
                    "url": "https://example.test/",
                    "text": "selected-search-credential selected-model-credential",
                    "status": "ok",
                    "truncated": False,
                }
            ],
            "truncated": False,
        }
    )
    request = LLMRequest(
        model="test/model",
        messages=[
            {
                "role": "assistant",
                "content": "selected-model-credential",
                "tool_calls": [
                    {
                        "name": "search",
                        "id": "past",
                        "args": {},
                        "provider_details": {"opaque": "hidden"},
                    }
                ],
            }
        ],
        max_tokens=123,
        temperature=0.2,
        params={"seed": 4},
        mechanism="native_tool",
        reasoning_policy="low_exclude",
        tools=[{"name": "search", "parameters": {"type": "object"}}],
    )
    asyncio.run(router.complete(request))
    writer = TraceWriter.open(tmp_path, 1)
    writer.write_row(router.record())
    result = read_trace_row(tmp_path, 1, 1)["row"]
    call = result["calls"][0]
    assert call["request"] == {
        "model": "test/model",
        "max_tokens": 123,
        "temperature": 0.2,
        "params": {"seed": 4},
        "mechanism": "native_tool",
        "reasoning_policy": "low_exclude",
        "tools": request.tools,
        "schema": None,
        "messages": [
            {
                "role": "assistant",
                "content": "[REDACTED]",
                "tool_calls": [{"name": "search", "id": "past", "args": {}}],
            }
        ],
    }
    assert call["tool_calls"] == [
        {"name": "search", "id": "call-1", "args": {"query": "[REDACTED]"}}
    ]
    assert call["raw_response"] is None
    assert "prompt" not in call
    assert result["research"]["steps"][0]["text"] == "[REDACTED] [REDACTED]"
    with gzip.open(writer.path, "rt") as stream:
        text = stream.read()
    assert "provider_details" not in text
    assert "selected-model-credential" not in text
    assert "selected-search-credential" not in text


def test_ordinary_ellipses_are_not_truncation_but_actual_string_cuts_are(tmp_path):
    class TextInner(_Inner):
        async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
            return LLMResponse(
                content="Finished…",
                data=None,
                tokens_in=1,
                tokens_out=1,
                model=req.model,
                cost=0,
            )

    row = RowTrace(row_id=1, trace_id="trace")
    router = TracingRouter(TextInner(), row)
    asyncio.run(
        router.complete(
            LLMRequest(
                model="test/model", messages=[{"role": "user", "content": "Explain…"}]
            )
        )
    )
    row.research = {
        "steps": [
            {
                "tool": "search",
                "query": "Population…",
                "status": "ok",
                "results": [
                    {
                        "title": "Population…",
                        "url": "https://example.test/",
                        "snippet": "Population grew to 5 million…",
                    }
                ],
                "truncated": False,
            },
            {
                "tool": "fetch",
                "url": "https://example.test/",
                "status": "ok",
                "text": "Complete page excerpt…",
                "truncated": False,
            },
        ],
        "truncated": False,
    }
    writer = TraceWriter.open(tmp_path, 4)
    writer.write_row(router.record())
    record = read_trace_row(tmp_path, 4, 1)["row"]
    assert record.get("truncated", False) is False
    assert record["calls"][0].get("truncated", False) is False
    assert record["research"]["truncated"] is False
    assert all(step["truncated"] is False for step in record["research"]["steps"])

    clipped = RowTrace(row_id=2, trace_id="trace")
    clipped_router = TracingRouter(TextInner(), clipped)
    asyncio.run(
        clipped_router.complete(
            LLMRequest(
                model="test/model", messages=[{"role": "user", "content": "q" * 4097}]
            )
        )
    )
    clipped.research = {
        "steps": [
            {
                "tool": "fetch",
                "url": "https://example.test/",
                "status": "ok",
                "text": "p" * 8001,
                "truncated": False,
            }
        ],
        "truncated": False,
    }
    writer.write_row(clipped_router.record())
    record = read_trace_row(tmp_path, 4, 2)["row"]
    assert record["truncated"] is True
    assert record["calls"][0]["truncated"] is True
    assert len(record["calls"][0]["request"]["messages"][0]["content"]) == 4096
    assert record["research"]["steps"][0]["truncated"] is True
    assert len(record["research"]["steps"][0]["text"]) == 8000


def test_ten_research_calls_store_messages_once_and_keep_all_bodies(tmp_path):
    row = RowTrace(row_id=1, trace_id="trace")
    router = TracingRouter(_Inner(), row)

    async def run():
        for index in range(10):
            await router.complete(
                LLMRequest(
                    model="test/model",
                    messages=[{"role": "user", "content": "question"}]
                    + [
                        {"role": "tool", "content": "x" * 4096}
                        for _ in range(index * 3)
                    ],
                )
            )

    asyncio.run(run())
    row.research = {
        "steps": [
            {
                "tool": "fetch",
                "url": f"https://example.test/{index}",
                "status": "ok",
                "text": "y" * 8000,
                "truncated": False,
            }
            for index in range(24)
        ],
        "truncated": False,
    }
    writer = TraceWriter.open(tmp_path, 5)
    writer.write_row(router.record())
    record = read_trace_row(tmp_path, 5, 1)["row"]
    assert len(record["calls"]) == 10
    assert all("request" in call and "prompt" not in call for call in record["calls"])
    assert all(call.get("truncated", False) is False for call in record["calls"])
    assert record["prompt"] == [{"role": "user", "content": "question"}]
    assert len(record["calls"][-1]["request"]["messages"]) == 28
    assert all(len(step["text"]) == 8000 for step in record["research"]["steps"])


def test_old_per_call_prompts_remain_readable(tmp_path):
    prompt = [{"role": "user", "content": "Legacy prompt…"}]
    writer = TraceWriter.open(tmp_path, 6)
    writer.write_row(
        RowTrace(
            row_id=1,
            trace_id="trace",
            calls=[{"model": "test/model", "prompt": prompt}],
        ).record()
    )
    row = read_trace_row(tmp_path, 6, 1)["row"]
    assert row["prompt"] == prompt
    assert row["calls"][0]["prompt"] == prompt


def test_byte_limit_preserves_final_data_and_retry_facts_before_old_call_bodies(
    tmp_path,
):
    event = {
        "event": "attempt",
        "outcome": "error",
        "error": "rate limited",
        "status": 429,
        "retryable": True,
    }
    row = RowTrace(
        row_id=1,
        trace_id="trace",
        calls=[
            {
                "model": f"test/{index}",
                "request": {
                    "messages": [
                        {"role": "tool", "content": "x" * 4096} for _ in range(80)
                    ]
                },
                "events": [event],
                "raw_response": "done",
            }
            for index in range(4)
        ],
    )
    writer = TraceWriter.open(tmp_path, 7)
    writer.write_row(row.record(data={"answer": "Final answer"}))
    record = read_trace_row(tmp_path, 7, 1)["row"]
    assert record["data"] == {"answer": "Final answer"}
    assert record["retries"] == [event] * 4
    assert read_trace_row_retries(tmp_path, 7, [1]) == {1: [event] * 4}
    assert "request" not in record["calls"][0]
    assert "request" in record["calls"][-1]


def test_byte_limiter_accepts_malformed_research_steps():
    serialized = TraceWriter._record_line(
        {
            "kind": "row",
            "row_id": 1,
            "calls": [{"model": "test/model"}],
            "data": ["漢" * 4096 for _ in range(100)],
            "research": {"steps": "[TRUNCATED]"},
        }
    )
    assert len(serialized.rstrip("\n").encode()) <= MAX_ROW_BYTES


def test_independent_call_and_research_budgets_survive_both_redaction_passes(tmp_path):
    shared = {"role": "user", "content": "history"}
    row = RowTrace(row_id=1, trace_id="trace")
    row.calls = [
        {
            "model": f"model/{i}",
            "request": {"messages": [shared] * 140},
            "prompt": [shared] * 140,
            "raw_response": f"last response {i}",
        }
        for i in range(10)
    ]
    row.research = {
        "steps": [
            {
                "tool": "fetch",
                "url": f"https://example.test/{i}",
                "status": "ok",
                "text": "x" * 8000,
                "truncated": False,
            }
            for i in range(24)
        ],
        "truncated": False,
    }
    writer = TraceWriter.open(tmp_path, 2)
    writer.write_row(row.record())
    result = read_trace_row(tmp_path, 2, 1)["row"]
    assert len(result["calls"]) == 10
    assert result["calls"][-1]["raw_response"] == "last response 9"
    assert result["calls"][0]["request"]["messages"][:2] == [shared, shared]
    assert result["calls"][0]["truncated"] is True
    assert len(result["research"]["steps"]) == 24
    assert all(len(step["text"]) == 8000 for step in result["research"]["steps"])


def test_serialized_utf8_row_cap_removes_old_bodies_and_retains_headers_and_tools():
    row = RowTrace(row_id=1, trace_id="trace")
    row.calls = [
        {
            "model": f"model/{i}",
            "request": {"messages": [{"content": "漢" * 4096} for _ in range(100)]},
            "raw_response": "ok",
        }
        for i in range(40)
    ]
    row.research = {
        "steps": [
            {
                "tool": "fetch",
                "url": "https://example.test/",
                "status": "ok",
                "text": "漢" * 8000,
                "truncated": False,
            }
        ],
        "truncated": False,
    }
    serialized = TraceWriter._record_line(row.record())
    assert len(serialized.rstrip("\n").encode()) <= MAX_ROW_BYTES
    result = json.loads(serialized)
    assert result["truncated"] is True
    assert [call["model"] for call in result["calls"]] == [
        f"model/{i}" for i in range(40)
    ]
    assert result["calls"][0]["truncated"] is True
    assert "request" not in result["calls"][0]
    assert "request" not in result["calls"][32]
    assert result["research"]["steps"][0]["text"] == "漢" * 8000


def test_research_only_cancellation_trace_is_persisted_and_cancellation_re_raised(
    tmp_path,
):
    class CancelledRecipe(Recipe):
        consumes_resolution = False
        cost_class = "free"

        def output_fields(self, spec):  # noqa: ANN001
            return [{"name": "answer"}]

        async def run_agent(self, values, spec, ctx, router):  # noqa: ANN001
            try:
                raise asyncio.CancelledError
            finally:
                router.set_research(
                    {
                        "steps": [
                            {
                                "tool": "fetch",
                                "url": "https://example.test",
                                "status": "cancelled",
                                "truncated": False,
                            }
                        ],
                        "truncated": False,
                    }
                )

    project = Project.create(tmp_path / "p.frisket")
    try:
        writer = TraceWriter.open(project.path, 3)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                execute_row(
                    project,
                    ModelRouter(cache=None, cache_mode="off"),
                    AdaptiveThrottle(),
                    CancelledRecipe(llm=False),
                    {},
                    {"action_kind": "map.cancelled_test"},
                    OpContext(project=project),
                    row_id=1,
                    recorder=writer,
                )
            )
        row = read_trace_row(project.path, 3, 1)["row"]
        assert row["calls"] == []
        assert row["research"]["steps"][0]["status"] == "cancelled"
        assert row["error"].startswith("cancelled:")
    finally:
        project.close()


def test_call_body_count_and_tool_body_omissions_are_explicit():
    row = RowTrace(row_id=1, trace_id="trace")

    async def call_many():
        router = TracingRouter(_Inner(), row)
        request = LLMRequest(model="test/model", messages=[])
        for _ in range(40):
            await router.complete(request)

    asyncio.run(call_many())
    assert len(row.calls) == 40
    assert sum("request" in call for call in row.calls) == 32
    assert all(call["truncated"] for call in row.calls[32:])
    row.calls = [{"model": f"model/{i}", "raw_response": "body"} for i in range(40)]
    record = row.record()
    assert sum("raw_response" in call for call in record["calls"]) == 32
    assert all(call["truncated"] for call in record["calls"][32:])
    hits = [
        {"title": "漢" * 300, "url": "漢" * 2000, "snippet": "漢" * 1000}
        for _ in range(6)
    ]
    row.calls = []
    row.research = {
        "steps": [
            {
                "tool": "search",
                "query": "q",
                "status": "ok",
                "results": hits,
                "truncated": False,
            }
            for _ in range(24)
        ],
        "truncated": False,
    }
    record = json.loads(TraceWriter._record_line(row.record()))
    assert len(record["research"]["steps"]) == 24
    assert record["research"]["truncated"] is True
    assert all(step["query"] == "q" for step in record["research"]["steps"])
    assert any(
        "results" not in step and step["truncated"]
        for step in record["research"]["steps"]
    )
