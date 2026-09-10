from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from frisket_models.transcription import CONTRACT_VERSION

from frisket_parakeet_tdt.adapter import (
    DESCRIPTOR,
    ENGINE,
    ParakeetConfig,
    ParakeetRuntime,
    build_registration,
)
from frisket_parakeet_tdt.app import create_app

TOKEN = "worker-test-token"
IMAGE_ID = "oci:sha256:" + ("a" * 64)


class FakeRecognizer:
    def recognize(self, _waveform: Any, *, sample_rate: int):
        assert sample_rate == 16_000
        return [SimpleNamespace(start=0.0, end=0.5, text=" control")]


class FakeModel:
    def with_vad(self, _vad: Any, *, batch_size: int) -> FakeRecognizer:
        assert batch_size == 1
        return FakeRecognizer()


def _registration():
    return build_registration(
        ParakeetConfig(Path("/fixed/models")),
        runtime_factory=lambda _config: ParakeetRuntime(FakeModel(), object()),
        dependency_probe=lambda _config: (True, None),
        clock=iter([1.0, 1.1]).__next__,
    )


def _environ(tmp_path: Path) -> dict[str, str]:
    return {
        "FRISKET_TRANSCRIPTION_WORKER_TOKEN": TOKEN,
        "FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID": IMAGE_ID,
        "FRISKET_TRANSCRIPTION_WORKER_SPOOL_DIR": str(tmp_path),
    }


def test_capabilities_do_not_load_model(tmp_path: Path) -> None:
    calls: list[str] = []
    registration = build_registration(
        ParakeetConfig(Path("/fixed/models")),
        runtime_factory=lambda _config: calls.append("loaded"),  # type: ignore[arg-type,return-value]
        dependency_probe=lambda _config: (True, None),
    )
    client = TestClient(
        create_app(registration=registration, environ=_environ(tmp_path))
    )

    response = client.get(
        "/v1/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["descriptor"]["engine"] == ENGINE
    assert body["descriptor"]["model_ids"] == DESCRIPTOR.model_ids
    assert body["descriptor"]["runtime_image_id"] == IMAGE_ID
    assert body["probe"] == {"available": True, "loaded": False, "error": None}
    assert calls == []


def test_worker_transcribes_and_cleans_spool(tmp_path: Path) -> None:
    client = TestClient(
        create_app(registration=_registration(), environ=_environ(tmp_path))
    )

    response = client.post(
        "/v1/transcribe",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={
            "contract_version": CONTRACT_VERSION,
            "engine": ENGINE,
            "options": json.dumps({"vad": True}),
        },
        files={"file": ("input.wav", b"RIFFmock", "audio/wav")},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["text"] == "control"
    assert result["accepted_options"] == {"vad": True}
    assert result["model_ids"] == DESCRIPTOR.model_ids
    assert set(result["timings"]) == {
        "adapter.asr_seconds",
        "worker.adapter_load_seconds",
        "worker.inference_seconds",
    }
    assert list(tmp_path.iterdir()) == []


def test_runtime_requires_token_and_single_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty printable"):
        create_app(environ={})

    invalid = _environ(tmp_path)
    invalid["FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY"] = "2"
    with pytest.raises(ValueError, match="must be 1"):
        create_app(environ=invalid)
