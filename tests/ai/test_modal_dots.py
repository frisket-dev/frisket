"""Acceptance contract for the isolated dots.mocr Modal worker."""

from __future__ import annotations

from pathlib import Path
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


def test_modal_registers_dots_mocr_as_an_isolated_worker() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_sidecar

    worker = _function_spec(modal_sidecar, "dots_mocr_worker")
    gateway = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is not _original(gateway.image)
    assert "frisket-dots-link" in _named_secrets(worker)
    assert "frisket-dots-link" in _named_secrets(gateway)


def test_dots_image_exposes_vllms_existing_python_to_modal() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[2] / "sidecar/workers/dots_mocr/Dockerfile"
    ).read_text()

    assert "RUN ln -s /usr/bin/python3.12 /usr/local/bin/python" in dockerfile


def test_modal_provides_the_dots_worker_url_and_token_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.models import modal_sidecar

    class DotsWorker:
        def get_web_url(self) -> str:
            return "https://dots-worker.example.test"

    monkeypatch.setenv("FRISKET_OCR_DOTS_WORKER_TOKEN", "worker-token")

    assert modal_sidecar.dots_worker_environment(DotsWorker()) == {
        "FRISKET_OCR_DOTS_WORKER_URL": "https://dots-worker.example.test",
        "FRISKET_OCR_DOTS_WORKER_TOKEN": "worker-token",
    }
