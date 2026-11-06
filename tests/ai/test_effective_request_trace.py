"""Explain captures the same prepared request used by cache and transport."""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter, ResponseCache
from frisket.ai.llm.cache import request_key
from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
from frisket.operability.trace import (
    RowTrace,
    TraceWriter,
    TracingRouter,
    read_trace_row,
)

_SECRET = "effective-request-fake-provider-key"
_PDF = base64.b64encode(b"%PDF-1.7 trace attachment sentinel").decode()
_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


class _Policy:
    calls = 0

    def prepare(self, request: LLMRequest, credential_source: str) -> LLMRequest:
        self.calls += 1
        assert credential_source == "platform_key"
        return replace(
            request,
            max_tokens=8000,
            temperature=0.25,
            params={"policy_note": _SECRET},
            messages=[
                *request.messages,
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"prepared {_SECRET}"},
                        {
                            "type": "document",
                            "media_type": "application/pdf",
                            "data": _PDF,
                        },
                    ],
                    "provider_details": {"private": "omit this detail"},
                },
            ],
        )

    def before_live(self, request: LLMRequest, credential_source: str) -> None:
        pass

    def after_live(
        self, request: LLMRequest, response: LLMResponse, credential_source: str
    ) -> None:
        pass


class _Adapter:
    api_key = _SECRET

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.seen: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, client: object) -> LLMResponse:
        self.seen.append(request)
        if self.outcome == "error":
            raise LLMError(f"provider rejected {_SECRET}", status=400)
        data = (
            {} if self.outcome == "repair" and len(self.seen) == 1 else {"answer": "ok"}
        )
        return LLMResponse(json.dumps(data), data, 10, 2, 0, request.model)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["live", "error", "cache"])
@pytest.mark.parametrize("path", ["transport", "structured", "public_structured"])
async def test_trace_records_prepared_request_on_every_completion_path(
    tmp_path, outcome: str, path: str
) -> None:
    policy = _Policy()
    adapter = _Adapter(outcome)
    cache = ResponseCache(tmp_path / "responses.db") if outcome == "cache" else None
    inner = ModelRouter(
        keys={"openai": _SECRET},
        key_sources={"openai": "platform_key"},
        use_env_keys=False,
        model_call_policy=policy,
        cache=cache,
        cache_mode="replay" if cache else "off",
        max_retries=0,
    )
    inner._adapters["openai"] = adapter  # noqa: SLF001 - external transport only
    row = RowTrace(row_id=1, trace_id="prepared")
    router = TracingRouter(inner, row)
    request = LLMRequest(
        model="openai/test",
        messages=[{"role": "user", "content": "original"}],
        max_tokens=32768,
        schema=None if path == "transport" else _SCHEMA,
    )

    async def complete() -> None:
        if path == "structured":
            await StructuredCompleter(router).complete(
                StructuredRequest(
                    model=request.model,
                    messages=request.messages,
                    schema=_SCHEMA,
                    max_tokens=request.max_tokens,
                )
            )
        elif path == "public_structured":
            await router.complete(request)
        else:
            await router.complete_transport(request)

    try:
        if cache is not None:
            # Populate through the real shaping/preparation path, then prove the
            # replay uses that same effective request without another adapter call.
            await complete()
            row.calls.clear()
        if outcome == "error":
            with pytest.raises(LLMError):
                await complete()
        else:
            await complete()
    finally:
        await inner.aclose()
        if cache is not None:
            cache.close()

    assert len(adapter.seen) == 1
    assert policy.calls == (2 if outcome == "cache" else 1)
    assert request.max_tokens == 32768
    assert len(row.calls) == 1
    call = row.calls[0]
    captured = call["request"]
    assert captured["max_tokens"] == adapter.seen[0].max_tokens == 8000
    assert captured["temperature"] == adapter.seen[0].temperature == 0.25
    assert captured["params"]["policy_note"] != _SECRET
    prepared_message = captured["messages"][-1]
    assert prepared_message["content"][0]["text"].startswith("prepared ")
    assert prepared_message["content"][1]["attachment"] == "omitted"
    assert "provider_details" not in prepared_message
    serialized = json.dumps(row.calls)
    assert _SECRET not in serialized
    assert _PDF not in serialized
    if outcome == "cache":
        assert call["cached"] is True
        assert call["events"] == [
            {"event": "cache", "hit": True, "key": request_key(adapter.seen[0])}
        ]
    if outcome == "error":
        assert call["error"]
    writer = TraceWriter.open(tmp_path, 1)
    writer.write_row(router.record())
    persisted = read_trace_row(tmp_path, 1, 1)
    assert persisted["row"]["calls"][0]["request"]["max_tokens"] == 8000


@pytest.mark.asyncio
async def test_structured_repair_captures_each_prepared_request() -> None:
    policy = _Policy()
    adapter = _Adapter("repair")
    inner = ModelRouter(
        keys={"openai": _SECRET},
        key_sources={"openai": "platform_key"},
        use_env_keys=False,
        model_call_policy=policy,
        cache_mode="off",
    )
    inner._adapters["openai"] = adapter  # noqa: SLF001 - external transport only
    row = RowTrace(row_id=1, trace_id="repair")
    try:
        result = await StructuredCompleter(TracingRouter(inner, row)).complete(
            StructuredRequest(
                model="openai/test",
                messages=[{"role": "user", "content": "original"}],
                schema=_SCHEMA,
                max_tokens=32768,
            )
        )
    finally:
        await inner.aclose()
    assert result.data == {"answer": "ok"}
    assert policy.calls == len(adapter.seen) == len(row.calls) == 2
    assert [call["request"]["max_tokens"] for call in row.calls] == [8000, 8000]
    assert len(row.calls[1]["request"]["messages"]) > len(
        row.calls[0]["request"]["messages"]
    )


@pytest.mark.asyncio
async def test_request_observer_failure_does_not_change_provider_result() -> None:
    inner = ModelRouter(keys={}, use_env_keys=False, cache_mode="off")
    adapter = _Adapter("live")
    inner._adapters["openai"] = adapter  # noqa: SLF001 - external transport only

    def refuse_capture(request: LLMRequest) -> None:
        raise RuntimeError("diagnostics unavailable")

    try:
        response = await inner.complete_transport(
            LLMRequest(model="openai/test", messages=[]), on_request=refuse_capture
        )
    finally:
        await inner.aclose()
    assert response.data == {"answer": "ok"}
    assert len(adapter.seen) == 1


@pytest.mark.asyncio
async def test_prepared_capture_redaction_failure_drops_call_without_losing_response() -> (
    None
):
    class FailingRedactionRouter(ModelRouter):
        lookups = 0

        def secret_values_for_model(self, model: str) -> tuple[str, ...]:
            self.lookups += 1
            if self.lookups > 1:
                raise RuntimeError("selected credential lookup unavailable")
            return super().secret_values_for_model(model)

    inner = FailingRedactionRouter(keys={}, use_env_keys=False, cache_mode="off")
    inner._adapters["openai"] = _Adapter("live")  # noqa: SLF001
    row = RowTrace(row_id=1, trace_id="capture-failed")
    router = TracingRouter(inner, row)
    try:
        response = await router.complete_transport(
            LLMRequest(model="openai/test", messages=[])
        )
    finally:
        await inner.aclose()
    assert response.data == {"answer": "ok"}
    assert row.calls == []
    assert router.record() is None
