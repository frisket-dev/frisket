from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.engines import Registry
from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    GatewayTranscriptionResponse,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
)
from frisket_models.transcription.gateway import (
    TranscriptionWorkerGateway,
    WorkerEndpoint,
    WorkerRegistry,
)
from frisket_models.transcription.models_gpu_runtime import (
    CONTRACT_STUB_DESCRIPTOR,
    CONTRACT_STUB_ENGINE,
    CONTRACT_STUB_REGISTRATION,
)
from frisket_models.transcription.worker import create_worker_app


def _load_benchmark():
    path = Path(__file__).resolve().parents[1] / "scripts/transcription_benchmark.py"
    spec = importlib.util.spec_from_file_location("transcription_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark()
BenchmarkConfig = benchmark.BenchmarkConfig
BenchmarkError = benchmark.BenchmarkError
run_benchmark = benchmark.run_benchmark

ENGINE = "test-engine"
TOKEN = "benchmark-token"
WORKER_TOKEN = "worker-token"
CONTEXT = "private customer 90210"
TRANSCRIPT = "private transcript 4111-1111-1111-1111"
RUNTIME_IMAGE_ID = "oci:sha256:" + ("c" * 64)
OPTIONS = CONTRACT_STUB_DESCRIPTOR.options


def _result(options: TranscribeOptions) -> TranscribeResult:
    return TranscribeResult(
        engine=ENGINE,
        text=TRANSCRIPT,
        segments=[
            TranscribeSegment(
                start=0.0,
                end=2.0,
                text=TRANSCRIPT,
                speaker="SPEAKER_00",
            )
        ],
        duration=2.0,
        model_ids=["example/test-model"],
        revision="test-revision",
        device="cuda:0",
        dtype="bfloat16",
        timings={"worker.inference_seconds": 0.2},
        warnings=["warning containing private input"],
        accepted_options=options.supplied_options(),
    )


def _capabilities(*, loaded: bool) -> dict:
    return {
        "service": "frisket-models",
        "version": "test",
        "engines": [
            {
                "name": ENGINE,
                "route": "/v1/transcribe",
                "available": True,
                "loaded": loaded,
                "models": ["example/test-model"],
                "error": None,
                "contract_versions": [CONTRACT_VERSION],
                "revision": "test-revision",
                "runtime_image_id": RUNTIME_IMAGE_ID,
                "options": OPTIONS.model_dump(mode="json"),
            }
        ],
    }


def _success() -> dict:
    return GatewayTranscriptionResponse(
        contract_version=CONTRACT_VERSION,
        results=[_result(TranscribeOptions(context=CONTEXT))],
    ).model_dump(mode="json")


def _at_capacity() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "error": {
            "code": "at_capacity",
            "message": "private worker detail",
            "retryable": True,
            "details": {"private": "never record"},
        },
    }


def _config(tmp_path: Path, **updates) -> BenchmarkConfig:
    audio = tmp_path / "private-fixture.wav"
    audio.write_bytes(b"RIFF private audio bytes")
    values = {
        "base_url": "http://benchmark.invalid",
        "engine": ENGINE,
        "audio_path": audio,
        "options": TranscribeOptions(context=CONTEXT),
        "warmups": 1,
        "concurrencies": (2,),
        "waves": 1,
    }
    values.update(updates)
    return BenchmarkConfig(**values)


