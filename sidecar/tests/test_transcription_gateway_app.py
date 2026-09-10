"""Boundary-A route tests over the real authenticated sidecar app."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.transcription.contract import (
    AdapterRegistration,
    CONTRACT_VERSION,
    DiarizationMode,
    EngineProbe,
    SpeakerHint,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionEngineDescriptor,
    TranscriptionErrorEnvelope,
    TranscriptionError,
    TranscriptionOptionSupport,
)
from frisket_models.transcription.gateway import (
    TranscriptionWorkerGateway,
    WorkerEndpoint,
    WorkerGatewayError,
    WorkerRegistry,
)
from frisket_models.transcription.worker import create_worker_app
from stub_helpers import AUTH, TOKEN, stub_registry

ENGINE = "fixture-intrinsic"


def _result(options: TranscribeOptions) -> TranscribeResult:
    return TranscribeResult(
        engine=ENGINE,
        text="hello there",
        segments=[
            {
                "start": 0.0,
                "end": 0.5,
                "text": "hello",
                "speaker": "SPEAKER_00",
                "speaker_confidence": "approximate",
                "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
            }
        ],
        language=options.language,
        duration=0.5,
        model_ids=["example/fixture"],
        revision="fixture-revision",
        device="cuda:0",
        dtype="bfloat16",
        timings={"inference_seconds": 0.01},
        warnings=[],
        accepted_options=options.supplied_options(),
    )


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.paths: list[Path] = []

    async def transcribe(
        self,
        *,
        engine: str,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        assert audio_path.exists()
        assert audio_path.parent == self.spool_dir
        self.paths.append(audio_path)
        self.calls.append(
            {
                "engine": engine,
                "bytes": audio_path.read_bytes(),
                "options": options,
                "name": audio_path.name,
            }
        )
        return _result(options)

    async def public_capabilities(self) -> list[dict]:
        return [
            {
                "name": ENGINE,
                "route": "/v1/transcribe",
                "available": True,
                "loaded": False,
                "models": ["example/fixture"],
                "error": None,
                "contract_versions": [CONTRACT_VERSION],
                "revision": "fixture-revision",
                "runtime_image_id": "oci:sha256:" + ("a" * 64),
                "options": {
                    "diarization_mode": "intrinsic",
                    "speaker_hint": "none",
                    "language": True,
                    "model_size": False,
                    "vad": False,
                    "context": True,
                },
            }
        ]


def _client(
    gateway: _FakeGateway,
    spool_dir: Path,
    *,
    max_upload_bytes: int = 1024,
) -> TestClient:
    gateway.spool_dir = spool_dir
    return TestClient(
        create_app(
            token=TOKEN,
            registry=stub_registry(),
            concurrency=1,
            transcription_gateway=gateway,  # type: ignore[arg-type]
            transcription_max_upload_bytes=max_upload_bytes,
            transcription_spool_dir=spool_dir,
        )
    )


def _data(**updates: str) -> dict[str, str]:
    data = {
        "contract_version": CONTRACT_VERSION,
        "engine": ENGINE,
        "options": '{"context":"Frisket","language":"en"}',
    }
    data.update(updates)
    return data


def test_v1_route_spools_batch_and_preserves_contract_result(tmp_path: Path) -> None:
    gateway = _FakeGateway()
    client = _client(gateway, tmp_path)

    response = client.post(
        "/v1/transcribe",
        data=_data(),
        files=[
            ("files", ("../../first.wav", b"first-audio", "audio/wav")),
            ("files", ("second.wav", b"second-audio", "audio/wav")),
        ],
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert [result["engine"] for result in body["results"]] == [ENGINE, ENGINE]
    assert body["results"][0]["segments"][0]["speaker"] == "SPEAKER_00"
    assert body["results"][0]["segments"][0]["words"][0]["word"] == "hello"
    assert [call["bytes"] for call in gateway.calls] == [
        b"first-audio",
        b"second-audio",
    ]
    assert all(
        call["name"].startswith("frisket-transcription-gateway-")
        for call in gateway.calls
    )
    assert all(not path.exists() for path in gateway.paths)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("data", "expected_code"),
    [
        (
            _data(contract_version="frisket.transcription.v2"),
            "unsupported_contract_version",
        ),
        (_data(options='{"future_knob":true}'), "invalid_options"),
    ],
)
def test_v1_route_normalizes_invalid_requests_to_400(
    tmp_path: Path,
    data: dict[str, str],
    expected_code: str,
) -> None:
    gateway = _FakeGateway()
    response = _client(gateway, tmp_path).post(
        "/v1/transcribe",
        data=data,
        files=[("files", ("clip.wav", b"audio", "audio/wav"))],
        headers=AUTH,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code
    assert gateway.calls == []


def test_v1_route_rejects_over_limit_and_cleans_spool(tmp_path: Path) -> None:
    gateway = _FakeGateway()
    response = _client(gateway, tmp_path, max_upload_bytes=5).post(
        "/v1/transcribe",
        data=_data(options="{}"),
        files=[("files", ("clip.wav", b"123456", "audio/wav"))],
        headers=AUTH,
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "input_too_large"
    assert gateway.calls == []
    assert list(tmp_path.iterdir()) == []


def test_v1_route_applies_upload_limit_to_the_whole_batch(tmp_path: Path) -> None:
    gateway = _FakeGateway()
    response = _client(gateway, tmp_path, max_upload_bytes=7).post(
        "/v1/transcribe",
        data=_data(options="{}"),
        files=[
            ("files", ("one.wav", b"1234", "audio/wav")),
            ("files", ("two.wav", b"5678", "audio/wav")),
        ],
        headers=AUTH,
    )

    assert response.status_code == 413
    assert response.json()["error"]["details"] == {"max_request_upload_bytes": 7}
    assert gateway.calls == []
    assert list(tmp_path.iterdir()) == []


def test_v1_route_rejects_at_capacity_before_custom_spooling(tmp_path: Path) -> None:
    gateway = _FakeGateway()
    client = _client(gateway, tmp_path)
    limiter = client.app.state.limiter
    assert limiter.acquire()
    try:
        response = client.post(
            "/v1/transcribe",
            data=_data(options="{}"),
            files=[("files", ("clip.wav", b"audio", "audio/wav"))],
            headers=AUTH,
        )
    finally:
        limiter.release()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    assert gateway.calls == []
    assert list(tmp_path.iterdir()) == []


def test_v1_route_preserves_worker_retry_after(tmp_path: Path) -> None:
    class FullGateway(_FakeGateway):
        async def transcribe(self, **_kwargs):
            raise WorkerGatewayError(
                status_code=429,
                envelope=TranscriptionErrorEnvelope(
                    contract_version=CONTRACT_VERSION,
                    error=TranscriptionError(
                        code="at_capacity",
                        message="worker full",
                        retryable=True,
                    ),
                ),
                headers={"Retry-After": "7"},
            )

    response = _client(FullGateway(), tmp_path).post(
        "/v1/transcribe",
        data=_data(options="{}"),
        files=[("files", ("clip.wav", b"audio", "audio/wav"))],
        headers=AUTH,
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.json()["error"]["code"] == "at_capacity"
    assert list(tmp_path.iterdir()) == []


def test_v1_route_does_not_reflect_unhandled_gateway_exceptions(
    tmp_path: Path,
) -> None:
    marker = "private-gateway-exception-marker"

    class LeakingGateway(_FakeGateway):
        async def transcribe(self, **_kwargs):
            raise RuntimeError(f"internal path and credential: {marker}")

    response = _client(LeakingGateway(), tmp_path).post(
        "/v1/transcribe",
        data=_data(options="{}"),
        files=[("files", ("clip.wav", b"audio", "audio/wav"))],
        headers=AUTH,
    )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "gateway_failure",
        "message": "transcription gateway failed",
        "retryable": False,
        "details": {},
    }
    assert marker not in response.text
    assert list(tmp_path.iterdir()) == []


def test_public_capabilities_include_remote_worker_metadata(tmp_path: Path) -> None:
    response = _client(_FakeGateway(), tmp_path).get("/capabilities", headers=AUTH)

    assert response.status_code == 200
    remote = next(
        engine for engine in response.json()["engines"] if engine["name"] == ENGINE
    )
    assert remote["available"] is True
    assert remote["loaded"] is False
    assert remote["revision"] == "fixture-revision"
    assert remote["runtime_image_id"].startswith("oci:sha256:")
    assert remote["options"]["diarization_mode"] == "intrinsic"


def test_authenticated_a_to_b_to_c_stub_round_trip(tmp_path: Path) -> None:
    """One request crosses the real gateway, worker HTTP, and adapter seam."""

    gateway_spool = tmp_path / "gateway"
    worker_spool = tmp_path / "worker"
    gateway_spool.mkdir()
    worker_spool.mkdir()
    descriptor = TranscriptionEngineDescriptor(
        engine=ENGINE,
        model_ids=["example/fixture"],
        revision="fixture-revision",
        runtime_image_id=None,
        options=TranscriptionOptionSupport(
            diarization_mode=DiarizationMode.INTRINSIC,
            speaker_hint=SpeakerHint.NONE,
            language=True,
            model_size=False,
            vad=False,
            context=True,
        ),
    )
    seen_paths: list[Path] = []

    class Adapter:
        def transcribe(
            self,
            audio_path: Path,
            options: TranscribeOptions,
        ) -> TranscribeResult:
            assert audio_path.exists()
            assert audio_path.read_bytes() == b"end-to-end-audio"
            assert audio_path.suffix == ".m4a"
            seen_paths.append(audio_path)
            return _result(options)

    registration = AdapterRegistration(
        descriptor=descriptor,
        factory=Adapter,
        probe=lambda: EngineProbe(
            available=True,
            loaded=False,
            error=None,
        ),
    )
    worker_app = create_worker_app(
        registration,
        token="worker-secret",
        spool_dir=worker_spool,
        runtime_image_id="oci:sha256:" + ("c" * 64),
    )
    endpoint = WorkerEndpoint(
        expected_engine=ENGINE,
        base_url="http://fixture-worker",
        token="worker-secret",
        descriptor=descriptor,
        timeout_seconds=30,
    )

    def worker_client(_endpoint: WorkerEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker_app),
            base_url="http://fixture-worker",
        )

    gateway = TranscriptionWorkerGateway(
        WorkerRegistry([endpoint]),
        client_factory=worker_client,
    )
    app = create_app(
        token=TOKEN,
        registry=stub_registry(),
        transcription_gateway=gateway,
        transcription_spool_dir=gateway_spool,
    )
    response = TestClient(app).post(
        "/v1/transcribe",
        data=_data(),
        files=[("files", ("meeting.m4a", b"end-to-end-audio", "audio/mp4"))],
        headers=AUTH,
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["segments"][0]["speaker"] == "SPEAKER_00"
    assert result["segments"][0]["words"][0]["word"] == "hello"
    assert result["accepted_options"] == {
        "context": "Frisket",
        "language": "en",
    }
    assert len(seen_paths) == 1 and not seen_paths[0].exists()
    assert list(gateway_spool.iterdir()) == []
    assert list(worker_spool.iterdir()) == []
