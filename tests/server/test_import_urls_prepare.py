from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.engine.executor import ExecutorDeps, UrlImportLimits
from frisket.ops import url_import
from frisket.ops.ytdlp import DownloadedMedia
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.imports import register_import_urls_routes
from frisket.server.services.import_urls import ImportUrlsService
from frisket.server.services.import_urls_prepare import ImportUrlsPrepareService
from frisket.server.services.pdf_imports.inventory import PdfDraftInventory
from frisket.server.services.pdf_imports.service import PdfImportService
from frisket.server.workspace import Workspace


class RecordingAdopter:
    def __init__(self, service: PdfImportService):
        self.service = service
        self.entries: list[dict[str, Any]] = []
        self.contents: dict[str, bytes] = {}
        self.staging: Path | None = None

    def adopt_staged(self, project_id, staging, entries, *, source_label):
        self.staging = staging
        self.entries = entries
        self.contents = {
            entry["logical_path"]: (staging / entry["path"]).read_bytes()
            for entry in entries
            if "path" in entry
        }
        return self.service.create(
            project_id,
            expected_files=len(entries),
            expected_bytes=sum(item["size"] for item in entries),
            source_label=source_label,
        )

    def discard(self, project_id, draft_id):
        return self.service.discard(project_id, draft_id)


def _client(tmp_path, *, limit=None, real_adopter=False):
    workspace = Workspace(
        tmp_path / "workspace",
        enable_local_model_pull=False,
        executor_deps_factory=lambda _pid, _request: ExecutorDeps(
            url_import_limits=UrlImportLimits(max_urls=limit)
        ),
    )
    pid = workspace.create("URL preparation", project_id="url-prepare")["id"]
    service = PdfImportService(workspace)
    adopter = service if real_adopter else RecordingAdopter(service)
    app = FastAPI()
    register_route_error_handler(app)
    register_import_urls_routes(
        app,
        service=ImportUrlsService(workspace),
        prepare_service=ImportUrlsPrepareService(workspace, pdf_import_service=adopter),
    )
    return TestClient(app), workspace, pid, adopter


