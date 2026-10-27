import hashlib
import io
from contextlib import nullcontext

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from frisket.server.routes.pdf_packet_sources import register_pdf_packet_source_routes
from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.services.pdf_packet_splits import PdfPacketSplitService
from frisket.server.workspace import Workspace


def test_source_view_serves_retained_bytes_without_publishing_blob(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="packet")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(workspace, registry=registry, preview_runs=None)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    sink = io.BytesIO()
    writer.write(sink)
    raw = sink.getvalue()
    path = tmp_path / "staged.pdf"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    source = AdmittedPacketSource(
        key="folder/one",
        sha256=digest,
        filename="staged.pdf",
        size=len(raw),
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(path),
    )
    split_id = service.create_from_sources(project_id, [source], organization="pages")[
        "split_id"
    ]
    app = FastAPI()
    register_pdf_packet_source_routes(app, service=service)
    with TestClient(app) as client:
        response = client.get(
            f"/api/projects/{project_id}/import/pdf-packet-splits/{split_id}/source-file?source_key=folder%2Fone"
        )
        assert response.status_code == 200
        assert response.content == raw
        assert response.headers["content-type"] == "application/pdf"
        assert (
            workspace.get(project_id)
            .db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,))
            .fetchone()
            is None
        )
    service.shutdown()
