"""Acceptance contract for the isolated GLM-OCR Modal worker."""

from __future__ import annotations

from typing import Any

import pytest


def _original(value: Any) -> Any:
    return next(
        (
            original
            for name, original in vars(value).items()
            if name.startswith("_sync_original_")
        ),
        value,
    )


def _function_spec(module: Any, name: str) -> Any:
    function = _original(module.app.registered_functions[name])
    return function._spec


def _named_secrets(spec: Any) -> set[str]:
    return {
        secret._name
        for secret in (_original(item) for item in spec.secrets)
        if secret._name is not None
    }


def test_modal_registers_glm_ocr_as_an_isolated_worker() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_sidecar

    worker = _function_spec(modal_sidecar, "glm_ocr_worker")
    gateway = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is not _original(gateway.image)
    assert "frisket-models-edge" in _named_secrets(worker)
    assert worker.gpus == "L4"
    assert worker.memory == 16384


def test_modal_provides_glm_worker_url_and_edge_token_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.models import modal_sidecar

    class Worker:
        def get_web_url(self) -> str:
            return "https://glm-worker.example.test"

    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "worker-token")

    assert modal_sidecar.glm_worker_environment(Worker()) == {
        "FRISKET_OCR_GLM_WORKER_URL": "https://glm-worker.example.test",
        "FRISKET_OCR_GLM_WORKER_TOKEN": "worker-token",
    }
