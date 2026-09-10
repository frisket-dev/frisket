from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.ops import url_import as import_family
from frisket.engine.executor import ExecutorDeps, UrlImportLimits
from frisket.server.app import create_app
from tests.action_test_helpers import run_typed_map_request, typed_map_request


def test_import_urls_route_preserves_v1_transport_and_legacy_shape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str] = []

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        seen.append(url)
        return b"ID3fake", "audio/mpeg", "ep1.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "URLs"}).json()["id"]

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/urls"
    ]["post"]
    assert operation.get("x-frisket-v1-transport") == {
        "state": "v1_product_transport",
        "transport": "json_url_import",
        "action_kind": "import.urls",
        "v1_task": "v1-import-urls-action-and-http-bridge",
        "canonical_action_route": "/api/projects/{pid}/actions/v1/run#import.urls",
    }

    response = client.post(
        f"/api/projects/{pid}/import/urls",
        json={
            "urls": ["https://cdn.example/ep1.mp3", "not-a-url"],
            "column": "media",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "sheet_id": 1,
        "rows": 2,
        "downloaded": 1,
        "failed": 1,
    }
    assert seen == ["https://cdn.example/ep1.mp3"]
    project = client.app.state.workspace.get(pid)
    receipt = project.db.execute("SELECT action_kind FROM receipts").fetchone()
    assert receipt is not None
    assert receipt["action_kind"] == "import.urls"


def test_import_urls_allocates_new_sheet_without_reviving_existing_columns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """URL import leaves existing rows and hidden columns untouched."""

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        return b"ID3fake", "audio/mpeg", "ep1.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "URLs"}).json()["id"]

    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("downloads")
    text_column = project.add_column(sheet_id, "text", type="text")
    project.add_rows(
        sheet_id, [{"text": "https://cdn.example/ep1.mp3"}], {"text": text_column}
    )
    extracted = run_typed_map_request(
        project,
        typed_map_request(
            "map.regex_extract",
            sheet_id,
            params={"input_columns": ["text"], "pattern": r"https://\S+"},
            output_names={"extracted": "url"},
            idempotency_key="import_urls_hidden_column@sha256:v1",
        ),
        project_id=pid,
    )
    assert extracted.status == "completed", extracted.errors
    assert project.undo() is not None
    hidden_url_column = project.db.execute(
        "SELECT id, hidden FROM columns WHERE sheet_id=? AND name='url'", (sheet_id,)
    ).fetchone()
    assert hidden_url_column is not None and hidden_url_column["hidden"] == 1

    response = client.post(
        f"/api/projects/{pid}/import/urls",
        json={
            "urls": ["https://cdn.example/ep1.mp3"],
            "sheet_name": "downloads",
            "column": "media",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["downloaded"] == 1
    assert response.json()["sheet_id"] != sheet_id
    assert (
        project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (response.json()["sheet_id"],)
        ).fetchone()["name"]
        == "downloads-2"
    )
    assert project.row_count(sheet_id) == 1
    unchanged = project.db.execute(
        "SELECT id, hidden FROM columns WHERE sheet_id=? AND name='url'", (sheet_id,)
    ).fetchall()
    assert [(row["id"], row["hidden"]) for row in unchanged] == [
        (hidden_url_column["id"], 1)
    ]


def test_import_urls_validation_errors_stay_400(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "URLs"}).json()["id"]

    empty = client.post(f"/api/projects/{pid}/import/urls", json={"urls": []})
    assert empty.status_code == 400
    assert empty.json()["detail"] == "no urls"


def test_import_urls_route_processes_every_url_beyond_the_legacy_ceiling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str] = []
    urls = [f"https://cdn.example/{index}.mp3" for index in range(101)]

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        seen.append(url)
        return f"ID3{url}".encode(), "audio/mpeg", "episode.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Many URLs"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/urls",
        json={"urls": urls},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "sheet_id": 1,
        "rows": len(urls),
        "downloaded": len(urls),
        "failed": 0,
    }
    assert seen == urls
    project = client.app.state.workspace.get(pid)
    assert project.row_count(1) == len(urls)


