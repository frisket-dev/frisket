from __future__ import annotations

import pytest

from frisket_models.engines import default_registry, load_glm_ocr
from frisket_models.glm_ocr_identity import ENGINE, MODEL_ID, MODEL_REVISION


def test_glm_ocr_identity_is_shared_by_gateway_and_worker() -> None:
    from frisket_models import engines

    engine = default_registry().get(ENGINE).describe()

    assert engines.GLM_OCR_MODEL_ID == MODEL_ID
    assert engines.GLM_OCR_MODEL_REVISION == MODEL_REVISION
    assert engine["models"] == [MODEL_ID]
    assert engine["revision"] == MODEL_REVISION


def test_glm_ocr_proxy_uses_the_authenticated_worker(monkeypatch) -> None:
    request: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"pages": [{"text": "hello", "blocks": []}]}

    def post(url: str, **kwargs: object) -> Response:
        request.update(url=url, **kwargs)
        return Response()

    monkeypatch.setenv("FRISKET_OCR_GLM_WORKER_URL", "https://glm.example/")
    monkeypatch.setenv("FRISKET_OCR_GLM_WORKER_TOKEN", "edge-token")
    monkeypatch.setattr("httpx.post", post)

    pages = load_glm_ocr()([b"png"])

    assert pages == [{"text": "hello", "blocks": []}]
    assert request["url"] == "https://glm.example/ocr"
    assert request["headers"] == {"Authorization": "Bearer edge-token"}
    assert request["follow_redirects"] is True


def test_glm_ocr_is_unavailable_without_worker_configuration(monkeypatch) -> None:
    for name in (
        "FRISKET_OCR_GLM_WORKER_URL",
        "FRISKET_OCR_GLM_WORKER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    engine = default_registry().get(ENGINE)

    assert engine.describe()["available"] is False
    with pytest.raises(RuntimeError, match="FRISKET_OCR_GLM_WORKER_URL"):
        engine.get()
