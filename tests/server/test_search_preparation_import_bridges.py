"""Async import transports hand synchronous preparation to a worker thread."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from frisket.actions.types import PdfPage
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.ops import url_import
from frisket.server.app import create_app
from frisket.server.route_errors import RouteError
from frisket.server.services.search_preparation import SearchPreparationService
from tests.server.test_server_import_pdf_routes import PNG_1X1, _pdf_bytes


@pytest.mark.parametrize("transport", ["pdf", "urls"])
@pytest.mark.parametrize("transient_failure", [False, True])
def test_pdf_import_bridges_publish_search_preparation(
    tmp_path, monkeypatch, transport, transient_failure
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
    planning_unavailable = transient_failure
    original_plan = SearchPreparationService._plan

    def plan(self, *args, **kwargs):
        if planning_unavailable:
            raise RouteError(503, "Preparation is temporarily unavailable")
        return original_plan(self, *args, **kwargs)

    monkeypatch.setattr(SearchPreparationService, "_plan", plan)
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
    workspace = client.app.state.workspace
    project = workspace.get(pid)
    sheet = response.json()["sheet_id"]
    assert project.row_count(sheet) == 1
    preparation = SearchPreparationService(workspace)
    receipt = project.db.execute(
        "SELECT id FROM receipts WHERE action_kind=?", (f"import.{transport}",)
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
