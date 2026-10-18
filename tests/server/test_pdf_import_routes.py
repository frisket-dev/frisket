from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.pdf_imports import register_pdf_import_routes
from frisket.server.services.pdf_imports import PdfImportService
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities
from frisket.server.workspace import Workspace


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
            "ocr_selection": {"kind": "engine", "engine": "rapidocr"},
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
            "new_category_counts": {
                "text": 0,
                "suspicious": 0,
                "missing": 0,
                "image": 0,
                "other": 0,
                "unreadable": 0,
                "unchecked": 0,
            },
            "error": None,
        },
        "batch_ocr": {
            "state": "idle",
            "revision": None,
            "advisory": False,
            "ocr_action_params": None,
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


def _capabilities(_request, _project_id):
    return SelectorCapabilities(
        may_author_actions=True,
        may_run_actions=True,
        manage_model_downloads=True,
    )


def _png_payload() -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (10, 6), (80, 120, 180)).save(output, format="PNG")
    return output.getvalue()


class _PreviewRuns:
    def __init__(self) -> None:
        self.on_page = None
        self.payload = None

    def prepare_ocr_scratch_path(self, _project_id, source, **kwargs):
        self.on_page = kwargs["on_page"]
        self.payload = kwargs["payload"]
        return ({"plan": "ocr"}, source, object(), object(), object())

    def scratch_estimate(self, _project_id, _plan):
        return {
            "rows": 1,
            "cost": 0.0,
            "cost_source": "local",
            "billed_cost": 0,
            "policy_id": "local",
        }

    def start_scratch_preview(self, _project_id, _plan, **_kwargs):
        assert self.on_page is not None
        self.on_page(
            {
                "page": 1,
                "text": "preview text",
                "blocks": [],
                "receipt_id": "receipt-1",
            }
        )
        return SimpleNamespace(
            status_code=202,
            payload={"preview_id": "preview-1", "receipt_id": "receipt-1"},
        )

    def get_preview(self, _project_id, _preview_id):
        return SimpleNamespace(
            status_code=200,
            payload={
                "status": "done",
                "accounting": {"receipt_id": "receipt-1"},
            },
        )

    def cancel_preview(self, _project_id, _preview_id):
        return None


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
            "extension_counts": [],
            "unlisted_extension_count": 0,
        }

    def page(self, pid, draft_id, occurrence_id, page):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "thumbnail_url": "/thumb",
            "native_text": "native",
            "native_text_truncated": False,
            "ocr_text": None,
            "ocr_blocks": [],
            "ocr_action_params": None,
        }

    def thumbnail(self, *args):
        return SimpleNamespace(path=self.thumb, close=lambda: None)

    def estimate_batch_ocr(self, pid, draft_id, **kwargs):
        return {
            "state": "ready",
            "revision": 5,
            "advisory": False,
            "ocr_action_params": {"engine": "rapidocr"},
            "ocr_policy": "all",
            "manifest_revision": 1,
            "manifest_hash": "a" * 64,
            "selected_through": 2,
            "selected_count": 2,
            "estimate": {
                "rows": 2,
                "cost": 0.0,
                "cost_source": "local",
                "billed_cost": 0,
                "policy_id": "local",
            },
            "needs_confirmation": False,
        }

    def estimate_ocr_preview(self, pid, draft_id, occurrence_id, page, **kwargs):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "ocr_action_params": {"engine": "rapidocr"},
            "cached": False,
            "estimate": {
                "rows": 1,
                "cost": 0.0,
                "cost_source": "local",
                "billed_cost": 0,
                "policy_id": "local",
            },
        }

    def start_ocr_preview(self, pid, draft_id, occurrence_id, page, **kwargs):
        return {
            "schema_version": "frisket.pdf_import.v1",
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "ocr_action_params": {"engine": "rapidocr"},
            "job_id": "preview-1",
            "status": "running",
            "receipt_id": None,
            "estimate": {
                "rows": 1,
                "cost": 0.0,
                "cost_source": "local",
                "billed_cost": 0,
                "policy_id": "local",
            },
            "needs_confirmation": False,
        }

    def freeze_for_commit(self, pid, draft_id, **kwargs):
        self.calls.append(("commit", pid, draft_id, kwargs))
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
    register_pdf_import_routes(app, service=service, capabilities_for=_capabilities)  # type: ignore[arg-type]
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
        json={
            "expected_revision": 4,
            "idempotency_key": "commit-client-1",
            "ocr_confirmation": "a" * 64,
        },
    )
    assert commit.status_code == 202, commit.text
    assert commit.json()["commit_id"] == "commit-1"
    assert service.calls[-1][3]["ocr_confirmation"] == "a" * 64

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


