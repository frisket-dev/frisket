from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.engines import (
    EXTRAS,
    Engine,
    Registry,
    default_registry,
)

TOKEN = "expansion-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\n stub-bytes"


def _stub_ocr(images: list[bytes]) -> list[dict]:
    return [
        {
            "text": f"page {i + 1}",
            "blocks": [
                {
                    "text": f"page {i + 1}",
                    "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]],
                    "score": 0.98,
                }
            ],
        }
        for i in range(len(images))
    ]


def _expansion_registry() -> Registry:
    """All public OCR engines plus one unavailable stub for the 503 path."""
    return Registry(
        [
            Engine(
                "dots.mocr",
                "/ocr",
                [],
                lambda: _stub_ocr,
                models=["dots-studio/dots.mocr"],
            ),
            Engine(
                "glm-ocr",
                "/ocr",
                [],
                lambda: _stub_ocr,
                models=["zai-org/GLM-OCR"],
            ),
            Engine(
                "surya2",
                "/ocr",
                [],
                lambda: _stub_ocr,
                models=["datalab-to/surya-ocr-2"],
            ),
            Engine(
                "paddleocr-vl",
                "/ocr",
                [],
                lambda: _stub_ocr,
                models=["PaddleOCR-VL"],
            ),
            Engine(
                "pp-ocrv6",
                "/ocr",
                [],
                lambda: _stub_ocr,
                models=["PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"],
            ),
            # extra genuinely absent → available=false, 503 with install hint
            Engine(
                "dots.mocr-missing",
                "/ocr",
                ["frisket_models_not_a_real_module"],
                lambda: _stub_ocr,
            ),
        ]
    )


@pytest.fixture
def client() -> TestClient:
    app = create_app(token=TOKEN, registry=_expansion_registry(), concurrency=2)
    return TestClient(app)


# ---------- default registry ----------


def test_default_registry_registers_all_public_sidecar_ocr_engines():
    by_name = {e.name: e for e in default_registry()._engines.values()}
    for name in ("dots.mocr", "glm-ocr", "surya2", "paddleocr-vl", "pp-ocrv6"):
        assert by_name[name].route == "/ocr"
        # Each gates capabilities on a real import/configuration condition.
        assert by_name[name].modules
    assert by_name["surya2"].models == ["datalab-to/surya-ocr-2"]


def test_local_ocr_engines_have_truthful_install_boundaries():
    assert "dots.mocr" not in EXTRAS
    assert "glm-ocr" not in EXTRAS
    assert EXTRAS["surya2"] == "ocr"
    assert EXTRAS["paddleocr-vl"] == "ocr-paddle"
    assert EXTRAS["pp-ocrv6"] == "ocr-paddle"


# ---------- HTTP surface (stubbed engines) ----------


def test_capabilities_lists_all_public_sidecar_ocr_engines(client):
    body = client.get("/capabilities", headers=AUTH).json()
    by_name = {e["name"]: e for e in body["engines"]}
    for name in ("dots.mocr", "glm-ocr", "surya2", "paddleocr-vl", "pp-ocrv6"):
        assert by_name[name]["route"] == "/ocr"
        assert by_name[name]["available"] is True


