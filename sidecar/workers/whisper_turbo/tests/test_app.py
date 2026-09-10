from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from frisket_models.transcription import CONTRACT_VERSION

from frisket_whisper_turbo.adapter import (
    ENGINE,
    MODEL_ID,
    MODEL_REVISION,
    WhisperTurboConfig,
    build_registration,
)
from frisket_whisper_turbo.app import create_app

RUNTIME_IMAGE_ID = "oci:sha256:" + ("a" * 64)
TOKEN = "worker-test-token"


class FakeModel:
    def transcribe(self, _path: str, **_kwargs: Any):
        segments = [
            SimpleNamespace(
                start=0.0,
                end=0.5,
                text=" control",
                words=[SimpleNamespace(word=" control", start=0.0, end=0.5)],
            )
        ]
        return iter(segments), SimpleNamespace(language="en", duration=0.5)


def _environ(tmp_path: Path) -> dict[str, str]:
    return {
        "FRISKET_TRANSCRIPTION_WORKER_TOKEN": TOKEN,
        "FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY": "1",
        "FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID": RUNTIME_IMAGE_ID,
        "FRISKET_TRANSCRIPTION_WORKER_SPOOL_DIR": str(tmp_path),
    }


def _post(client: TestClient):
    return client.post(
        "/v1/transcribe",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={
            "contract_version": CONTRACT_VERSION,
            "engine": ENGINE,
            "options": json.dumps({"language": "en", "vad": False}),
        },
        files={"file": ("fixture.wav", b"mock bytes", "audio/wav")},
    )


def test_capabilities_is_truthful_without_constructing_model(tmp_path: Path) -> None:
    factory_calls: list[dict[str, Any]] = []

    def model_factory(**kwargs: Any) -> FakeModel:
        factory_calls.append(kwargs)
        return FakeModel()

    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=model_factory,
        dependency_probe=lambda _config: (True, None),
        environ={},
    )
    client = TestClient(
        create_app(registration=registration, environ=_environ(tmp_path))
    )

    response = client.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["descriptor"]["engine"] == ENGINE
    assert body["descriptor"]["model_ids"] == [MODEL_ID]
    assert body["descriptor"]["revision"] == MODEL_REVISION
    assert body["descriptor"]["runtime_image_id"] == RUNTIME_IMAGE_ID
    assert body["probe"] == {"available": True, "loaded": False, "error": None}
    assert factory_calls == []


def test_real_worker_wrapper_preserves_turbo_result_and_receipt(
    tmp_path: Path,
) -> None:
    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=lambda **_kwargs: FakeModel(),
        dependency_probe=lambda _config: (True, None),
        environ={},
    )
    client = TestClient(
        create_app(registration=registration, environ=_environ(tmp_path))
    )

    response = _post(client)

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["engine"] == ENGINE
    assert result["text"] == "control"
    assert result["accepted_options"] == {"language": "en", "vad": False}
    assert result["segments"] == [
        {
            "start": 0.0,
            "end": 0.5,
            "text": " control",
            "speaker": None,
            "speaker_confidence": None,
            "words": [{"word": " control", "start": 0.0, "end": 0.5}],
        }
    ]
    assert set(result["timings"]) == {
        "adapter.inference_seconds",
        "worker.adapter_load_seconds",
        "worker.inference_seconds",
    }
    assert list(tmp_path.iterdir()) == []


def test_runtime_requires_token_and_exactly_one_concurrent_request(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be a non-empty printable"):
        create_app(environ={})

    invalid = _environ(tmp_path)
    invalid["FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY"] = "2"
    with pytest.raises(ValueError, match="must be 1"):
        create_app(environ=invalid)


def test_worker_rejects_missing_bearer_token(tmp_path: Path) -> None:
    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=lambda **_kwargs: FakeModel(),
        dependency_probe=lambda _config: (True, None),
        environ={},
    )
    client = TestClient(
        create_app(registration=registration, environ=_environ(tmp_path))
    )

    response = client.get("/v1/capabilities")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_bearer_token"


def test_worker_never_returns_raw_model_factory_failure(tmp_path: Path) -> None:
    secret = "private-loader-token"

    def fail_factory(**_kwargs: Any) -> FakeModel:
        raise RuntimeError(secret)

    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=fail_factory,
        dependency_probe=lambda _config: (True, None),
        environ={},
    )
    client = TestClient(
        create_app(registration=registration, environ=_environ(tmp_path))
    )

    response = _post(client)

    assert response.status_code == 503
    assert secret not in response.text
    assert "Whisper Turbo model failed to load" in response.text

    capability = client.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert capability.status_code == 200
    assert secret not in capability.text
