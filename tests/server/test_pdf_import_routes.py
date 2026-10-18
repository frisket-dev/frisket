from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.pdf_imports import register_pdf_import_routes


def _status(revision=1):
    return {
        "schema_version": "frisket.pdf_import.v1",
        "id": "pdf-one",
        "draft_id": "pdf-one",
        "state": "open",
        "paused": False,
        "revision": revision,
        "created_at": "2026-10-18T00:00:00Z",
        "updated_at": "2026-10-18T00:00:00Z",
        "expires_at": "2026-10-19T00:00:00Z",
        "source_label": "Evidence",
        "preflight": {"state": "ready", "free_bytes": 1000},
        "manifest": {
            "files_done": 2,
            "files_total": 2,
            "bytes_done": 20,
            "bytes_total": 20,
            "sealed": True,
        },
        "upload": {
            "files_done": 0,
            "files_total": 2,
            "bytes_done": 0,
            "bytes_total": 20,
            "sealed": False,
        },
        "analysis": {"files_done": 0, "files_total": 2},
        "category_counts": {
            "text": 0,
            "suspicious": 0,
            "missing": 0,
            "image": 0,
            "other": 0,
            "unreadable": 0,
            "unchecked": 2,
        },
        "sample_ready_counts": {
            "text": 0,
            "suspicious": 0,
            "missing": 0,
            "image": 0,
            "other": 0,
            "unreadable": 0,
        },
        "can_review_text": False,
        "choices": {
            "ocr_policy": "missing_or_suspicious",
            "ocr_engine": "rapidocr",
            "ocr_confirmation": None,
            "semantic_search": True,
            "semantic_setup_confirmation": None,
            "destination": {"kind": "new", "sheet_name": "Evidence"},
        },
        "match": {
            "state": "idle",
            "sheet_id": None,
            "checked": 0,
            "total": 0,
            "matched_count": 0,
            "new_count": 0,
            "error": None,
        },
        "batch_ocr": {
            "state": "idle",
            "engine": None,
            "ocr_policy": None,
            "manifest_revision": None,
            "manifest_hash": None,
            "selected_through": 0,
            "selected_count": 0,
            "estimate": None,
            "needs_confirmation": False,
        },
        "semantic_setup": {
            "setup_ref": "engine-setup:semantic.local@1",
            "display_name": "Local semantic search",
            "ready": False,
            "runtime_ready": True,
            "download_bytes": 123,
            "additional_runtime_download": False,
            "needs_confirmation": True,
            "confirmation_hash": "setup-hash",
            "license": "Apache-2.0",
            "source_url": "https://example.test/model",
            "available": True,
        },
        "preview_jobs": [],
        "cleanup": {"bytes_done": 0, "bytes_total": 0},
        "commit": None,
    }


