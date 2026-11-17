"""A PDF upload publishes search preparation, and recovery retries a failed plan."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from frisket.actions.types import PdfPage
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.server.app import create_app
from frisket.server.route_errors import RouteError
from frisket.server.services.search_preparation import SearchPreparationService


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


@pytest.mark.parametrize("transient_failure", [False, True])
def test_pdf_import_bridge_publishes_search_preparation(
    tmp_path, monkeypatch, transient_failure
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
    planning_unavailable = transient_failure
    original_plan = SearchPreparationService._plan

    def plan(self, *args, **kwargs):
        if planning_unavailable:
            raise RouteError(503, "Preparation is temporarily unavailable")
        return original_plan(self, *args, **kwargs)

    monkeypatch.setattr(SearchPreparationService, "_plan", plan)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Preparation bridge"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/files",
        files={"files": ("scan.pdf", raw_pdf, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    workspace = client.app.state.workspace
    project = workspace.get(pid)
    sheet = response.json()["sheet_id"]
    assert project.row_count(sheet) == 1
    preparation = SearchPreparationService(workspace)
    receipt = project.db.execute(
        "SELECT id FROM receipts WHERE action_kind=?", ("import.files",)
    ).fetchone()
    if transient_failure:
        assert (
            client.get(f"/api/projects/{pid}/search-preparation").json()["batches"]
            == []
        )
        pending = preparation.store.for_receipt(
            storage_org_id=None,
            project_id=pid,
            receipt_id=receipt["id"],
            include_pending=True,
        )
        assert (
            pending is not None and pending.plan is None and pending.settled_at is None
        )
        source = next(
            column for column in project.columns(sheet) if column["type"] == "file"
        )
        [original_row] = project.visible_row_ids(sheet)
        original_value = project.get_values(
            sheet, source["id"], row_ids=[original_row]
        )[original_row]
        added_column = project.add_column(sheet, "Later PDF", type="file")
        project.apply_edits(
            [
                {
                    "row_id": original_row,
                    "column_id": added_column,
                    "value": original_value,
                }
            ]
        )
        project.add_rows(
            sheet, [{source["name"]: original_value}], {source["name"]: source["id"]}
        )
        planning_unavailable = False
        # A fresh server coordinator owns retry; no transport replay is needed.
        preparation = SearchPreparationService(workspace)
        preparation.recover()
        assert preparation.batch_for_receipt(pid, receipt["id"]) == pending.id
        recovered = preparation.store.get(
            pending.id, project_id=pid, storage_org_id=None
        )
        assert {
            (item["row_id"], item["column_id"]) for item in recovered.plan["items"]
        } == {(original_row, source["id"])}
        preparation.recover()
        assert preparation.store.children(pending.id) == []
    batches = client.get(f"/api/projects/{pid}/search-preparation").json()["batches"]
    assert len(batches) == 1
    assert batches[0]["sheet_id"] == response.json()["sheet_id"]
    assert any(group["kind"] == "ocr" for group in batches[0]["groups"])
    assert client.get("/api/providers/models/pulls").json()["pulls"] == []
