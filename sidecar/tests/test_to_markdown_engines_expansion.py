"""Chandra 2 (Datalab, github.com/datalab-to/chandra) joins docling on
/to-markdown — a second, experimental document-VLM engine on the same
route. Mirrors test_ocr_engines_expansion.py's pattern for paddleocr-vl
joining dots.mocr on /ocr: engines are stubbed here (contract tests never load
the real heavy libraries — the default `uv sync` installs no extras), the
`real` tests at the bottom importorskip and stay dormant until
`uv sync --extra convert-chandra` (and the model weights) are present."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.engines import (
    EXTRAS,
    Engine,
    Registry,
    default_registry,
)

TOKEN = "chandra-expansion-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _stub_to_markdown(name: str, data: bytes) -> dict:
    return {"markdown": f"# {name}\n\n{len(data)} bytes", "ocr_used": [True]}


def _expansion_registry() -> Registry:
    """docling alongside chandra on /to-markdown, plus a deliberately
    not-installed chandra to exercise the missing-extra 503/degradation
    path (same shape as the OCR expansion's dots.mocr-missing)."""
    return Registry(
        [
            Engine(
                "docling",
                "/to-markdown",
                [],
                lambda: _stub_to_markdown,
                models=["docling"],
            ),
            Engine(
                "chandra",
                "/to-markdown",
                [],
                lambda: _stub_to_markdown,
                models=["chandra-ocr-2"],
            ),
            # extra genuinely absent → available=false, 503 with install hint
            Engine(
                "chandra-missing",
                "/to-markdown",
                ["frisket_models_not_a_real_module"],
                lambda: _stub_to_markdown,
            ),
        ]
    )


@pytest.fixture
def client() -> TestClient:
    app = create_app(token=TOKEN, registry=_expansion_registry(), concurrency=2)
    return TestClient(app)


# ---------- default registry: chandra joins docling on /to-markdown ----------


def test_default_registry_registers_chandra_alongside_docling():
    by_name = {e.name: e for e in default_registry()._engines.values()}
    assert "docling" in by_name
    assert "chandra" in by_name
    assert by_name["docling"].route == "/to-markdown"
    assert by_name["chandra"].route == "/to-markdown"
    # gates capabilities on a real import module (no torch at startup)
    assert by_name["chandra"].modules == ["chandra"]


def test_chandra_has_its_own_extra():
    # its own extra, engine-qualified like paddleocr-vl/ocr-paddle (two
    # engines sharing a route, each with a truthful missing-extra hint)
    assert EXTRAS["chandra"] == "convert-chandra"
    assert EXTRAS["docling"] == "convert"


# ---------- HTTP surface (stubbed engines) ----------


def test_capabilities_lists_chandra(client):
    body = client.get("/capabilities", headers=AUTH).json()
    by_name = {e["name"]: e for e in body["engines"]}
    assert by_name["chandra"]["route"] == "/to-markdown"
    assert by_name["chandra"]["available"] is True
    assert by_name["chandra"]["models"] == ["chandra-ocr-2"]


def test_chandra_to_markdown_routing_returns_markdown(client):
    resp = client.post(
        "/to-markdown",
        files=[("files", ("report.pdf", b"%PDF- stub", "application/octet-stream"))],
        data={"engine": "chandra"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()["documents"][0]
    assert isinstance(doc["markdown"], str) and doc["markdown"]
    assert doc["ocr_used"] == [True]


def test_chandra_missing_extra_is_503_with_install_hint(client):
    resp = client.post(
        "/to-markdown",
        files=[("files", ("d.pdf", b"%PDF- stub", "application/octet-stream"))],
        data={"engine": "chandra-missing"},
        headers=AUTH,
    )
    assert resp.status_code == 503
    assert "not installed" in resp.json()["detail"]


# ---------- installed-state (UNRUN dep-less — skips until the extra + weights are present) ----------


@pytest.mark.real
def test_chandra_real():
    """INSTALLED-STATE: real Chandra 2 over a tiny rendered document. Un-gap
    with ``uv sync --extra convert-chandra`` (chandra-ocr[hf]: torch +
    transformers + accelerate) — first run downloads the chandra-ocr-2
    weights (OpenRAIL-M license, see pyproject.toml's LICENSE NOTE; a
    product/legal call, not an engineering one). load_chandra's docstring
    documents the verified API: InferenceManager(method='hf').generate(
    [BatchInputItem(image=..., prompt_type='ocr_layout')]) -> list of
    BatchOutputItem with a .markdown field, one per page."""
    pytest.importorskip("chandra")
    import io

    from PIL import Image, ImageDraw

    from frisket_models.engines import default_registry

    img = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(img).text((10, 20), "HELLO FRISKET", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    app = create_app(token=TOKEN, registry=default_registry(), concurrency=1)
    c = TestClient(app)
    resp = c.post(
        "/to-markdown",
        files=[("files", ("p.png", buf.getvalue(), "image/png"))],
        data={"engine": "chandra"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert "HELLO" in resp.json()["documents"][0]["markdown"].upper()
