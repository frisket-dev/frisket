"""The models-only factories stay light and expose fixed worker identities."""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

from frisket_models.transcription.contract import CONTRACT_VERSION
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_DESCRIPTOR,
    WHISPER_TURBO_ENGINE,
)
from frisket_models.transcription.models_gpu_runtime import (
    CONTRACT_STUB_ENGINE,
    create_contract_stub_worker_app,
    create_models_gateway_app,
)

TOKEN = "models-boundary-a-token"
WORKER_TOKEN = "worker-boundary-b-token"
RUNTIME_IMAGE_ID = "oci:sha256:" + ("d" * 64)


def _clear_worker_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("FRISKET_TRANSCRIPTION_"):
            monkeypatch.delenv(key, raising=False)


def test_models_gateway_has_no_legacy_or_default_remote_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_environment(monkeypatch)
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", TOKEN)

    app = create_models_gateway_app()
    assert app.state.registry.describe() == []
    assert app.state.transcription_gateway.registry.endpoints() == ()

    response = TestClient(app).get(
        "/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json()["engines"] == []


def test_models_gateway_opt_in_stub_is_code_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_environment(monkeypatch)
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", TOKEN)
    monkeypatch.setenv(
        "FRISKET_TRANSCRIPTION_CONTRACT_STUB_WORKER_URL",
        "http://gpu-worker:9000",
    )
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_CONTRACT_STUB_WORKER_TOKEN", WORKER_TOKEN)

    app = create_models_gateway_app()
    (endpoint,) = app.state.transcription_gateway.registry.endpoints()
    assert endpoint.expected_engine == CONTRACT_STUB_ENGINE
    assert endpoint.base_url == "http://gpu-worker:9000"
    assert endpoint.descriptor.engine == CONTRACT_STUB_ENGINE


def test_models_gateway_opt_in_whisper_turbo_is_code_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_environment(monkeypatch)
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", TOKEN)
    monkeypatch.setenv(
        "FRISKET_TRANSCRIPTION_WHISPER_TURBO_WORKER_URL",
        "http://gpu-worker:9000",
    )
    monkeypatch.setenv(
        "FRISKET_TRANSCRIPTION_WHISPER_TURBO_WORKER_TOKEN",
        WORKER_TOKEN,
    )

    app = create_models_gateway_app()
    (endpoint,) = app.state.transcription_gateway.registry.endpoints()
    assert endpoint.expected_engine == WHISPER_TURBO_ENGINE
    assert endpoint.base_url == "http://gpu-worker:9000"
    assert endpoint.descriptor == WHISPER_TURBO_DESCRIPTOR


def test_models_gateway_rejects_reused_public_and_worker_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_environment(monkeypatch)
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", TOKEN)
    monkeypatch.setenv(
        "FRISKET_TRANSCRIPTION_CONTRACT_STUB_WORKER_URL",
        "http://gpu-worker:9000",
    )
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_CONTRACT_STUB_WORKER_TOKEN", TOKEN)

    with pytest.raises(RuntimeError, match="must be distinct"):
        create_models_gateway_app()


def test_contract_stub_preserves_intrinsic_speaker_and_option_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_environment(monkeypatch)
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID", RUNTIME_IMAGE_ID)
    client = TestClient(create_contract_stub_worker_app())
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}

    capability = client.get("/v1/capabilities", headers=headers)
    assert capability.status_code == 200
    assert capability.json()["probe"] == {
        "available": True,
        "loaded": False,
        "error": None,
    }
    assert capability.json()["descriptor"]["runtime_image_id"] == RUNTIME_IMAGE_ID

    response = client.post(
        "/v1/transcribe",
        headers=headers,
        data={
            "contract_version": CONTRACT_VERSION,
            "engine": CONTRACT_STUB_ENGINE,
            "options": '{"context":"Frisket","language":"en"}',
        },
        files={"file": ("clip.wav", io.BytesIO(b"RIFF-stub"), "audio/wav")},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["accepted_options"] == {"context": "Frisket", "language": "en"}
    assert result["segments"] == [
        {
            "start": 0.0,
            "end": 0.25,
            "text": "contract",
            "speaker": "SPEAKER_00",
            "speaker_confidence": "diagnostic",
            "words": [{"word": "contract", "start": 0.0, "end": 0.25}],
        }
    ]
    assert result["model_ids"] == ["frisket/contract-stub"]
    assert result["revision"] == "transcription-contract-v1"
    assert "no model inference" in result["warnings"][0]


def test_contract_stub_worker_is_fail_closed_and_concurrency_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_environment(monkeypatch)
    with pytest.raises(RuntimeError, match="WORKER_TOKEN is required"):
        create_contract_stub_worker_app()

    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="exactly 1"):
        create_contract_stub_worker_app()

    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY", "1")
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_MAX_UPLOAD_BYTES", "0")
    with pytest.raises(ValueError, match="upload limit must be positive"):
        create_contract_stub_worker_app()
