"""Boundary-B worker wrapper tests with a dependency-free adapter stub."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    AdapterRegistration,
    DiarizationMode,
    EngineProbe,
    SpeakerHint,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscribeWord,
    TranscriptionEngineDescriptor,
    TranscriptionInputError,
    TranscriptionOptionSupport,
)
from frisket_models.transcription.worker import (
    _spool_upload,
    create_worker_app,
)

ENGINE = "stub-joint"
MODEL_ID = "frisket/stub-joint"
REVISION = "0123456789abcdef"
RUNTIME_IMAGE_ID = "oci:sha256:" + ("a" * 64)
AUTH = {"Authorization": "Bearer worker-secret"}


class RecordingAdapter:
    def __init__(self, *, fail: bool = False, result_engine: str = ENGINE) -> None:
        self.fail = fail
        self.result_engine = result_engine
        self.paths: list[Path] = []
        self.payloads: list[bytes] = []
        self.options: list[TranscribeOptions] = []

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        assert isinstance(audio_path, Path)
        assert audio_path.exists()
        self.paths.append(audio_path)
        self.payloads.append(audio_path.read_bytes())
        self.options.append(options)
        if self.fail:
            raise RuntimeError("stub inference exploded")
        return TranscribeResult(
            engine=self.result_engine,
            text="hello there",
            segments=[
                TranscribeSegment(
                    start=0.0,
                    end=0.75,
                    text="hello there",
                    speaker="speaker_0",
                    speaker_confidence="approximate",
                    words=[
                        TranscribeWord(word="hello", start=0.0, end=0.25),
                        TranscribeWord(word="there", start=0.3, end=0.75),
                    ],
                )
            ],
            language=options.language or "en",
            duration=0.75,
            model_ids=[MODEL_ID],
            revision=REVISION,
            device="cuda:0",
            dtype="bfloat16",
            timings={"inference_seconds": 0.01},
            warnings=[],
            accepted_options=options.supplied_options(),
        )


def _descriptor() -> TranscriptionEngineDescriptor:
    return TranscriptionEngineDescriptor(
        engine=ENGINE,
        model_ids=[MODEL_ID],
        revision=REVISION,
        runtime_image_id=RUNTIME_IMAGE_ID,
        options=TranscriptionOptionSupport(
            diarization_mode=DiarizationMode.INTRINSIC,
            speaker_hint=SpeakerHint.NONE,
            language=True,
            model_size=False,
            vad=False,
            context=True,
        ),
    )


def _registration(
    adapter: RecordingAdapter,
    *,
    factory_calls: list[int] | None = None,
    factory: Any | None = None,
    probe: Any | None = None,
) -> AdapterRegistration:
    calls = factory_calls if factory_calls is not None else []

    def default_factory() -> RecordingAdapter:
        calls.append(1)
        return adapter

    return AdapterRegistration(
        descriptor=_descriptor(),
        factory=factory or default_factory,
        probe=probe or (lambda: EngineProbe(available=True, loaded=False, error=None)),
    )


def _client(
    tmp_path: Path,
    adapter: RecordingAdapter,
    *,
    token: str | None = None,
    max_upload_bytes: int = 1024,
    factory_calls: list[int] | None = None,
    factory: Any | None = None,
    probe: Any | None = None,
    runtime_image_id: str | None = None,
    clock: Any | None = None,
) -> TestClient:
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    app = create_worker_app(
        _registration(
            adapter,
            factory_calls=factory_calls,
            factory=factory,
            probe=probe,
        ),
        token=token,
        concurrency=1,
        max_upload_bytes=max_upload_bytes,
        spool_dir=tmp_path,
        runtime_image_id=runtime_image_id,
        **kwargs,
    )
    return TestClient(app)


def _post(
    client: TestClient,
    *,
    payload: bytes = b"RIFF exact stub bytes",
    filename: str = "clip.wav",
    options: dict[str, Any] | None = None,
    engine: str = ENGINE,
    contract_version: str = CONTRACT_VERSION,
    headers: dict[str, str] | None = None,
    extra_data: dict[str, str] | None = None,
):
    data = {
        "contract_version": contract_version,
        "engine": engine,
        "options": json.dumps(
            options or {},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if extra_data:
        data.update(extra_data)
    return client.post(
        "/v1/transcribe",
        data=data,
        files={"file": (filename, payload, "application/octet-stream")},
        headers=headers,
    )


def test_spools_exact_bytes_to_generated_local_path_and_cleans_up(tmp_path):
    adapter = RecordingAdapter()
    client = _client(tmp_path, adapter)
    payload = b"\x00RIFF\xff\x01 exact"

    response = _post(
        client,
        payload=payload,
        filename="../../escape.wav",
        options={"context": "Ada Lovelace", "language": "en"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"contract_version", "result"}
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["result"]["engine"] == ENGINE
    assert body["result"]["accepted_options"] == {
        "context": "Ada Lovelace",
        "language": "en",
    }
    assert adapter.payloads == [payload]
    assert adapter.options == [TranscribeOptions(context="Ada Lovelace", language="en")]
    assert len(adapter.paths) == 1
    assert adapter.paths[0].parent == tmp_path.resolve()
    assert adapter.paths[0].name.startswith("frisket-transcription-")
    assert adapter.paths[0].suffix == ".wav"
    assert "escape" not in adapter.paths[0].name
    assert not adapter.paths[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_worker_adds_namespaced_load_and_inference_wall_timings(tmp_path):
    samples = iter((10.0, 10.25, 20.0, 20.75, 30.0, 30.125, 40.0, 40.5))
    client = _client(tmp_path, RecordingAdapter(), clock=lambda: next(samples))
    cold = _post(client)
    warm = _post(client, payload=b"warm request")

    assert cold.status_code == warm.status_code == 200
    assert cold.json()["result"]["timings"] == {
        "inference_seconds": 0.01,
        "worker.adapter_load_seconds": 0.25,
        "worker.inference_seconds": 0.75,
    }
    assert warm.json()["result"]["timings"] == {
        "inference_seconds": 0.01,
        "worker.adapter_load_seconds": 0.0,
        "worker.inference_seconds": 0.5,
    }


def test_upload_limit_is_inclusive_and_overflow_is_413(tmp_path):
    adapter = RecordingAdapter()
    client = _client(tmp_path, adapter, max_upload_bytes=4)

    assert _post(client, payload=b"1234").status_code == 200
    too_large = _post(client, payload=b"12345")

    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "input_too_large"
    assert adapter.payloads == [b"1234"]
    assert list(tmp_path.iterdir()) == []


def test_inference_error_is_structured_and_temp_path_is_removed(tmp_path):
    adapter = RecordingAdapter(fail=True)
    client = _client(tmp_path, adapter)

    response = _post(client)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"
    assert len(adapter.paths) == 1
    assert not adapter.paths[0].exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "timeout_error",
    [
        TimeoutError("native model server deadline"),
        httpx.ReadTimeout(
            "native model server deadline",
            request=httpx.Request("POST", "http://native/v1/transcribe"),
        ),
    ],
)
def test_inference_timeout_is_a_retryable_504_and_cleans_up(tmp_path, timeout_error):
    class TimedOutAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            super().transcribe(audio_path, options)
            raise timeout_error

    adapter = TimedOutAdapter()
    client = _client(tmp_path, adapter)

    response = _post(client)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "inference_timeout"
    assert response.json()["error"]["retryable"] is True
    assert not adapter.paths[0].exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError(
            "connection refused",
            request=httpx.Request("POST", "http://native/v1/transcribe"),
        ),
        httpx.ReadError(
            "connection reset",
            request=httpx.Request("POST", "http://native/v1/transcribe"),
        ),
        httpx.RemoteProtocolError(
            "peer disconnected before response completed",
            request=httpx.Request("POST", "http://native/v1/transcribe"),
        ),
    ],
)
def test_native_transport_failure_is_a_retryable_503_and_cleans_up(
    tmp_path, transport_error
):
    class DisconnectedAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            super().transcribe(audio_path, options)
            raise transport_error

    adapter = DisconnectedAdapter()
    response = _post(_client(tmp_path, adapter))

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "engine_unavailable"
    assert body["retryable"] is True
    assert body["details"]["reason"].startswith(type(transport_error).__name__)
    assert not adapter.paths[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_local_http_protocol_error_remains_an_inference_failure(tmp_path):
    class MisconfiguredAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            super().transcribe(audio_path, options)
            raise httpx.LocalProtocolError("invalid adapter request framing")

    adapter = MisconfiguredAdapter()
    response = _post(_client(tmp_path, adapter))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"
    assert not adapter.paths[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_health_and_capabilities_never_construct_adapter(tmp_path):
    adapter = RecordingAdapter()
    calls: list[int] = []
    client = _client(
        tmp_path,
        adapter,
        factory_calls=calls,
        runtime_image_id="oci:sha256:" + ("b" * 64),
    )

    assert client.get("/health").json() == {"ok": True}
    before = client.get("/v1/capabilities")
    assert before.status_code == 200
    body = before.json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["descriptor"]["runtime_image_id"] == "oci:sha256:" + ("b" * 64)
    assert body["probe"] == {"available": True, "loaded": False, "error": None}
    assert calls == []

    assert _post(client).status_code == 200
    assert _post(client, payload=b"second").status_code == 200
    assert calls == [1]
    after = client.get("/v1/capabilities").json()
    # The engine probe, not construction of a lightweight adapter/client, is
    # authoritative for whether native model weights are loaded.
    assert after["probe"]["loaded"] is False


def test_concurrent_first_use_constructs_adapter_once(tmp_path):
    adapter = RecordingAdapter()
    calls: list[int] = []
    client = _client(tmp_path, adapter, factory_calls=calls)
    state = client.app.state.adapter_state
    callers = 8
    start = threading.Barrier(callers)

    def load_at_once():
        start.wait()
        return state.load()

    with ThreadPoolExecutor(max_workers=callers) as pool:
        loaded = list(pool.map(lambda _index: load_at_once(), range(callers)))

    assert all(item is adapter for item in loaded)
    assert calls == [1]


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"diarize": True}, "unsupported_option"),
        ({"num_speakers": 2}, "invalid_options"),
        ({"vad": True}, "unsupported_option"),
        ({"surprise": True}, "invalid_options"),
    ],
)
def test_unsupported_or_unknown_options_fail_before_load(
    tmp_path,
    options,
    code,
):
    adapter = RecordingAdapter()
    calls: list[int] = []
    client = _client(tmp_path, adapter, factory_calls=calls)

    response = _post(client, options=options)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"contract_version": "frisket.transcription.v999"},
            "unsupported_contract_version",
        ),
        ({"engine": "another-engine"}, "unsupported_engine"),
        ({"extra_data": {"mystery": "value"}}, "unknown_field"),
    ],
)
def test_wrong_version_engine_and_unknown_form_field_are_stable_400s(
    tmp_path,
    kwargs,
    code,
):
    adapter = RecordingAdapter()
    client = _client(tmp_path, adapter)

    response = _post(client, **kwargs)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert list(tmp_path.iterdir()) == []


def test_occupied_admission_slot_returns_429_without_spooling(tmp_path):
    adapter = RecordingAdapter()
    calls: list[int] = []
    client = _client(tmp_path, adapter, factory_calls=calls)
    admission = client.app.state.admission
    assert admission.acquire()
    try:
        response = _post(client)
    finally:
        admission.release()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    assert response.json()["error"]["code"] == "at_capacity"
    assert response.json()["error"]["retryable"] is True
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_load_failure_is_retained_and_reported_by_capabilities(tmp_path):
    adapter = RecordingAdapter()
    calls: list[int] = []

    def fail_load():
        calls.append(1)
        raise RuntimeError("weights are corrupt")

    client = _client(tmp_path, adapter, factory=fail_load)

    first = _post(client)
    second = _post(client, payload=b"retry body")

    assert first.status_code == second.status_code == 503
    assert first.json()["error"]["code"] == "engine_unavailable"
    assert second.json()["error"]["code"] == "engine_unavailable"
    assert calls == [1]
    probe = client.get("/v1/capabilities").json()["probe"]
    assert probe["available"] is False
    assert probe["loaded"] is False
    assert "weights are corrupt" in probe["error"]
    assert list(tmp_path.iterdir()) == []


def test_unavailable_probe_returns_503_without_loading_or_spooling(tmp_path):
    adapter = RecordingAdapter()
    calls: list[int] = []
    client = _client(
        tmp_path,
        adapter,
        factory_calls=calls,
        probe=lambda: EngineProbe(
            available=False,
            loaded=False,
            error="native server is down",
        ),
    )

    response = _post(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_unavailable"
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_missing_runtime_image_id_makes_worker_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID", raising=False)
    adapter = RecordingAdapter()
    base = _registration(adapter)
    registration = AdapterRegistration(
        descriptor=base.descriptor.model_copy(update={"runtime_image_id": None}),
        factory=base.factory,
        probe=base.probe,
    )
    app = create_worker_app(registration, spool_dir=tmp_path)
    client = TestClient(app)

    capabilities = client.get("/v1/capabilities").json()
    assert capabilities["descriptor"]["runtime_image_id"] is None
    assert capabilities["probe"]["available"] is False
    assert "runtime image id" in capabilities["probe"]["error"]
    assert _post(client).status_code == 503


def test_adapter_input_bound_is_a_nonretryable_413(tmp_path):
    class DurationBoundAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            raise TranscriptionInputError(
                "decoded audio exceeds this engine's duration limit",
                too_large=True,
                details={"max_duration_seconds": 3600},
            )

    response = _post(_client(tmp_path, DurationBoundAdapter()))

    assert response.status_code == 413
    assert response.json()["error"] == {
        "code": "input_too_large",
        "message": "decoded audio exceeds this engine's duration limit",
        "retryable": False,
        "details": {"max_duration_seconds": 3600},
    }
    assert list(tmp_path.iterdir()) == []


def test_wrong_adapter_engine_is_rejected(tmp_path):
    adapter = RecordingAdapter(result_engine="wrong-engine")
    client = _client(tmp_path, adapter)

    response = _post(client)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "invalid_adapter_result"
    assert list(tmp_path.iterdir()) == []


def test_unregistered_model_provenance_is_rejected(tmp_path):
    class DriftedAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            result = super().transcribe(audio_path, options)
            return result.model_copy(update={"revision": "different-revision"})

    adapter = DriftedAdapter()
    client = _client(tmp_path, adapter)

    response = _post(client)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "invalid_adapter_result"
    assert list(tmp_path.iterdir()) == []


def test_intrinsic_engine_must_label_every_segment(tmp_path):
    class SpeakerlessAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            result = super().transcribe(audio_path, options)
            segment = result.segments[0].model_copy(
                update={"speaker": None, "speaker_confidence": None}
            )
            return result.model_copy(update={"segments": [segment]})

    response = _post(_client(tmp_path, SpeakerlessAdapter()))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "invalid_adapter_result"
    assert "omitted speaker labels" in response.json()["error"]["details"]["reason"]


def test_adapter_must_account_for_every_accepted_option(tmp_path):
    class IncompleteReceiptAdapter(RecordingAdapter):
        def transcribe(self, audio_path, options):
            result = super().transcribe(audio_path, options)
            return result.model_copy(update={"accepted_options": {}})

    adapter = IncompleteReceiptAdapter()
    client = _client(tmp_path, adapter)

    response = _post(client, options={"language": "fr"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "invalid_adapter_result"
    assert list(tmp_path.iterdir()) == []


def test_token_protects_contract_routes_but_not_health(tmp_path):
    adapter = RecordingAdapter()
    client = _client(tmp_path, adapter, token="worker-secret")

    assert client.get("/health").json() == {"ok": True}
    missing = client.get("/v1/capabilities")
    wrong = client.get(
        "/v1/capabilities",
        headers={"Authorization": "Bearer wrong"},
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "missing_bearer_token"
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "invalid_bearer_token"
    assert client.get("/v1/capabilities", headers=AUTH).status_code == 200
    assert _post(client, headers=AUTH).status_code == 200


def test_spool_cleanup_survives_cancellation(tmp_path):
    class CancelledUpload:
        async def read(self, _size: int):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _spool_upload(
                CancelledUpload(),  # type: ignore[arg-type]
                max_upload_bytes=8,
                spool_dir=tmp_path,
            )
        )

    assert list(tmp_path.iterdir()) == []
