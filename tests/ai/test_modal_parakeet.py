"""Acceptance contract for the isolated Parakeet Modal worker."""

from __future__ import annotations

from typing import Any

import pytest

from frisket.ai.models import modal_parakeet


def _original(value: Any) -> Any:
    return next(
        (
            original
            for name, original in vars(value).items()
            if name.startswith("_sync_original_")
        ),
        value,
    )


def _function_spec(module: Any, name: str) -> tuple[Any, Any]:
    function = _original(module.app.registered_functions[name])
    return function._spec, function._webhook_config


def _named_secrets(spec: Any) -> set[str]:
    return {
        secret._name
        for secret in (_original(item) for item in spec.secrets)
        if secret._name is not None
    }


def test_modal_registers_parakeet_as_an_isolated_worker() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_parakeet, modal_sidecar

    worker, webhook = _function_spec(modal_sidecar, "parakeet_worker")
    gateway, _ = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is _original(modal_parakeet.image)
    assert _original(worker.image) is not _original(gateway.image)
    assert _named_secrets(worker) == {"frisket-moss-link"}
    assert (worker.gpus, worker.cpu, worker.memory) == ("T4", 2.0, 16384)
    assert webhook.web_server_port == 9000
    assert webhook.web_server_startup_timeout == 1800


def test_modal_provides_parakeet_worker_url_and_private_token_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.models import modal_sidecar

    class Worker:
        def get_web_url(self) -> str:
            return "https://parakeet-worker.example.test"

    monkeypatch.setenv("FRISKET_MOSS_WORKER_TOKEN", "private-worker-token")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "public-edge-token")

    assert modal_sidecar.parakeet_worker_environment(Worker()) == {
        "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_URL": (
            "https://parakeet-worker.example.test"
        ),
        "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_TOKEN": ("private-worker-token"),
    }


def test_modal_refuses_missing_parakeet_private_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.models import modal_sidecar

    class Worker:
        def get_web_url(self) -> str:
            return "https://parakeet-worker.example.test"

    monkeypatch.delenv("FRISKET_MOSS_WORKER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="FRISKET_MOSS_WORKER_TOKEN"):
        modal_sidecar.parakeet_worker_environment(Worker())


def test_parakeet_worker_maps_private_token_and_runtime_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def popen(command: list[str], *, env: dict[str, str]) -> None:
        captured["command"] = command
        captured["env"] = env

    monkeypatch.setenv("FRISKET_MOSS_WORKER_TOKEN", "private-worker-token")
    monkeypatch.setenv("MODAL_IMAGE_ID", "im-Abc123")
    monkeypatch.setattr(modal_parakeet.subprocess, "Popen", popen)

    modal_parakeet.parakeet_worker()

    assert captured["command"] == [
        "uvicorn",
        "--factory",
        "frisket_parakeet_tdt.app:create_app",
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
    ]
    worker_env = captured["env"]
    assert worker_env["FRISKET_TRANSCRIPTION_WORKER_TOKEN"] == ("private-worker-token")
    assert worker_env["FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID"] == "modal:im-Abc123"


def test_parakeet_worker_refuses_missing_private_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRISKET_MOSS_WORKER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="FRISKET_MOSS_WORKER_TOKEN"):
        modal_parakeet.parakeet_worker()