def test_dots_mocr_ocr_routing_returns_pages(client):
    resp = client.post(
        "/ocr",
        files=[("files", ("p.png", PNG, "image/png"))],
        data={"engine": "dots.mocr"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    page = resp.json()["pages"][0]
    assert isinstance(page["text"], str)
    assert len(page["blocks"][0]["bbox"]) == 4


def test_glm_ocr_routing_returns_pages(client):
    resp = client.post(
        "/ocr",
        files=[("files", ("p.png", PNG, "image/png"))],
        data={"engine": "glm-ocr"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pages"][0]["text"] == "page 1"


def test_surya2_ocr_routing_returns_pages(client):
    resp = client.post(
        "/ocr",
        files=[("files", ("p.png", PNG, "image/png"))],
        data={"engine": "surya2"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    page = resp.json()["pages"][0]
    assert isinstance(page["text"], str)
    assert len(page["blocks"][0]["bbox"]) == 4


def test_paddleocr_vl_ocr_routing_returns_pages(client):
    resp = client.post(
        "/ocr",
        files=[("files", ("p.png", PNG, "image/png"))],
        data={"engine": "paddleocr-vl"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    page = resp.json()["pages"][0]
    assert len(page["blocks"][0]["bbox"]) == 4


def test_pp_ocrv6_ocr_routing_returns_pages(client):
    resp = client.post(
        "/ocr",
        files=[("files", ("p.png", PNG, "image/png"))],
        data={"engine": "pp-ocrv6"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    page = resp.json()["pages"][0]
    assert len(page["blocks"][0]["bbox"]) == 4


def test_dots_mocr_missing_extra_is_503_with_install_hint(client):
    resp = client.post(
        "/ocr",
        files=[("files", ("p.png", PNG, "image/png"))],
        data={"engine": "dots.mocr-missing"},
        headers=AUTH,
    )
    assert resp.status_code == 503
    assert "not installed" in resp.json()["detail"]


def test_surya2_adapter_uses_v2_manager_and_html_block_schema(monkeypatch):
    """Pin the breaking 0.22 API without starting or contacting a backend."""
    from frisket_models import engines

    calls: dict[str, object] = {}
    manager = object()

    class SuryaInferenceManager:
        def __new__(cls):
            calls["manager_constructed"] = True
            return manager

    class RecognitionPredictor:
        def __init__(self, received_manager):
            calls["manager"] = received_manager

        def __call__(self, images, *, full_page):
            calls["images"] = images
            calls["full_page"] = full_page
            return [
                SimpleNamespace(
                    blocks=[
                        SimpleNamespace(
                            html="<p>Second &amp; final</p>",
                            polygon=[[10.2, 20.8], [30, 20], [30, 40], [10, 40]],
                            confidence=0.87654,
                            reading_order=1,
                        ),
                        SimpleNamespace(
                            html="<h2>Hello</h2><p>Frisket</p>",
                            polygon=[[0, 0], [9, 0], [9, 9], [0, 9]],
                            confidence=0.95,
                            reading_order=0,
                        ),
                        SimpleNamespace(
                            html="",
                            polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
                            confidence=1.0,
                            reading_order=2,
                        ),
                    ]
                )
            ]

    class Image:
        @staticmethod
        def open(stream):
            calls["image_bytes"] = stream.read()
            return SimpleNamespace(convert=lambda mode: ("rgb-image", mode))

    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(Image=Image))
    monkeypatch.setitem(sys.modules, "surya", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "surya.inference",
        SimpleNamespace(SuryaInferenceManager=SuryaInferenceManager),
    )
    monkeypatch.setitem(
        sys.modules,
        "surya.recognition",
        SimpleNamespace(RecognitionPredictor=RecognitionPredictor),
    )

    pages = engines.load_surya2()([b"fake-png"])

    assert calls["manager_constructed"] is True
    assert calls["manager"] is manager
    assert calls["full_page"] is True
    assert calls["image_bytes"] == b"fake-png"
    assert pages == [
        {
            "text": "Hello Frisket\nSecond & final",
            "blocks": [
                {
                    "text": "Hello Frisket",
                    "bbox": [[0, 0], [9, 0], [9, 9], [0, 9]],
                    "score": 0.95,
                },
                {
                    "text": "Second & final",
                    "bbox": [[10, 20], [30, 20], [30, 40], [10, 40]],
                    "score": 0.8765,
                },
            ],
        }
    ]


# ---------- installed-state (UNRUN dep-less — skips until extras present) ----------


@pytest.mark.real
def test_paddleocr_vl_real():
    """INSTALLED-STATE: real PaddleOCR-VL over a rendered image. Un-gap with
    ``uv sync --extra ocr-paddle`` (verified: paddleocr 3.7.0 / paddlex[ocr]
    3.7.2 / paddlepaddle 3.3.1 + the PaddleOCR-VL-1.6 weights). The VL
    pipeline is ``paddleocr.PaddleOCRVL`` driven via ``predict()``;
    ``_paddle_result_to_page`` maps its ``parsing_res_list`` blocks
    (``.content``/``.bbox``/``.label``, plus line-level ``spotting_res``
    detail when present) to the shared {text, blocks[{text, bbox}]} shape."""
    pytest.importorskip("paddleocr")
    from PIL import Image, ImageDraw

    from frisket_models.engines import default_registry

    img = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(img).text((10, 20), "HELLO FRISKET", fill="black")
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    app = create_app(token=TOKEN, registry=default_registry(), concurrency=1)
    c = TestClient(app)
    resp = c.post(
        "/ocr",
        files=[("files", ("p.png", buf.getvalue(), "image/png"))],
        data={"engine": "paddleocr-vl"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert "HELLO" in resp.json()["pages"][0]["text"].upper()


@pytest.mark.real
def test_pp_ocrv6_real():
    """Real PP-OCRv6 medium smoke with substantive text and line geometry."""
    pytest.importorskip("paddleocr")
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "ocr"
        / "census-language-card-page-1.png"
    )
    app = create_app(token=TOKEN, registry=default_registry(), concurrency=1)
    response = TestClient(app).post(
        "/ocr",
        files=[("files", (fixture.name, fixture.read_bytes(), "image/png"))],
        data={"engine": "pp-ocrv6"},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    page = response.json()["pages"][0]
    assert "CENSUS" in page["text"].upper()
    assert page["blocks"]
    assert all(len(block["bbox"]) == 4 for block in page["blocks"])
    assert all(0.0 <= block["score"] <= 1.0 for block in page["blocks"])


@pytest.mark.real
def test_surya2_real():
    """Real Surya 2 smoke; requires the ``ocr`` extra and an upstream backend."""
    pytest.importorskip("surya")
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(image).text((10, 20), "HELLO FRISKET", fill="black")
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    app = create_app(token=TOKEN, registry=default_registry(), concurrency=1)
    response = TestClient(app).post(
        "/ocr",
        files=[("files", ("p.png", raw.getvalue(), "image/png"))],
        data={"engine": "surya2"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert "HELLO" in response.json()["pages"][0]["text"].upper()
