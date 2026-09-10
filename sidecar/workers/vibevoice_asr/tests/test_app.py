from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from frisket_models.transcription import (
    CONTRACT_VERSION,
    AdapterRegistration,
    EngineProbe,
)

from frisket_vibevoice_asr.adapter import DESCRIPTOR, ENGINE
from frisket_vibevoice_asr.app import create_app

TOKEN = "worker-test-token"
RUNTIME_IMAGE_ID = "oci:sha256:" + ("b" * 64)


class FakeAdapter:
    def transcribe(self, _path: Path, _options):
        from frisket_models.transcription import TranscribeResult, TranscribeSegment

        from frisket_vibevoice_asr.adapter import MODEL_ID, MODEL_REVISION

        return TranscribeResult(
            engine=ENGINE,
            text="hello",
            segments=[TranscribeSegment(start=0, end=0.5, text="hello", speaker="S1")],
            language=None,
            duration=0.5,
            model_ids=[MODEL_ID],
            revision=MODEL_REVISION,
            device="cuda:0",
            dtype="bfloat16",
            timings={"adapter.inference_seconds": 0.1},
            warnings=[],
            accepted_options={},
        )


def _environ(tmp_path: Path) -> dict[str, str]:
    return {
        "FRISKET_TRANSCRIPTION_WORKER_TOKEN": TOKEN,
        "FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY": "1",
        "FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID": RUNTIME_IMAGE_ID,
        "FRISKET_TRANSCRIPTION_WORKER_SPOOL_DIR": str(tmp_path),
    }


def _registration():
    return AdapterRegistration(
        descriptor=DESCRIPTOR,
        factory=FakeAdapter,
        probe=lambda: EngineProbe(available=True, loaded=False, error=None),
    )


def test_capabilities_is_lazy_and_truthful(tmp_path: Path) -> None:
    client = TestClient(
        create_app(registration=_registration(), environ=_environ(tmp_path))
    )

    response = client.get(
        "/v1/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["descriptor"]["engine"] == ENGINE
    assert body["descriptor"]["runtime_image_id"] == RUNTIME_IMAGE_ID
    assert body["probe"] == {"available": True, "loaded": False, "error": None}


def test_wrapper_authenticates_spools_and_cleans(tmp_path: Path) -> None:
    client = TestClient(
        create_app(registration=_registration(), environ=_environ(tmp_path))
    )
    response = client.post(
        "/v1/transcribe",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={
            "contract_version": CONTRACT_VERSION,
            "engine": ENGINE,
            "options": json.dumps({}),
        },
        files={"file": ("fixture.wav", b"mock bytes", "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json()["result"]["segments"][0]["speaker"] == "S1"
    assert list(tmp_path.iterdir()) == []


def test_worker_requires_token_and_single_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a non-empty printable"):
        create_app(environ={})
    invalid = _environ(tmp_path)
    invalid["FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY"] = "2"
    with pytest.raises(ValueError, match="must be 1"):
        create_app(environ=invalid)
