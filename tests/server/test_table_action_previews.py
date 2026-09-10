from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.engine.executor import import_blob_stage
from frisket.server.routes.action_preview_support import (
    register_action_preview_run_routes,
)
from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.workspace import Workspace
from tests.deterministic_time import controlled_time


@pytest.fixture
def preview_host(tmp_path, monkeypatch):
    monkeypatch.setattr(
        import_blob_stage,
        "probe_for_ingest",
        lambda path, **kwargs: {
            "kind": "file",
            "size_bytes": Path(path).stat().st_size,
        },
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Preview", project_id="preview")
    workspace.create("Other", project_id="other")
    registry = ActionPreviewJobRegistry()
    service = ActionPreviewRunService(workspace, registry=registry)
    app = FastAPI()
    register_action_preview_run_routes(app, service=service)
    with TestClient(app) as client:
        try:
            yield workspace, registry, service, client
        finally:
            registry.shutdown()


def _request(action_id, params):
    return {
        "action_id": action_id,
        "scope": {"kind": "project"},
        "sheet_name": "New table",
        "idempotency_key": "preview-test",
        "params": params,
    }


def _run(client, request):
    started = client.post("/api/projects/preview/actions/v1/preview", json=request)
    assert started.status_code == 202, started.text
    assert started.json()["total"] is None
    preview_id = started.json()["preview_id"]
    payload = None

    def finished():
        nonlocal payload
        polled = client.get(f"/api/projects/preview/actions/v1/preview/{preview_id}")
        assert polled.status_code == 200, polled.text
        payload = polled.json()
        return payload["status"] != "running"

    with controlled_time(timeout=5) as clock:
        clock.wait_until(finished, message="table preview did not finish")
    assert payload["status"] == "done", payload
    return preview_id, payload["result"]


def _assert_unpublished(project):
    for table in ("sheets", "blobs", "ops", "receipts", "runs"):
        assert project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("count,total", [(0, 0), (3, 3), (20, None), (25, None)])
def test_table_samples_have_no_source_identity_or_publication(
    preview_host, count, total
):
    workspace, _registry, _service, client = preview_host
    request = _request(
        "import.rows",
        {
            "columns": [{"name": "city", "type": "text"}],
            "rows": [{"city": f"City {i}"} for i in range(count)],
        },
    )
    request["output_names"] = {"city": "Place"}
    _preview_id, result = _run(client, request)
    assert result["kind"] == "table"
    assert "sheet_id" not in result and "row_ids" not in result
    assert result["total"] == total
    assert result["sampled"] == min(count, 20)
    assert result["rows"] == [
        {"Place": {"value": f"City {i}"}} for i in range(min(count, 20))
    ]
    _assert_unpublished(workspace.get("preview"))


def test_file_preview_bounds_reads_and_artifacts_follow_result_lifetime(
    preview_host, tmp_path
):
    workspace, registry, service, client = preview_host
    path = tmp_path / "sample.bin"
    path.write_bytes(b"sample bytes")
    request = _request(
        "import.files",
        {
            "files": [{"path": str(path)} for _ in range(20)]
            + [{"path": str(tmp_path / "unread.bin")}],
        },
    )
    preview_id, result = _run(client, request)
    assert result["sampled"] == 20 and result["total"] is None
    url = result["rows"][0]["media"]["value"]
    assert url.startswith(
        f"/api/projects/preview/actions/v1/preview/{preview_id}/artifacts/"
    )
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == b"sample bytes"
    assert response.headers["cache-control"] == "private, no-store"
    assert "sandbox" in response.headers["content-security-policy"]
    assert (
        client.get(url.replace("/projects/preview/", "/projects/other/")).status_code
        == 404
    )
    assert client.get(url.rsplit("/", 1)[0] + "/unknown").status_code == 404
    _assert_unpublished(workspace.get("preview"))

    # Replacement keeps the old successful sample and its files queryable.
    _run(
        client,
        _request(
            "import.rows",
            {
                "columns": [{"name": "city", "type": "text"}],
                "rows": [],
            },
        ),
    )
    assert client.get(url).content == b"sample bytes"
    opened, artifact = service.open_preview_file(
        "preview", preview_id, url.rsplit("/", 1)[1]
    )
    assert artifact.path.exists()
    registry.shutdown()
    assert not artifact.path.exists()
    assert client.get(url).status_code == 404
    try:
        assert opened.read() == b"sample bytes"
    finally:
        opened.close()


def test_pdf_http_preview_bounds_page_work_and_keeps_images(
    preview_host, tmp_path, monkeypatch
):
    import shutil
    import subprocess

    from frisket.engine.executor import pdf_page_read

    pypdf = pytest.importorskip("pypdf")
    if shutil.which("pdftoppm") is None:
        pytest.skip("optional PDF renderer not installed")
    workspace, _registry, _service, client = preview_host
    source = tmp_path / "pages.pdf"
    writer = pypdf.PdfWriter()
    for _ in range(25):
        writer.add_blank_page(width=20, height=20)
    writer.write(source)
    commands = []
    processes = []
    original_popen = subprocess.Popen
    original_sandbox = pdf_page_read.run_sandboxed
    original_text = pypdf.PageObject.extract_text
    extracted = []

    def start(command, **kwargs):
        processes.append(command)
        return original_popen(command, **kwargs)

    async def rasterize(command, **kwargs):
        commands.append(command)
        return await original_sandbox(command, **kwargs)

    def extract(page, *args, **kwargs):
        extracted.append(page)
        return original_text(page, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(pdf_page_read, "run_sandboxed", rasterize)
    monkeypatch.setattr(pypdf.PageObject, "extract_text", extract)
    _preview_id, result = _run(
        client,
        _request(
            "import.pdf",
            {
                "source": {"kind": "file", "path": str(source)},
                "dpi": 50,
            },
        ),
    )
    assert len(extracted) == 20
    assert len(commands) == 1
    assert len(processes) == 1  # The fence launcher execs the same batch child.
    assert commands[0][commands[0].index("-l") + 1] == "20"
    assert result["sampled"] == 20 and result["total"] is None
    assert result["warnings"] == []
    assert result["rows"][-1]["page"]["value"] == 20
    image = client.get(result["rows"][-1]["page_image"]["value"])
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    _assert_unpublished(workspace.get("preview"))


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("image/png\r\nX-Injected: yes", "application/octet-stream"),
        ("image/\u2603", "application/octet-stream"),
        ("not a mime", "application/octet-stream"),
        ("application/pdf; harmless=parameter", "application/pdf"),
    ],
)
def test_preview_download_does_not_trust_declared_mime_header(
    preview_host, tmp_path, mime, expected
):
    _workspace, _registry, _service, client = preview_host
    path = tmp_path / "sample.bin"
    path.write_bytes(b"sample")
    _, result = _run(
        client, _request("import.files", {"files": [{"path": str(path), "mime": mime}]})
    )
    response = client.get(result["rows"][0]["media"]["value"])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == expected
    assert "x-injected" not in response.headers
    assert response.content == b"sample"


@pytest.mark.parametrize(
    "action_id,params,code",
    [
        (
            "import.urls",
            {"urls": ["https://example.com/file.pdf"]},
            "preview_effect_requires_run",
        ),
    ],
)
def test_unadmitted_table_readers_refuse_before_invocation(
    preview_host, monkeypatch, action_id, params, code
):
    from frisket.engine.executor.url_import_read import AdmittedUrlImporter

    def unexpected(*args, **kwargs):
        pytest.fail("unsupported preview must not invoke the reader")

    monkeypatch.setattr(AdmittedUrlImporter, "read", unexpected)
    workspace, _registry, _service, client = preview_host
    response = client.post(
        "/api/projects/preview/actions/v1/preview", json=_request(action_id, params)
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert "preview_id" not in response.json()
    _assert_unpublished(workspace.get("preview"))
