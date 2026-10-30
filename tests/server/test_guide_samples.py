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
        result = client.post(
            f"/api/guide-samples/{sample}/launch",
            json={"revision": 1, "operation_id": sample},
        ).json()
        assert (
            client.post(
                f"/api/guide-samples/{sample}/launch",
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
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: client.post(
                    "/api/guide-samples/council-recordings/launch", json=body
                ),
                range(2),
            )
        )
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert len(client.get("/api/projects").json()) == 1
    assert (
        client.post("/api/guide-samples/court-filings/launch", json=body).status_code
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
    with TestClient(app, raise_server_exceptions=False) as failing:
        assert (
            failing.post(
                "/api/guide-samples/court-filings/launch", json=body
            ).status_code
            == 500
        )
    response = client.post("/api/guide-samples/court-filings/launch", json=body)
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
        client.post("/api/guide-samples/court-filings/launch", json=body).status_code
        == 409
    )
