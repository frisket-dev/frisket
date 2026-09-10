"""Media calls declare transport counts; unrelated attribution facts do not."""

import asyncio
from types import SimpleNamespace

import pytest

from frisket.ai.llm.cache import ResponseCache
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.engine.executor.action_support import _routed_call_provider_use
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines import OcrEngines
from frisket.sdk.ops.transcribe_engines import (
    FasterWhisperAdapter,
    TranscriptionV1Adapter,
)
from frisket.sdk.ops.transcription.common import provider_model_call


@pytest.mark.parametrize("transport", ["local", "sidecar", "provider"])
def test_transcription_facts_count_transport_not_local_inference(tmp_path, transport):
    path = tmp_path / "clip.wav"
    path.write_bytes(b"audio")
    output = {"duration": 10.0, "cost": 0.001, "credential_source": "local"}
    if transport == "provider":
        calls = [provider_model_call("openai/whisper-1", str(path), output).as_dict()]
    else:
        adapter = (
            FasterWhisperAdapter() if transport == "local" else TranscriptionV1Adapter()
        )
        calls = adapter.model_calls(
            "faster_whisper" if transport == "local" else "moss", str(path), {}, output
        )
    [summary] = _routed_call_provider_use(calls, capability="transcribe")
    assert summary["model_call_count"] == 1
    assert summary["request_count"] == (0 if transport == "local" else 1)
    assert summary["cost_actual"] == (0.001 if transport == "provider" else 0.0)


@pytest.mark.parametrize(
    ("engine", "page_calls", "expected_requests"),
    [("rapidocr", 0, 0), ("dots.mocr", 0, 1), ("openai/gpt-4.1-mini", 2, 2)],
)
def test_ocr_batch_and_per_page_calls_are_not_conflated(
    tmp_path, engine, page_calls, expected_requests
):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"pdf")
    fact = (
        OcrEngines()
        ._model_call_for(
            engine,
            path,
            [{"text": "one"}, {"text": "two"}],
            {
                "calls": page_calls,
                "requests": page_calls,
                "input_pages": 2,
                "cost": 0.002,
            },
            ctx=SimpleNamespace(extras={}),
        )
        .as_dict()
    )
    [summary] = _routed_call_provider_use([fact], capability=fact["capability"])
    assert summary["request_count"] == expected_requests
    assert summary["model_call_count"] == 1
    assert fact["units"]["pages"] == 2


@pytest.mark.parametrize("provider_requests", [0, 999, "not-an-integer"])
def test_provider_usage_cannot_override_host_request_count(tmp_path, provider_requests):
    fact = provider_model_call(
        "openai/whisper-1",
        str(tmp_path / "clip.wav"),
        {"cost": 0.001, "usage": {"requests": provider_requests}},
    ).as_dict()
    assert fact["units"]["requests"] == 1
    [summary] = _routed_call_provider_use([fact], capability="transcribe")
    assert summary["request_count"] == 1
    assert summary["cost_actual"] == 0.001


@pytest.mark.parametrize("include_uncached", [False, True])
def test_ocr_vlm_cached_pages_do_not_count_as_requests(tmp_path, include_uncached):
    class Adapter:
        calls = 0

        async def complete(self, request, client):
            self.calls += 1
            return LLMResponse(
                content='{"text": "hello"}',
                data={"text": "hello"},
                tokens_in=10,
                tokens_out=2,
                cost=0.002,
                model=request.model,
            )

    first, second = tmp_path / "one.png", tmp_path / "two.png"
    first.write_bytes(b"first image")
    second.write_bytes(b"second image")
    adapter = Adapter()
    router = ModelRouter(
        cache=ResponseCache(tmp_path / "cache.db"), cache_mode="replay"
    )
    try:
        router._adapters["mock"] = adapter
        ctx = OpContext(http=None, extras={"router": router})
        engines = OcrEngines()
        initial_usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
        asyncio.run(engines._ocr_vlm("mock/vlm", [first], ctx, initial_usage))
        assert adapter.calls == 1

        pages = [first, second] if include_uncached else [first]
        usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "input_pages": len(pages)}
        result = asyncio.run(engines._ocr_vlm("mock/vlm", pages, ctx, usage))
        fact = engines._model_call_for(
            "mock/vlm", first, result, usage, ctx=ctx
        ).as_dict()
        [summary] = _routed_call_provider_use([fact], capability=fact["capability"])
        assert router.cache_hits == 1
        assert adapter.calls - 1 == summary["request_count"] == int(include_uncached)
        assert usage["calls"] == len(pages)
        assert fact["units"]["pages"] == len(pages)
        assert summary["cost_actual"] == (0.002 if include_uncached else 0.0)
    finally:
        asyncio.run(router.aclose())
        router.cache.close()
