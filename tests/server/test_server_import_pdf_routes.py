from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from frisket.actions.types import PdfPage
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.server.app import create_app
from frisket.server.services import import_pdf as pdf_service
from frisket.server.services.import_uploads import AdmittedUpload


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _pdf_bytes(text: str = "Audit Findings 2026") -> bytes:
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n",
        b"4 0 obj<</Length "
        + str(len(content)).encode()
        + b">>stream\n"
        + content
        + b"\nendstream\nendobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for obj in objs:
        offsets.append(len(out))
        out += obj
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += (f"{off:010d} 00000 n \n").encode()
    out += (
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    )
    return bytes(out)


def test_import_pdf_route_preserves_transport_response_and_uses_typed_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renders = []

    def render(self, document, *, dpi):
        renders.append(dpi)
        return {
            1: self._blobs.stage(
                io.BytesIO(PNG_1X1),
                filename="docket-p0001.png",
                mime="image/png",
                role=PdfPage(document=document, page=1),
            )
        }

    monkeypatch.setattr(AdmittedPdfPageRenderer, "render", render)
    requests = []
    execute = pdf_service.run_action_spec

    def record_request(project, request, **kwargs):
        requests.append(request)
        assert set(request) == {
            "action_id",
            "scope",
            "sheet_name",
            "params",
            "output_names",
            "idempotency_key",
        }
        assert request["action_id"] == "import.pdf"
        assert request["scope"] == {"kind": "project"}
        assert request["sheet_name"] == "docket"
        assert request["output_names"] == {}
        params = request["params"]
        assert set(params) == {"source", "dpi", "render_pages"}
        assert params["dpi"] == 150 and params["render_pages"] is True
        assert set(params["source"]) == {"kind", "path", "label"}
        assert params["source"]["kind"] == "file"
        assert params["source"]["label"] == "docket.pdf"
        admitted = kwargs["deps"].local_file_sources[params["source"]["path"]]
        assert not admitted.stream.closed
        return execute(project, request, **kwargs)

    monkeypatch.setattr(pdf_service, "run_action_spec", record_request)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "PDF Upload"}).json()["id"]

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/pdf"
    ]["post"]
    assert operation.get("x-frisket-v1-transport") == {
        "state": "v1_product_transport",
        "transport": "multipart_upload",
        "action_kind": "import.pdf",
        "v1_task": "v1-import-pdf-action-and-http-bridge",
        "canonical_action_route": "/api/projects/{pid}/actions/v1/run#import.pdf",
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportPdfResponse"
    }
    assert operation["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActionResult"
    }

    response = client.post(
        f"/api/projects/{pid}/import/pdf",
        files={"file": ("docket.pdf", _pdf_bytes(), "application/pdf")},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "sheet_id": 1,
        "rows": 1,
        "pages": 1,
        "columns": ["page", "text", "source", "page_image"],
    }
    project = client.app.state.workspace.get(pid)
    receipt = project.db.execute("SELECT action_kind FROM receipts").fetchone()
    assert receipt is not None
    assert receipt["action_kind"] == "import.pdf"
    replay = client.post(
        f"/api/projects/{pid}/import/pdf",
        files={"file": ("docket.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    assert requests[0] == requests[1]
    assert renders == [150]
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_pdf_upload_cancellation_keeps_source_open_until_worker_exits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_exited = threading.Event()
    borrowed_sources = []
    open_at_worker_exit = []

    def blocked_run_action(_project, request, **kwargs):
        admitted_path = request["params"]["source"]["path"]
        source = kwargs["deps"].local_file_sources[admitted_path].stream
        borrowed_sources.append(source)
        worker_started.set()
        assert release_worker.wait(5), "test did not release the PDF worker"
        open_at_worker_exit.append(not source.closed)
        worker_exited.set()
        return SimpleNamespace(status="completed", outputs=[])

    monkeypatch.setattr(pdf_service, "run_action_spec", blocked_run_action)
    app = create_app(tmp_path / "workspace")
    pid = app.state.workspace.create("Cancelled PDF")["id"]
    service = pdf_service.ImportPdfUploadService(app.state.workspace)
    raw = _pdf_bytes()
    source = io.BytesIO(raw)
    upload = AdmittedUpload(
        filename="docket.pdf",
        mime="application/pdf",
        source=source,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )

    async def cancel_while_worker_owns_source() -> bool:
        task = asyncio.create_task(
            service.upload_pdf(
                pid,
                upload=upload,
                sheet_name=None,
                dpi=150,
            )
        )
        assert await asyncio.to_thread(worker_started.wait, 5)
        assert len(borrowed_sources) == 1
        cancelled = False
        try:
            task.cancel()
            for _ in range(4):
                await asyncio.sleep(0)
            assert not borrowed_sources[0].closed
        finally:
            release_worker.set()
            assert await asyncio.to_thread(worker_exited.wait, 5)
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
        return cancelled

    assert asyncio.run(cancel_while_worker_owns_source())
    assert open_at_worker_exit == [True]
    assert not borrowed_sources[0].closed
    source.close()
    assert source.closed


def test_pdf_upload_without_renderer_preserves_text_and_requested_image_column(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(AdmittedPdfPageRenderer, "render", lambda *args, **kwargs: {})
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Text PDF"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/pdf?sheet_name=Findings&dpi=300",
        files={"file": ("docket.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "sheet_id": 1,
        "rows": 1,
        "pages": 1,
        "columns": ["page", "text", "source", "page_image"],
    }


def test_pdf_upload_default_name_allocates_and_replays_but_explicit_name_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(AdmittedPdfPageRenderer, "render", lambda *args, **kwargs: {})
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "PDF names"}).json()["id"]
    endpoint = f"/api/projects/{pid}/import/pdf"
    first = client.post(
        endpoint,
        files={"file": ("docket.pdf", _pdf_bytes("First"), "application/pdf")},
    )
    assert first.status_code == 200, first.text
    upload = {"file": ("docket.pdf", _pdf_bytes("Second"), "application/pdf")}
    allocated = client.post(endpoint, files=upload)
    assert allocated.status_code == 200, allocated.text
    project = client.app.state.workspace.get(pid)
    assert [sheet["name"] for sheet in project.sheets()] == ["docket", "docket-2"]

    replay = client.post(endpoint, files=upload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == allocated.json()
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2

    # Explicit intent differs from omission, including an explicit request for
    # the PDF filename stem that was auto-suffixed on the preceding upload.
    for name in ("docket", "docket-2"):
        refused = client.post(endpoint, files=upload, params={"sheet_name": name})
        assert refused.status_code == 409, refused.text
        assert [error["code"] for error in refused.json()["errors"]] == [
            "duplicate_sheet_name"
        ]
    assert [sheet["name"] for sheet in project.sheets()] == ["docket", "docket-2"]