def test_prepare_reuses_acquisition_and_preserves_occurrences_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def download(url):
        calls.append(url)
        return b"%PDF-same-bytes", "application/pdf", "report.pdf", None

    monkeypatch.setattr(url_import, "download_url", download)
    client, workspace, pid, adopter = _client(tmp_path)
    urls = [
        "https://one.example/report.pdf",
        "not-a-url",
        "https://two.example/report.pdf",
    ]
    response = client.post(
        f"/api/projects/{pid}/import/urls/prepare", json={"urls": urls}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "pdf_import"
    assert body["downloaded"] == 2
    assert body["failed"] == 1
    assert body["warnings"]
    assert calls == [urls[0], urls[2]]
    entries = adopter.entries
    assert [entry["source_url"] for entry in entries] == urls
    assert entries[0]["logical_path"] != entries[2]["logical_path"]
    assert entries[0]["sha256"] == entries[2]["sha256"]
    assert entries[1]["acquisition_error"] == "URL must start with http:// or https://"
    assert "sha256" not in entries[1] and "path" not in entries[1]
    assert entries[1]["size"] == 0
    assert len(adopter.contents) == 2
    assert adopter.staging is not None and not adopter.staging.exists()
    project = workspace.get(pid)
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_prepare_keeps_non_pdf_provider_acquisition_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_import.ytdlp,
        "download_media",
        lambda _url: DownloadedMedia(
            b"video",
            "video/mp4",
            "movie.mp4",
            duration_seconds=7,
            metadata={"title": "Video title"},
        ),
    )
    client, _workspace, pid, adopter = _client(tmp_path)
    response = client.post(
        f"/api/projects/{pid}/import/urls/prepare",
        json={
            "urls": ["https://www.youtube.com/watch?v=abc12345678"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "pdf_import"
    assert response.json()["downloaded"] == 1
    entry = adopter.entries[0]
    assert entry["mime"] == "video/mp4"
    assert entry["provider"] == "youtube"
    assert entry["metadata"]["_media_acquisition_v1"]["title"] == "Video title"
    assert entry["metadata"]["_media_acquisition_v1"]["duration_seconds"] == 7


def test_prepare_checks_deployment_limit_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(url_import, "download_url", lambda url: calls.append(url))
    client, _workspace, pid, adopter = _client(tmp_path, limit=1)
    response = client.post(
        f"/api/projects/{pid}/import/urls/prepare",
        json={
            "urls": [
                "https://one.example/report.pdf",
                "https://two.example/report.pdf",
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "URL count exceeds the deployment limit of 1"
    assert calls == [] and adopter.entries == []


def test_acquisition_inventory_retains_error_rows_without_blob_items(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inventory.db"
    digest = hashlib.sha256(b"pdf").hexdigest()
    with PdfDraftInventory(path) as inventory:
        inventory.append_manifest_batch(
            "manifest",
            [
                {
                    "occurrence_id": "success",
                    "logical_path": "one/report.pdf",
                    "filename": "report.pdf",
                    "size": 3,
                },
                {
                    "occurrence_id": "failed",
                    "logical_path": "two/report.pdf",
                    "filename": "report.pdf",
                    "size": 0,
                },
            ],
            expected_files=2,
            expected_bytes=3,
        )
        inventory.seal_manifest()
        inventory.stage_batch(
            "upload",
            [
                {
                    "occurrence_id": "success",
                    "logical_path": "one/report.pdf",
                    "sha256": digest,
                    "size": 3,
                    "mime": "application/pdf",
                    "media_kind": "pdf",
                }
            ],
        )
        facts = [
            {
                "occurrence_id": "success",
                "source_url": "https://one.example/report.pdf",
                "provider": "direct",
                "metadata": {"title": "First"},
            },
            {
                "occurrence_id": "failed",
                "source_url": "https://two.example/report.pdf",
                "provider": "direct",
                "metadata": {},
            },
        ]
        inventory.record_acquisitions(facts)
        inventory.stage_acquisition_errors(
            [{"occurrence_id": "failed", "error": "Download failed"}]
        )
        inventory.stage_acquisition_errors(
            [{"occurrence_id": "failed", "error": "Download failed"}]
        )
        stats = inventory.stats()
        assert stats["staged_count"] == 2 and stats["staged_bytes"] == 3
        assert stats["analyzed_count"] == stats["unreadable_count"] == 1
        assert inventory.base.totals() == {"count": 1, "bytes": 3}
        rows = inventory.staged_page(after=0, limit=5)
        assert rows[1]["sha256"] is None and rows[1]["staged_ordinal"] is None
        assert rows[1]["source_url"] == facts[1]["source_url"]
        assert rows[0]["metadata"] == {"title": "First"}
        inventory.initialize_commit_projection()
        projected = inventory.projection_page(after=0, limit=5)
        assert [row["occurrence_id"] for row in projected] == ["success", "failed"]
        assert projected[1]["error"] == "Download failed"
        inventory.record_acquisitions(facts)
        with pytest.raises(ValueError, match="changed after admission"):
            inventory.record_acquisitions(
                [{**facts[0], "source_url": "https://changed.example"}]
            )
    with PdfDraftInventory(path) as reopened:
        assert reopened.selected("success")["metadata"] == {"title": "First"}
        assert (
            reopened.samples("unreadable", after="", limit=6)[0][0]["source_url"]
            == facts[1]["source_url"]
        )


def test_url_preparation_adopts_real_inventory_without_publishing(
    tmp_path, monkeypatch
):
    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(buffer)
    monkeypatch.setattr(
        url_import,
        "download_url",
        lambda _url: (buffer.getvalue(), "application/pdf", "report.pdf", None),
    )
    client, workspace, pid, service = _client(tmp_path, real_adopter=True)
    response = client.post(
        f"/api/projects/{pid}/import/urls/prepare",
        json={"urls": ["https://example.org/report.pdf", "broken-url"]},
    )
    assert response.status_code == 200, response.text
    draft = response.json()["draft"]
    assert draft["manifest"]["files_total"] == 2
    draft = service.analyze_staged(pid, draft["draft_id"])
    directory = workspace.root / ".pdf_import_drafts" / draft["draft_id"]
    with PdfDraftInventory(directory / "inventory.db") as inventory:
        items = inventory.staged_page(after=0, limit=10)
    assert len(items) == 2
    assert [item["source_url"] for item in items] == [
        "https://example.org/report.pdf",
        "broken-url",
    ]
    assert items[0]["page_count"] == 1
    assert items[1]["sha256"] is None
    assert items[1]["error"]
    assert workspace.get(pid).sheets() == []
