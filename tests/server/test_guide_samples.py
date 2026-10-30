from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.services.guide_samples import files


def test_catalog_download_and_launch_are_same_inputs(tmp_path):
    client = TestClient(create_app(tmp_path))
    for sample in ("court-filings", "council-recordings"):
        before = client.get("/api/projects").json()
        info = client.get(f"/api/guide-samples/{sample}").json()
        response = client.get(f"/api/guide-samples/{sample}/download")
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assets = files(sample)
        assert {f.name: f.data for f in assets} == {
            name: archive.read(name) for name in archive.namelist()
        }
        assert info["total_bytes"] == sum(len(f.data) for f in assets)
        assert client.get("/api/projects").json() == before
        pid = client.post("/api/projects", json={"name": sample}).json()["id"]
        result = client.post(
            f"/api/projects/{pid}/guide-samples/{sample}/prepare",
            json={"revision": 1, "operation_id": sample},
        ).json()
        assert (
            client.post(
                f"/api/projects/{pid}/guide-samples/{sample}/prepare",
                json={"revision": 1, "operation_id": sample},
            ).json()
            == result
        )
        if sample == "court-filings":
            dest = result["destination"]
            assert dest["kind"] == "pdf_import_draft" and dest["state"] == "ready"
            status = client.get(
                f"/api/projects/{result['project_id']}/import/pdf-imports/{dest['draft_id']}"
            ).json()
            assert status["can_review_text"] and status["upload"]["files_done"] == 6
            assert (
                len(client.get(f"/api/projects/{result['project_id']}/sheets").json())
                == 1
            )
        else:
            assert result["destination"]["kind"] == "sheet"
            assert (
                len(client.get(f"/api/projects/{result['project_id']}/sheets").json())
                == 2
            )


def test_concurrent_launch_and_conflict(tmp_path):
    client = TestClient(create_app(tmp_path))
    body = {"revision": 1, "operation_id": "same"}
    pid = client.post("/api/projects", json={"name": "concurrent"}).json()["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: client.post(
                    f"/api/projects/{pid}/guide-samples/council-recordings/prepare",
                    json=body,
                ),
                range(2),
            )
        )
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert len(client.get("/api/projects").json()) == 1
    assert (
        client.post(
            f"/api/projects/{pid}/guide-samples/court-filings/prepare", json=body
        ).status_code
        == 409
    )
    assert client.get("/api/guide-samples/missing").status_code == 404


def test_pdf_retry_and_discard_do_not_duplicate(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    client = TestClient(app)
    pdf = app.state.pdf_import_service
    original = pdf.upload
    attempts = 0

    def fail_after_upload(*args, **kwargs):
        nonlocal attempts
        result = original(*args, **kwargs)
        attempts += 1
        if attempts == 1:
            raise RuntimeError("connection lost after upload")
        return result

    monkeypatch.setattr(pdf, "upload", fail_after_upload)
    body = {"revision": 1, "operation_id": "pdf-retry"}
    pid = client.post("/api/projects", json={"name": "pdf"}).json()["id"]
    with TestClient(app, raise_server_exceptions=False) as failing:
        assert (
            failing.post(
                f"/api/projects/{pid}/guide-samples/court-filings/prepare", json=body
            ).status_code
            == 500
        )
    response = client.post(
        f"/api/projects/{pid}/guide-samples/court-filings/prepare", json=body
    )
    assert response.status_code == 200
    result = response.json()
    pid, draft = result["project_id"], result["destination"]["draft_id"]
    assert (
        len(client.get(f"/api/projects/{pid}/import/pdf-imports").json()["items"]) == 1
    )
    assert len(client.get(f"/api/projects/{pid}/sheets").json()) == 1
    assert (
        client.delete(f"/api/projects/{pid}/import/pdf-imports/{draft}").status_code
        == 202
    )
    assert (
        client.post(
            f"/api/projects/{pid}/guide-samples/court-filings/prepare", json=body
        ).status_code
        == 409
    )


def test_prepare_retries_a_lost_csv_response_without_duplicate_sheet(
    tmp_path, monkeypatch
):
    from frisket.server.services.import_csv import ImportCsvUploadService

    app = create_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    pid = client.post("/api/projects", json={"name": "retry"}).json()["id"]
    url = f"/api/projects/{pid}/guide-samples/council-recordings/prepare"
    body = {"revision": 1, "operation_id": "retry"}
    original = ImportCsvUploadService.upload_csv
    calls = 0

    def lose_response(self, *args, **kwargs):
        nonlocal calls
        response = original(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            raise RuntimeError("lost response")
        return response

    monkeypatch.setattr(ImportCsvUploadService, "upload_csv", lose_response)
    assert client.post(url, json=body).status_code == 500
    assert client.post(url, json=body).status_code == 200
    assert len(client.get(f"/api/projects/{pid}/sheets").json()) == 2


def test_pdf_creation_response_loss_reuses_bound_draft(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    pid = client.post("/api/projects", json={"name": "pdf create retry"}).json()["id"]
    pdf = app.state.pdf_import_service
    original = pdf.create
    calls = 0

    def lose_response(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise RuntimeError("lost response")
        return result

    monkeypatch.setattr(pdf, "create", lose_response)
    url = f"/api/projects/{pid}/guide-samples/court-filings/prepare"
    body = {"revision": 1, "operation_id": "pdf-create"}
    assert client.post(url, json=body).status_code == 500
    assert client.post(url, json=body).status_code == 200
    assert (
        len(client.get(f"/api/projects/{pid}/import/pdf-imports").json()["items"]) == 1
    )
    assert len(client.get(f"/api/projects/{pid}/sheets").json()) == 1


def test_pdf_creation_key_rejects_changed_binding(tmp_path):
    from frisket.server.services.pdf_imports.storage import PdfImportDraftError
    import pytest

    app = create_app(tmp_path)
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "binding"}).json()["id"]
    pdf = app.state.pdf_import_service
    pdf.create(
        pid, expected_files=1, expected_bytes=12, source_label="A", creation_key="one"
    )
    with pytest.raises(PdfImportDraftError) as error:
        pdf.create(
            pid,
            expected_files=2,
            expected_bytes=12,
            source_label="A",
            creation_key="one",
        )
    assert error.value.status_code == 409


def test_pdf_creation_recovers_process_exit_before_manifest(tmp_path):
    import multiprocessing
    import os

    app = create_app(tmp_path)
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "hard crash"}).json()["id"]
    pdf = app.state.pdf_import_service

    def terminate_during_create():
        pdf._store.write_header = lambda *_args: os._exit(73)
        pdf.create(
            pid,
            expected_files=1,
            expected_bytes=4,
            source_label="Crash",
            creation_key="crash",
        )

    child = multiprocessing.get_context("fork").Process(target=terminate_during_create)
    child.start()
    child.join(20)
    assert child.exitcode == 73
    draft = pdf.create(
        pid,
        expected_files=1,
        expected_bytes=4,
        source_label="Crash",
        creation_key="crash",
    )
    assert draft["state"] == "open"
    assert len(pdf.list(pid)) == 1