def _records(output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_gateway_waves_record_backpressure_and_redact_content(tmp_path: Path) -> None:
    lock = threading.Lock()
    calls = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.method == "GET":
            calls["get"] += 1
            assert request.url.path == "/capabilities"
            return httpx.Response(200, json=_capabilities(loaded=False))
        assert request.url.path == "/v1/transcribe"
        body = request.read()
        assert b'name="files"' in body and b'name="file"' not in body
        with lock:
            calls["post"] += 1
            number = calls["post"]
        if number == 4:
            return httpx.Response(429, json=_at_capacity())
        return httpx.Response(200, json=_success())

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = run_benchmark(
            _config(tmp_path, concurrencies=(1, 2)),
            token=TOKEN,
            output=output,
            client=client,
        )

    assert calls == {"get": 1, "post": 5}  # A 429 is never retried.
    assert records == _records(output)
    assert (
        records[0]["audio_sha256"]
        == hashlib.sha256(b"RIFF private audio bytes").hexdigest()
    )
    assert records[0]["option_receipt"]["context"] == {
        "present": True,
        "utf8_bytes": len(CONTEXT.encode()),
    }
    requests = [record for record in records if record["type"] == "request"]
    assert [record["phase"] for record in requests] == [
        "cold",
        "warmup",
        "measure",
        "measure",
        "measure",
    ]
    success = next(record for record in requests if record["success"])
    assert success["result"]["model_ids"] == ["example/test-model"]
    assert success["result"]["revision"] == "test-revision"
    assert success["result"]["speaker_count"] == 1
    assert success["result"]["timing_seconds"] == {"worker.inference_seconds": 0.2}
    assert success["rtf"] is not None
    summaries = [
        record for record in records if record["type"] == "concurrency_summary"
    ]
    assert [summary["concurrency"] for summary in summaries] == [1, 2]
    assert all(summary["waves"] == 1 for summary in summaries)
    assert [summary["request_count"] for summary in summaries] == [1, 2]
    assert [summary["success_count"] for summary in summaries] == [1, 1]
    assert [summary["at_capacity_count"] for summary in summaries] == [0, 1]
    assert records[-1] == {
        "schema": benchmark.BENCHMARK_SCHEMA,
        "type": "run_end",
        "status": "succeeded",
    }
    artifact = output.getvalue()
    for secret in (TOKEN, CONTEXT, TRANSCRIPT, "private input", "private detail"):
        assert secret not in artifact
    assert "private-fixture.wav" not in artifact


def test_main_redacts_invalid_context_from_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "SENSITIVE-CONTEXT-VALUE"
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", TOKEN)

    exit_code = benchmark.main(
        [
            "--base-url",
            "http://benchmark.invalid",
            "--engine",
            ENGINE,
            "--audio",
            str(audio),
            "--options-json",
            json.dumps({"context": {"secret": secret}}),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "benchmark failed: transcription options are invalid\n"
    assert secret not in captured.err


def test_require_cold_stops_before_upload(tmp_path: Path) -> None:
    calls = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_capabilities(loaded=True))
        calls["post"] += 1
        return httpx.Response(200, json=_success())

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BenchmarkError, match="already loaded"):
            run_benchmark(
                _config(tmp_path, require_cold=True),
                token=TOKEN,
                output=output,
                client=client,
            )
    assert calls["post"] == 0
    assert _records(output)[-1] == {
        "schema": benchmark.BENCHMARK_SCHEMA,
        "type": "run_end",
        "status": "failed",
    }


def test_runner_drives_real_gateway_worker_and_contract_stub(tmp_path: Path) -> None:
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"RIFF benchmark transport proof")
    gateway_spool, worker_spool = tmp_path / "gateway", tmp_path / "worker"
    gateway_spool.mkdir()
    worker_spool.mkdir()
    worker_app = create_worker_app(
        CONTRACT_STUB_REGISTRATION,
        token=WORKER_TOKEN,
        spool_dir=worker_spool,
        runtime_image_id=RUNTIME_IMAGE_ID,
    )
    endpoint = WorkerEndpoint(
        expected_engine=CONTRACT_STUB_ENGINE,
        base_url="http://worker.internal",
        token=WORKER_TOKEN,
        descriptor=CONTRACT_STUB_DESCRIPTOR,
        timeout_seconds=30,
    )

    def worker_client(_endpoint: WorkerEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker_app),
            base_url="http://worker.internal",
        )

    app = create_app(
        token=TOKEN,
        registry=Registry([]),
        transcription_gateway=TranscriptionWorkerGateway(
            WorkerRegistry([endpoint]), client_factory=worker_client
        ),
        transcription_spool_dir=gateway_spool,
    )
    config = BenchmarkConfig(
        base_url="http://testserver",
        engine=CONTRACT_STUB_ENGINE,
        audio_path=audio,
        options=TranscribeOptions(language="en", context=CONTEXT),
        warmups=0,
        concurrencies=(1,),
        waves=1,
    )
    output = io.StringIO()
    with TestClient(app) as client:
        records = run_benchmark(config, token=TOKEN, output=output, client=client)

    requests = [record for record in records if record["type"] == "request"]
    assert [record["phase"] for record in requests] == ["cold", "measure"]
    assert all(record["success"] for record in requests)
    assert requests[0]["result"]["speaker_count"] == 1
    assert requests[0]["result"]["word_count"] == 1
    assert records[1]["runtime_image_id"] == RUNTIME_IMAGE_ID
    assert list(gateway_spool.iterdir()) == []
    assert list(worker_spool.iterdir()) == []
