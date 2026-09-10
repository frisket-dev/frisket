from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.routes.exports import register_project_export_routes
from frisket.server.services.project_exports import ProjectExportError


def _make_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Bundle Test"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name,score\nAda,5\nGrace,4\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return pid, imported.json()["sheet_id"]


class _FailingProjectExportService:
    def action_export(self, project_id: str) -> dict:
        del project_id
        return {"exports": {}}

    def export_bundle(
        self,
        project_id: str,
        *,
        include_media: bool,
        include_traces: bool = False,
    ) -> NoReturn:
        del project_id, include_media, include_traces
        raise ProjectExportError(500, "bundle failed")

    def export_database(self, project_id: str) -> NoReturn:
        del project_id
        raise ProjectExportError(500, "database failed")


def test_project_export_routes_preserve_bundle_and_action_contract(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, sheet_id = _make_project(client)

    action_links = client.get(f"/api/projects/{pid}/actions/export")
    assert action_links.status_code == 200, action_links.text
    exports = action_links.json()["exports"]
    assert exports["bundle"].endswith("/export")
    assert exports["database"].endswith("/export?mode=db")
    assert exports["sheet_csv"] == (
        f"/api/projects/{pid}/exports/sheets?sheet_id={{sheet_id}}&format=csv"
    )
    assert exports["work_log_html"].endswith("/work-log.html")

    sheet_csv = client.get(exports["sheet_csv"].replace("{sheet_id}", str(sheet_id)))
    assert sheet_csv.status_code == 200, sheet_csv.text
    assert sheet_csv.content.startswith(b"\xef\xbb\xbf")

    response = client.get(f"/api/projects/{pid}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert "bundle-test.frisket.zip" in response.headers["content-disposition"]
    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(bundle.namelist())
    assert {"manifest.json", "project.db"} <= names
    assert json.loads(bundle.read("manifest.json"))["format"] == "frisket-bundle"


def test_project_export_routes_preserve_include_media_and_database_mode(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, _ = _make_project(client)
    blob = client.post(
        f"/api/projects/{pid}/import/files",
        files=[("files", ("a.txt", b"hello blob", "text/plain"))],
    )
    assert blob.status_code == 200, blob.text
    project = client.app.state.workspace.get(pid)
    trace = project.path / "traces" / "run-7.jsonl.gz"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_bytes(b"diagnostic")

    slim = zipfile.ZipFile(
        io.BytesIO(
            client.get(f"/api/projects/{pid}/export?include_media=false").content
        )
    )
    assert not any(name.startswith("blobs/") for name in slim.namelist())
    assert not any(name.startswith("traces/") for name in slim.namelist())

    with_traces = zipfile.ZipFile(
        io.BytesIO(
            client.get(f"/api/projects/{pid}/export?include_traces=true").content
        )
    )
    assert "traces/run-7.jsonl.gz" in with_traces.namelist()
    assert json.loads(with_traces.read("manifest.json"))["include_traces"] is True

    response = client.get(f"/api/projects/{pid}/export?mode=db")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.sqlite3"
    assert "bundle-test.frisket.db" in response.headers["content-disposition"]
    assert response.content.startswith(b"SQLite format 3\x00")
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(response.content)).namelist()

    db_path = tmp_path / "snapshot.frisket.db"
    db_path.write_bytes(response.content)
    conn = sqlite3.connect(db_path)
    try:
        sheet_names = [
            row[0] for row in conn.execute("SELECT name FROM sheets ORDER BY id")
        ]
    finally:
        conn.close()
    assert sheet_names == ["rows", "files"]


def test_project_export_routes_preserve_404s_and_mode_validation(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, _ = _make_project(client)

    assert client.get("/api/projects/nope/actions/export").status_code == 404
    assert client.get("/api/projects/nope/export").status_code == 404
    assert client.get("/api/projects/nope/export?mode=db").status_code == 404
    assert client.get(f"/api/projects/{pid}/export?mode=archive").status_code == 422


def test_project_export_filenames_use_safe_download_helper(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post(
        "/api/projects",
        json={"name": 'Unsafe "Export" Name'},
    ).json()["id"]

    response = client.get(f"/api/projects/{pid}/export")

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'filename="{pid}.frisket.zip"' in disposition

    database = client.get(f"/api/projects/{pid}/export?mode=db")
    assert database.status_code == 200, database.text
    assert f'filename="{pid}.frisket.db"' in database.headers["content-disposition"]


def test_project_export_routes_map_service_export_errors() -> None:
    app = FastAPI()
    register_project_export_routes(app, service=_FailingProjectExportService())
    client = TestClient(app)

    bundle = client.get("/api/projects/p/export")
    database = client.get("/api/projects/p/export?mode=db")

    assert bundle.status_code == 500
    assert bundle.json()["detail"] == "bundle failed"
    assert database.status_code == 500
    assert database.json()["detail"] == "database failed"
