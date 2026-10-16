"""Async import transports hand synchronous preparation to a worker thread."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from frisket.actions.types import PdfPage
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.ops import url_import
from frisket.server.app import create_app
from tests.server.test_server_import_pdf_routes import PNG_1X1, _pdf_bytes


@pytest.mark.parametrize("transport", ["pdf", "urls"])
def test_pdf_import_bridges_publish_search_preparation(
    tmp_path, monkeypatch, transport
):
    raw_pdf = _pdf_bytes("")

    def render(self, document, *, dpi):
        return {
            1: self._blobs.stage(
                io.BytesIO(PNG_1X1),
                filename="scan-p0001.png",
                mime="image/png",
                role=PdfPage(document=document, page=1),
            )
        }

    monkeypatch.setattr(AdmittedPdfPageRenderer, "render", render)
    monkeypatch.setattr(
        url_import,
        "download_url",
        lambda *_args, **_kwargs: (raw_pdf, "application/pdf", "scan.pdf", None),
    )
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Preparation bridge"}).json()["id"]
    if transport == "pdf":
        response = client.post(
            f"/api/projects/{pid}/import/pdf",
            files={"file": ("scan.pdf", raw_pdf, "application/pdf")},
        )
    else:
        response = client.post(
            f"/api/projects/{pid}/import/urls",
            json={"urls": ["https://example.test/scan.pdf"], "column": "media"},
        )
    assert response.status_code == 200, response.text
    batches = client.get(f"/api/projects/{pid}/search-preparation").json()["batches"]
    assert len(batches) == 1
    assert batches[0]["sheet_id"] == response.json()["sheet_id"]
    assert any(group["kind"] == "ocr" for group in batches[0]["groups"])
    assert client.get("/api/providers/models/pulls").json()["pulls"] == []