class _Service:
    def __init__(self, tmp_path: Path):
        self.calls = []
        self.thumb = tmp_path / "thumb.png"
        self.thumb.write_bytes(b"png")

    def create(self, pid, **kwargs):
        self.calls.append(("create", pid, kwargs))
        return _status()

    def list(self, pid):
        return [_status()]

    def status(self, pid, draft_id):
        return _status()

    def append_manifest(self, pid, draft_id, **kwargs):
        self.calls.append(("manifest", kwargs))
        return _status(2)

    def seal_manifest(self, pid, draft_id):
        return _status(3)

    def upload(self, pid, draft_id, uploads, **kwargs):
        self.calls.append(("upload", [value.filename for value in uploads], kwargs))
        return _status(4)

    def set_paused(self, pid, draft_id, paused):
        value = _status(2)
        value["paused"] = paused
        return value

    def update_choices(self, pid, draft_id, **kwargs):
        return _status(2)

    def match_destination(self, pid, draft_id):
        return _status(2)

    def refresh_semantic_setup(self, pid, draft_id, **kwargs):
        return _status(2)

    def samples(self, pid, draft_id, **kwargs):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "category": kwargs["category"],
            "items": [],
            "next_cursor": None,
        }

    def page(self, pid, draft_id, occurrence_id, page):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "thumbnail_url": "/thumb",
            "native_text": "native",
            "ocr_text": None,
            "ocr_blocks": [],
            "ocr_engine": None,
        }

    def thumbnail(self, *args):
        return SimpleNamespace(path=self.thumb, close=lambda: None)

    def estimate_batch_ocr(self, pid, draft_id, **kwargs):
        return {
            "state": "ready",
            "engine": "rapidocr",
            "ocr_policy": "all",
            "manifest_revision": 1,
            "manifest_hash": "a" * 64,
            "selected_through": 2,
            "selected_count": 2,
            "estimate": {"cost": 0},
            "needs_confirmation": False,
        }

    def estimate_ocr_preview(self, pid, draft_id, occurrence_id, page, **kwargs):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "engine": kwargs["engine"],
            "cached": False,
            "estimate": {"cost": 0},
        }

    def start_ocr_preview(self, pid, draft_id, occurrence_id, page, **kwargs):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "job_id": "preview-1",
            "status": "running",
            "receipt_id": None,
            "estimate": {"cost": 0},
            "needs_confirmation": False,
        }

    def freeze_for_commit(self, pid, draft_id, **kwargs):
        return SimpleNamespace(
            commit_id="commit-1",
            manifest_hash="b" * 64,
            committed_total=2,
            available_through=1,
            uploads_complete=False,
        )

    def discard(self, pid, draft_id):
        value = _status(2)
        value["state"] = "cancelled"
        return value


def test_pdf_import_http_contract_and_operation_ids(tmp_path: Path) -> None:
    app = FastAPI()
    service = _Service(tmp_path)
    register_pdf_import_routes(app, service=service)  # type: ignore[arg-type]
    client = TestClient(app)
    created = client.post(
        "/api/projects/p/import/pdf-imports",
        json={"expected_files": 2, "expected_bytes": 20, "source_label": "Evidence"},
    )
    assert created.status_code == 201, created.text
    manifest = client.post(
        "/api/projects/p/import/pdf-imports/pdf-one/manifest",
        json={
            "batch_id": "m1",
            "files": [
                {
                    "client_file_id": "one",
                    "logical_path": "one.pdf",
                    "filename": "one.pdf",
                    "size": 10,
                },
                {
                    "client_file_id": "two",
                    "logical_path": "two.pdf",
                    "filename": "two.pdf",
                    "size": 10,
                },
            ],
        },
    )
    assert manifest.status_code == 200, manifest.text
    uploaded = client.post(
        "/api/projects/p/import/pdf-imports/pdf-one/uploads",
        files=[
            ("files", ("one.pdf", b"%PDF-one", "application/pdf")),
            ("files", ("two.pdf", b"%PDF-two", "application/pdf")),
            ("file_ids", (None, "one")),
            ("file_ids", (None, "two")),
            ("logical_paths", (None, "one.pdf")),
            ("logical_paths", (None, "two.pdf")),
            ("batch_id", (None, "u1")),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert service.calls[-1][2]["file_ids"] == ["one", "two"]
    commit = client.post(
        "/api/projects/p/import/pdf-imports/pdf-one/commit",
        json={"expected_revision": 4, "idempotency_key": "commit-client-1"},
    )
    assert commit.status_code == 202, commit.text
    assert commit.json()["commit_id"] == "commit-1"

    operations = {
        details[method]["operationId"]
        for details in app.openapi()["paths"].values()
        for method in details
        if method in {"get", "post", "patch", "delete"}
    }
    assert {
        "createPdfImport",
        "listPdfImports",
        "getPdfImport",
        "appendPdfImportManifest",
        "sealPdfImportManifest",
        "uploadPdfImportFiles",
        "pausePdfImport",
        "resumePdfImport",
        "updatePdfImportChoices",
        "comparePdfImportDestination",
        "refreshPdfImportSemanticSetup",
        "reconcilePdfImportReselection",
        "listPdfImportSamples",
        "getPdfImportPage",
        "getPdfImportThumbnail",
        "estimatePdfImportBatchOcr",
        "estimatePdfImportOcrPreview",
        "startPdfImportOcrPreview",
        "commitPdfImport",
        "discardPdfImport",
    }.issubset(operations)