def test_real_pdf_import_http_create_upload_sample_page_and_discard(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("PDF HTTP", project_id="pdf-http")["id"])
    preview_runs = _PreviewRuns()
    service = PdfImportService(
        workspace,
        preview_runs=preview_runs,
        semantic_setup_resolver=lambda _pid, _context: {
            "setup_ref": "semantic-ready",
            "display_name": "Local semantic search",
            "ready": True,
            "runtime_ready": True,
            "download_bytes": 0,
            "additional_runtime_download": False,
            "license": "Apache-2.0",
            "source_url": "https://example.test/model",
            "available": True,
        },
    )
    app = FastAPI()
    register_route_error_handler(app)
    register_pdf_import_routes(app, service=service, capabilities_for=_capabilities)
    client = TestClient(app)
    payload = (
        Path(__file__).parents[1] / "fixtures" / "pdf_tables" / "simple_table.pdf"
    ).read_bytes()
    base = f"/api/projects/{project_id}/import/pdf-imports"
    created = client.post(
        base,
        json={
            "expected_files": 2,
            "expected_bytes": 2 * len(payload),
            "source_label": "PDFs",
        },
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft_id"]
    unsafe_manifest = client.post(
        f"{base}/{draft_id}/manifest",
        json={
            "batch_id": "unsafe-manifest",
            "files": [
                {
                    "client_file_id": "folder/one",
                    "logical_path": "one.pdf",
                    "filename": "one.pdf",
                    "size": len(payload),
                }
            ],
        },
    )
    assert unsafe_manifest.status_code == 422, unsafe_manifest.text
    manifest = client.post(
        f"{base}/{draft_id}/manifest",
        json={
            "batch_id": "manifest-1",
            "files": [
                {
                    "client_file_id": occurrence_id,
                    "logical_path": f"{occurrence_id}.pdf",
                    "filename": f"{occurrence_id}.pdf",
                    "size": len(payload),
                }
                for occurrence_id in ("one", "two")
            ],
        },
    )
    assert manifest.status_code == 200, manifest.text
    sealed = client.post(f"{base}/{draft_id}/manifest/seal")
    assert sealed.status_code == 200, sealed.text
    uploaded = client.post(
        f"{base}/{draft_id}/uploads",
        files=[
            ("files", ("one.pdf", payload, "application/pdf")),
            ("files", ("two.pdf", payload, "application/pdf")),
            ("file_ids", (None, "one")),
            ("file_ids", (None, "two")),
            ("logical_paths", (None, "one.pdf")),
            ("logical_paths", (None, "two.pdf")),
            ("batch_id", (None, "upload-1")),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["analysis"]["files_done"] == 2
    samples = client.get(f"{base}/{draft_id}/samples", params={"category": "text"})
    assert samples.status_code == 200, samples.text
    sample = samples.json()["items"][0]
    page = client.get(f"{base}/{draft_id}/files/{sample['occurrence_id']}/pages/1")
    assert page.status_code == 200, page.text
    assert page.json()["native_text"]
    assert page.json()["native_text_truncated"] is False
    preview_selection = {
        "kind": "engine_model",
        "engine": "llm",
        "model": "openai/gpt-4.1-mini",
    }
    choices = client.patch(
        f"{base}/{draft_id}/choices",
        json={
            "expected_revision": uploaded.json()["revision"],
            "ocr_policy": "all",
            "ocr_selection": preview_selection,
            "semantic_search": False,
            "semantic_setup_confirmation": None,
            "destination": {"kind": "new", "sheet_name": "PDFs"},
        },
    )
    assert choices.status_code == 200, choices.text
    paused = client.post(f"{base}/{draft_id}/pause")
    assert paused.status_code == 200, paused.text
    paused_commit = client.post(
        f"{base}/{draft_id}/commit",
        json={
            "expected_revision": paused.json()["revision"],
            "idempotency_key": "paused-commit",
        },
    )
    assert paused_commit.status_code == 409, paused_commit.text
    assert paused_commit.json()["detail"] == (
        "PDF import is paused; resume it before importing"
    )
    resumed = client.post(f"{base}/{draft_id}/resume")
    assert resumed.status_code == 200, resumed.text
    estimate = client.post(
        f"{base}/{draft_id}/files/{sample['occurrence_id']}/pages/1/"
        "ocr-preview/estimate",
        json={"ocr_selection": preview_selection},
    )
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["cached"] is False
    assert estimate.json()["ocr_action_params"] == {
        "engine": "llm",
        "model": "openai/gpt-4.1-mini",
    }
    assert preview_runs.payload["model"] == "openai/gpt-4.1-mini"
    started = client.post(
        f"{base}/{draft_id}/files/{sample['occurrence_id']}/pages/1/ocr-preview",
        json={"ocr_selection": preview_selection},
    )
    assert started.status_code == 202, started.text
    assert started.json()["ocr_action_params"] == estimate.json()["ocr_action_params"]
    refreshed = client.get(f"{base}/{draft_id}")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["preview_jobs"] == [
        {
            "job_id": "preview-1",
            "occurrence_id": sample["occurrence_id"],
            "page": 1,
            "ocr_action_params": estimate.json()["ocr_action_params"],
            "status": "done",
            "receipt_id": "receipt-1",
            "error": None,
        }
    ]
    previewed = client.get(f"{base}/{draft_id}/files/{sample['occurrence_id']}/pages/1")
    assert previewed.status_code == 200, previewed.text
    assert previewed.json()["ocr_text"] == "preview text"
    assert previewed.json()["ocr_action_params"] == estimate.json()["ocr_action_params"]
    assert workspace.get(project_id).sheets() == []
    discarded = client.delete(f"{base}/{draft_id}")
    assert discarded.status_code == 202, discarded.text
    assert discarded.json()["state"] == "cancelled"


def test_real_http_mixed_upload_exposes_image_and_other_review(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Mixed HTTP", project_id="mixed-http")["id"])
    preview_runs = _PreviewRuns()
    service = PdfImportService(workspace, preview_runs=preview_runs)
    monkeypatch.setattr(
        service,
        "_analyze",
        lambda _path: {
            "analysis_state": "done",
            "page_count": 1,
            "category": "missing",
            "sample_pages": [1],
            "error": None,
        },
    )
    app = FastAPI()
    register_route_error_handler(app)
    register_pdf_import_routes(app, service=service, capabilities_for=_capabilities)
    client = TestClient(app)
    pdf = b"%PDF-mixed"
    png = _png_payload()
    note = b"notes"
    selected = [
        ("one", "one.pdf", pdf, "application/octet-stream"),
        ("two", "two.pdf", pdf, "application/pdf"),
        ("photo", "photo.pdf", png, "application/pdf"),
        ("notes", "notes.txt", note, "text/plain"),
    ]
    base = f"/api/projects/{project_id}/import/pdf-imports"
    created = client.post(
        base,
        json={
            "expected_files": len(selected),
            "expected_bytes": sum(len(value[2]) for value in selected),
            "source_label": "Mixed evidence",
        },
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft_id"]
    junk_manifest = client.post(
        f"{base}/{draft_id}/manifest",
        json={
            "batch_id": "mixed-manifest",
            "files": [
                {
                    "client_file_id": occurrence_id,
                    "logical_path": (
                        "__MACOSX/._one.pdf" if occurrence_id == "one" else filename
                    ),
                    "filename": filename,
                    "size": len(payload),
                }
                for occurrence_id, filename, payload, _mime in selected
            ],
        },
    )
    assert junk_manifest.status_code == 422, junk_manifest.text
    assert junk_manifest.json()["detail"] == (
        "PDF import selection contains an ignored system file"
    )
    manifest = client.post(
        f"{base}/{draft_id}/manifest",
        json={
            "batch_id": "mixed-manifest",
            "files": [
                {
                    "client_file_id": occurrence_id,
                    "logical_path": filename,
                    "filename": filename,
                    "size": len(payload),
                }
                for occurrence_id, filename, payload, _mime in selected
            ],
        },
    )
    assert manifest.status_code == 200, manifest.text
    assert client.post(f"{base}/{draft_id}/manifest/seal").status_code == 200
    uploaded = client.post(
        f"{base}/{draft_id}/uploads",
        files=[
            *(
                ("files", (filename, payload, mime))
                for _occurrence_id, filename, payload, mime in selected
            ),
            *(("file_ids", (None, value[0])) for value in selected),
            *(("logical_paths", (None, value[1])) for value in selected),
            ("batch_id", (None, "mixed-upload")),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["category_counts"] == {
        "text": 0,
        "suspicious": 0,
        "missing": 2,
        "image": 1,
        "other": 1,
        "unreadable": 0,
        "unchecked": 0,
    }
    image_samples = client.get(
        f"{base}/{draft_id}/samples", params={"category": "image"}
    )
    assert image_samples.status_code == 200, image_samples.text
    image = image_samples.json()["items"][0]
    page = client.get(f"{base}/{draft_id}/files/{image['occurrence_id']}/pages/1")
    assert page.status_code == 200, page.text
    assert page.json()["native_text"] == ""
    thumbnail = client.get(page.json()["thumbnail_url"])
    assert thumbnail.status_code == 200
    assert thumbnail.content.startswith(b"\x89PNG\r\n\x1a\n")
    preview_url = (
        f"{base}/{draft_id}/files/{image['occurrence_id']}/pages/1/ocr-preview"
    )
    preview_selection = {"kind": "engine", "engine": "rapidocr"}
    estimate = client.post(
        f"{preview_url}/estimate", json={"ocr_selection": preview_selection}
    )
    assert estimate.status_code == 200, estimate.text
    assert preview_runs.payload["mime"] == "image/png"
    started = client.post(
        preview_url,
        json={"ocr_selection": preview_selection, "confirmation": None},
    )
    assert started.status_code == 202, started.text
    previewed = client.get(f"{base}/{draft_id}/files/{image['occurrence_id']}/pages/1")
    assert previewed.json()["ocr_text"] == "preview text"
    other = client.get(f"{base}/{draft_id}/samples", params={"category": "other"})
    assert other.status_code == 200, other.text
    assert other.json()["extension_counts"] == [{"extension": ".txt", "count": 1}]
    current = client.get(f"{base}/{draft_id}")
    assert current.status_code == 200, current.text
    choices = client.patch(
        f"{base}/{draft_id}/choices",
        json={
            "expected_revision": current.json()["revision"],
            "ocr_policy": "missing",
            "ocr_selection": {"kind": "engine", "engine": "rapidocr"},
            "semantic_search": False,
            "semantic_setup_confirmation": None,
            "destination": {"kind": "new", "sheet_name": "Mixed evidence"},
        },
    )
    assert choices.status_code == 200, choices.text
    assert choices.json()["choices"]["ocr_policy"] == "missing"


def test_preview_and_commit_require_action_capability(tmp_path: Path) -> None:
    app = FastAPI()
    register_pdf_import_routes(
        app,
        service=_Service(tmp_path),  # type: ignore[arg-type]
        capabilities_for=lambda _request, _pid: SelectorCapabilities(),
    )
    client = TestClient(app)
    preview = client.post(
        "/api/projects/p/import/pdf-imports/pdf-one/files/one/pages/1/"
        "ocr-preview/estimate",
        json={"ocr_selection": {"kind": "engine", "engine": "rapidocr"}},
    )
    assert preview.status_code == 403
    commit = client.post(
        "/api/projects/p/import/pdf-imports/pdf-one/commit",
        json={"expected_revision": 1, "idempotency_key": "commit-1"},
    )
    assert commit.status_code == 403
