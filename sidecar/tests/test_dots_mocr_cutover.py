"""Acceptance coverage for the hosted OCR-model cutover."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from frisket_models.engines import default_registry, load_paddleocr_vl
from frisket_models.dots_mocr_identity import ENGINE, MODEL_ID, MODEL_REVISION
from frisket_models.modal_service import create_hosted_app

TOKEN = "hosted-models-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_hosted_capabilities_use_dots_while_local_registry_retains_surya2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in list(os.environ):
        if key.startswith("FRISKET_TRANSCRIPTION_"):
            monkeypatch.delenv(key, raising=False)

    app = create_hosted_app(token=TOKEN)

    available = {entry["name"] for entry in default_registry().describe()}
    registered = {entry["name"] for entry in app.state.registry.describe()}
    capabilities = TestClient(app).get("/capabilities", headers=AUTH).json()
    advertised = {entry["name"] for entry in capabilities["engines"]}

    assert ENGINE in available
    assert ENGINE in registered
    assert "surya2" in available
    assert "surya2" not in registered
    assert ENGINE in advertised
    assert "surya2" not in advertised
    assert "pp-ocrv6" in registered
    assert "pp-ocrv6" in advertised


def _stub_paddle(monkeypatch, *, cuda: bool) -> list[dict[str, object]]:
    constructors: list[dict[str, object]] = []

    class PaddleOCRVL:
        def __init__(self, **kwargs: object) -> None:
            constructors.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "paddleocr",
        SimpleNamespace(PaddleOCRVL=PaddleOCRVL),
    )
    monkeypatch.setitem(
        sys.modules,
        "paddle",
        SimpleNamespace(is_compiled_with_cuda=lambda: cuda),
    )
    return constructors


def test_paddleocr_vl_preserves_upstream_device_selection_by_default(
    monkeypatch,
) -> None:
    constructors = _stub_paddle(monkeypatch, cuda=False)
    monkeypatch.delenv("FRISKET_PADDLE_DEVICE", raising=False)
    monkeypatch.delenv("FRISKET_PADDLE_VL_REC_SERVER_URL", raising=False)

    load_paddleocr_vl()
    paddle = default_registry().get("paddleocr-vl").describe()

    assert constructors == [{"pipeline_version": "v1.6"}]
    assert paddle["models"] == ["PaddleOCR-VL-1.6"]


def test_paddleocr_vl_uses_explicit_cuda_device(monkeypatch) -> None:
    constructors = _stub_paddle(monkeypatch, cuda=True)
    monkeypatch.setenv("FRISKET_PADDLE_DEVICE", "gpu:0")

    load_paddleocr_vl()

    assert constructors == [{"pipeline_version": "v1.6", "device": "gpu:0"}]


def test_paddleocr_vl_uses_official_client_for_accelerated_server(
    monkeypatch,
) -> None:
    constructors = _stub_paddle(monkeypatch, cuda=True)
    monkeypatch.setenv("FRISKET_PADDLE_DEVICE", "gpu:0")
    monkeypatch.setenv(
        "FRISKET_PADDLE_VL_REC_SERVER_URL",
        "http://127.0.0.1:8118/v1",
    )

    load_paddleocr_vl()

    assert constructors == [
        {
            "pipeline_version": "v1.6",
            "device": "gpu:0",
            "vl_rec_backend": "vllm-server",
            "vl_rec_server_url": "http://127.0.0.1:8118/v1",
            "vl_rec_api_model_name": "PaddleOCR-VL-1.6-0.9B",
        }
    ]


def test_paddleocr_vl_refuses_a_cpu_only_paddle_runtime(monkeypatch) -> None:
    constructors = _stub_paddle(monkeypatch, cuda=False)
    monkeypatch.setenv("FRISKET_PADDLE_DEVICE", "gpu:0")

    with pytest.raises(RuntimeError, match="CUDA-enabled PaddlePaddle"):
        load_paddleocr_vl()

    assert constructors == []


def test_paddleocr_vl_proxy_uses_the_authenticated_worker(monkeypatch) -> None:
    from frisket_models import engines

    request: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"pages": [{"text": "hello", "blocks": []}]}

    def post(url: str, **kwargs: object) -> Response:
        request.update(url=url, **kwargs)
        return Response()

    monkeypatch.setenv("FRISKET_OCR_PADDLE_WORKER_URL", "https://paddle.example/")
    monkeypatch.setenv("FRISKET_OCR_PADDLE_WORKER_TOKEN", "edge-token")
    monkeypatch.setattr("httpx.post", post)

    pages = engines.load_paddleocr_vl()([b"png"])

    assert pages == [{"text": "hello", "blocks": []}]
    assert request["url"] == "https://paddle.example/ocr"
    assert request["headers"] == {"Authorization": "Bearer edge-token"}
    assert request["data"] == {"engine": "paddleocr-vl"}
    assert request["follow_redirects"] is True


def test_paddleocr_vl_worker_config_is_available_without_local_paddle(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRISKET_OCR_PADDLE_WORKER_URL", "https://paddle.example/")
    monkeypatch.setenv("FRISKET_OCR_PADDLE_WORKER_TOKEN", "edge-token")

    assert default_registry().get("paddleocr-vl").describe()["available"] is True


def test_dots_mocr_proxy_uses_the_authenticated_worker(monkeypatch) -> None:
    from frisket_models import engines

    request: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"pages": [{"text": "hello", "blocks": []}]}

    def post(url: str, **kwargs: object) -> Response:
        request.update(url=url, **kwargs)
        return Response()

    monkeypatch.setenv("FRISKET_OCR_DOTS_WORKER_URL", "https://dots.example/")
    monkeypatch.setenv("FRISKET_OCR_DOTS_WORKER_TOKEN", "link-token")
    monkeypatch.setattr("httpx.post", post)

    pages = engines.load_dots_mocr()([b"png"])

    assert pages == [{"text": "hello", "blocks": []}]
    assert request["url"] == "https://dots.example/ocr"
    assert request["headers"] == {"Authorization": "Bearer link-token"}
    assert request["data"] == {"engine": "dots.mocr"}
    assert request["follow_redirects"] is True


def test_dots_mocr_identity_is_shared_by_gateway_and_worker() -> None:
    from frisket_models import engines

    engine = default_registry().get(ENGINE).describe()

    assert engines.DOTS_MOCR_MODEL_ID == MODEL_ID
    assert engines.DOTS_MOCR_MODEL_REVISION == MODEL_REVISION
    assert engine["models"] == [MODEL_ID]
    assert engine["revision"] == MODEL_REVISION


def test_dots_mocr_is_unavailable_without_worker_configuration(monkeypatch) -> None:
    for name in (
        "FRISKET_OCR_DOTS_WORKER_URL",
        "FRISKET_OCR_DOTS_WORKER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    engine = default_registry().get("dots.mocr")

    assert engine.describe()["available"] is False
    with pytest.raises(RuntimeError, match="FRISKET_OCR_DOTS_WORKER_URL"):
        engine.get()
