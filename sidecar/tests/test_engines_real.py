"""Real-engine smoke tests, importorskip'd per library (dependency
policy: the heavy extras are optional, the default sync installs none of
them, so this whole file SKIPS in the contract env). In an
``uv sync --extra all`` env these load the actual models — first run
downloads weights into the HF/fastembed caches. Marked ``real``."""

from __future__ import annotations

import io
import os
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.engines import default_registry

TOKEN = "real-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

pytestmark = pytest.mark.real


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app(token=TOKEN, registry=default_registry(), concurrency=1)
    return TestClient(app)


def test_capabilities_reflect_installed_extras(client):
    """Whatever subset is installed, capabilities must tell the truth —
    a partial install still serves what it has."""
    body = client.get("/capabilities", headers=AUTH).json()
    for entry in body["engines"]:
        if not entry["available"]:  # missing extra or failed loader
            assert entry["loaded"] is False


def test_embeddings_real(client):
    pytest.importorskip("fastembed")
    resp = client.post(
        "/v1/embeddings",
        json={"input": ["a free press", "an open spreadsheet"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    vecs = [d["embedding"] for d in resp.json()["data"]]
    assert len(vecs) == 2
    assert len(vecs[0]) == len(vecs[1]) > 100  # bge-small = 384 dims
    assert vecs[0] != vecs[1]


def test_rerank_real(client):
    pytest.importorskip("sentence_transformers")
    resp = client.post(
        "/rerank",
        json={
            "query": "who paved the road?",
            "documents": [
                "The paving contract went to the mayor's brother-in-law.",
                "Light rail ridership beat projections this quarter.",
            ],
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0]["index"] == 0  # paving doc outranks transit doc


def test_ner_real(client):
    pytest.importorskip("gliner")
    resp = client.post(
        "/ner",
        json={
            "texts": ["Ada Lovelace wrote programs for Charles Babbage in London."],
            "labels": ["person", "city"],
            "threshold": 0.3,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    ents = resp.json()["results"][0]
    found = {e["text"] for e in ents}
    assert any("Ada" in t for t in found)


def test_dots_mocr_real(client):
    if not all(
        os.environ.get(name)
        for name in (
            "FRISKET_OCR_DOTS_WORKER_URL",
            "FRISKET_OCR_DOTS_WORKER_TOKEN",
        )
    ):
        pytest.skip("dots.mocr worker is not configured")
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(img).text((10, 20), "HELLO FRISKET", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    resp = client.post(
        "/ocr",
        files=[("files", ("page-1.png", buf.getvalue(), "image/png"))],
        data={"engine": "dots.mocr"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    pages = resp.json()["pages"]
    assert len(pages) == 1
    assert "HELLO" in pages[0]["text"].upper()


def test_to_markdown_real(client):
    pytest.importorskip("docling")
    html = (
        b"<html><body><h1>Paving Contracts</h1><p>4 million dollars.</p></body></html>"
    )
    resp = client.post(
        "/to-markdown",
        files=[("files", ("doc.html", html, "application/octet-stream"))],
        data={"engine": "docling"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()["documents"][0]
    assert "Paving Contracts" in doc["markdown"]
    assert isinstance(doc.get("ocr_used") or [], list)


def test_transcribe_real(client):
    pytest.importorskip("faster_whisper")
    # one second of silence — shape check only, no speech expected
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<16000h", *([0] * 16000)))
    resp = client.post(
        "/v1/transcribe",
        files=[("files", ("clip.wav", buf.getvalue(), "audio/wav"))],
        data={
            "contract_version": "frisket.transcription.v1",
            "engine": "whisper-turbo",
            "options": "{}",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert isinstance(result["segments"], list)
    assert isinstance(result["text"], str)
