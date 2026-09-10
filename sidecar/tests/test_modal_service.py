"""Acceptance contract for the hosted Modal models service."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from frisket_models.transcription.moss import MOSS_DESCRIPTOR

TOKEN = "hosted-models-token"


def _clear_worker_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("FRISKET_TRANSCRIPTION_"):
            monkeypatch.delenv(key, raising=False)


def test_hosted_app_declares_only_the_approved_resident_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket_models.modal_service import create_hosted_app

    _clear_worker_environment(monkeypatch)
    app = create_hosted_app(token=TOKEN)

    assert {
        entry["name"]: entry["route"] for entry in app.state.registry.describe()
    } == {
        "dots.mocr": "/ocr",
        "glm-ocr": "/ocr",
        "paddleocr-vl": "/ocr",
        "pp-ocrv6": "/ocr",
        "docling": "/to-markdown",
        "gliner": "/ner",
        "whisper-turbo": "/v1/transcribe",
    }
    assert "surya2" not in {entry["name"] for entry in app.state.registry.describe()}

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/capabilities").status_code == 401


def test_hosted_app_keeps_moss_behind_its_existing_worker_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket_models.modal_service import create_hosted_app

    _clear_worker_environment(monkeypatch)
    monkeypatch.setenv(
        "FRISKET_TRANSCRIPTION_MOSS_WORKER_URL", "https://moss-worker.example"
    )
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_MOSS_WORKER_TOKEN", "worker-token")

    app = create_hosted_app(token=TOKEN)
    (endpoint,) = app.state.transcription_gateway.registry.endpoints()

    assert endpoint.expected_engine == "moss"
    assert endpoint.descriptor == MOSS_DESCRIPTOR