def test_import_urls_route_uses_an_injected_deployment_limit_before_download(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str] = []

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        seen.append(url)
        return b"ID3fixture", "audio/mpeg", "episode.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            url_import_limits=UrlImportLimits(max_urls=2),
        )
    )
    pid = client.post("/api/projects", json={"name": "Bounded URLs"}).json()["id"]
    exact = ["https://cdn.example/one.mp3", "https://cdn.example/two.mp3"]

    accepted = client.post(f"/api/projects/{pid}/import/urls", json={"urls": exact})
    assert accepted.status_code == 200, accepted.text
    assert seen == exact
    project = client.app.state.workspace.get(pid)
    before = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "rows", "cells", "blobs", "ops", "receipts")
    }

    refused = client.post(
        f"/api/projects/{pid}/import/urls",
        json={"urls": [*exact, "https://cdn.example/three.mp3"]},
    )

    assert refused.status_code == 400
    assert "limit" in refused.json()["detail"].lower()
    assert "2" in refused.json()["detail"]
    assert seen == exact
    canonical = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "import.urls",
            "scope": {"kind": "project"},
            "sheet_name": "canonical downloads",
            "params": {
                "urls": [*exact, "https://cdn.example/three.mp3"],
            },
            "idempotency_key": "canonical-import-urls@sha256:over-bound",
        },
    )
    assert canonical.status_code == 400, canonical.text
    assert canonical.json()["errors"][0]["code"] == "url_limit_exceeded"
    assert seen == exact
    after = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in before
    }
    assert after == before


def test_import_urls_route_uses_factory_limits_once_per_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str] = []
    factory_paths: list[str] = []

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        seen.append(url)
        return b"ID3fixture", "audio/mpeg", "episode.mp3", None

    def executor_deps_factory(project_id: str, request: Any) -> ExecutorDeps:
        assert project_id
        factory_paths.append(str(request.url.path))
        return ExecutorDeps(url_import_limits=UrlImportLimits(max_urls=2))

    monkeypatch.setattr(import_family, "download_url", fake_download)
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            executor_deps_factory=executor_deps_factory,
        )
    )
    pid = client.post("/api/projects", json={"name": "Factory Bounded URLs"}).json()[
        "id"
    ]
    urls = [
        "https://cdn.example/one.mp3",
        "https://cdn.example/two.mp3",
        "https://cdn.example/three.mp3",
    ]
    project = client.app.state.workspace.get(pid)
    before = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "rows", "cells", "blobs", "ops", "receipts")
    }

    legacy = client.post(f"/api/projects/{pid}/import/urls", json={"urls": urls})
    assert legacy.status_code == 400, legacy.text
    assert "limit" in legacy.json()["detail"].lower()

    canonical = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "import.urls",
            "scope": {"kind": "project"},
            "sheet_name": "downloads",
            "params": {
                "urls": urls,
            },
            "idempotency_key": "factory-import-urls@sha256:over-bound",
        },
    )
    assert canonical.status_code == 400, canonical.text
    assert canonical.json()["errors"][0]["code"] == "url_limit_exceeded"
    assert factory_paths == [
        f"/api/projects/{pid}/import/urls",
        f"/api/projects/{pid}/actions/v1/run",
    ]
    assert seen == []
    after = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in before
    }
    assert after == before


def test_import_urls_distinct_requests_allocate_names_and_retries_keep_original(
    tmp_path, monkeypatch
):
    seen = []

    def download(url):
        seen.append(url)
        return b"ID3fixture", "audio/mpeg", "episode.mp3", None

    monkeypatch.setattr(import_family, "download_url", download)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "URL allocation"}).json()["id"]
    route = f"/api/projects/{pid}/import/urls"
    first_body = {"urls": ["https://cdn.example/first.mp3"], "column": "Audio"}
    second_body = {"urls": ["https://cdn.example/second.mp3"], "column": "Audio"}
    first = client.post(route, json=first_body)
    second = client.post(route, json=second_body)
    assert first.status_code == second.status_code == 200
    project = client.app.state.workspace.get(pid)
    assert [
        row["name"] for row in project.db.execute("SELECT name FROM sheets ORDER BY id")
    ] == ["downloads", "downloads-2"]
    assert first.json()["sheet_id"] != second.json()["sheet_id"]
    assert client.post(route, json=first_body).json() == first.json()
    assert client.post(route, json=second_body).json() == second.json()
    assert seen == [*first_body["urls"], *second_body["urls"]]
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 2
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2
    for response in (first, second):
        assert project.row_count(response.json()["sheet_id"]) == 1
        assert "Audio" in {
            column["name"] for column in project.columns(response.json()["sheet_id"])
        }
