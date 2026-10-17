from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_blob_missing_digest_keeps_plain_404_detail(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Blob misses"}).json()["id"]

    for headers in ({}, {"Range": "bytes=0-15"}):
        response = client.get(
            f"/api/projects/{project_id}/blobs/missing-digest", headers=headers
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "no such blob"}


def test_blob_range_download_and_eof_close_materialization(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from fastapi import FastAPI
    from frisket.server.routes.project_blobs import register_project_blob_routes

    path = tmp_path / "original.txt"
    contents = ("a🙂漢\n" * 10000).encode()
    path.write_bytes(contents)
    close = Mock()
    service = SimpleNamespace(
        blob_download=lambda *_: SimpleNamespace(
            path=path, media_type="text/plain", close=close
        )
    )
    app = FastAPI()
    register_project_blob_routes(app, service=service)
    client = TestClient(app)
    url = "/api/projects/p/blobs/digest"
    response = client.get(url, headers={"Range": "bytes=16384-"})
    assert response.status_code == 206
    assert response.content == contents[16384:]
    assert (
        response.headers["content-range"]
        == f"bytes 16384-{len(contents) - 1}/{len(contents)}"
    )
    assert close.call_count == 1
    response = client.get(url, headers={"Range": "bytes=999999-"})
    assert response.status_code == 416
    assert close.call_count == 2
    path.write_bytes(b"")
    response = client.get(url, headers={"Range": "bytes=0-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */0"
    assert close.call_count == 3
    path.write_bytes(contents)
    assert client.get(url).content == contents
    assert close.call_count == 4
    response = client.get(url, headers={"Range": "bytes=0-15", "If-Range": '"old"'})
    assert response.status_code == 200
    assert response.content == contents
    assert client.get(url, headers={"Range": "bytes=-3"}).content == contents[-3:]
    assert close.call_count == 6


def test_blob_response_releases_materialization_when_transfer_is_cancelled(
    tmp_path: Path,
) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock

    import pytest
    from frisket.server.routes.project_blobs import _BlobResponse

    path = tmp_path / "original.txt"
    path.write_text("original", encoding="utf-8")
    close = Mock()
    response = _BlobResponse(
        SimpleNamespace(path=path, media_type="text/plain", close=close)
    )

    async def send(_message):
        raise asyncio.CancelledError()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            response({"type": "http", "method": "GET", "headers": []}, receive, send)
        )
    close.assert_called_once()


def test_bounded_blob_route_keeps_project_membership_and_skips_full_materialization(
    tmp_path: Path,
) -> None:
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import Mock

    import pytest
    from fastapi import FastAPI
    from frisket.engine.store.blob_backend import FilesystemProjectBlobStore
    from frisket.server.routes.project_blobs import register_project_blob_routes
    from frisket.server.services.project_blobs import (
        ProjectBlobService,
        ProjectBlobRouteError,
    )

    store = FilesystemProjectBlobStore(root=tmp_path / "blobs")
    contents = ("abc🙂\n" * 10000).encode()
    digest = store.put(contents)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE blobs (hash TEXT, mime TEXT, filename TEXT)")
    db.execute("INSERT INTO blobs VALUES (?, 'text/plain', 'notes.txt')", (digest,))
    project = SimpleNamespace(
        db=db,
        blob_store=store,
        materialize_blob=Mock(side_effect=AssertionError("full materialization")),
    )
    workspace = SimpleNamespace(get=Mock(return_value=project))
    service = ProjectBlobService(workspace)
    app = FastAPI()
    register_project_blob_routes(app, service=service)
    client = TestClient(app)
    url = f"/api/projects/allowed/blobs/{digest}"
    response = client.get(url, headers={"Range": "bytes=16384-32767"})
    assert response.status_code == 206
    assert response.content == contents[16384:32768]
    assert response.headers["content-range"] == f"bytes 16384-32767/{len(contents)}"
    assert "content-encoding" not in response.headers
    workspace.get.assert_called_with("allowed")
    assert client.get(url, headers={"Range": "bytes=999999-1000000"}).status_code == 416
    db.execute("DELETE FROM blobs")
    with pytest.raises(ProjectBlobRouteError, match="no such blob"):
        service.blob_range("allowed", digest, 0, 15)
    project.materialize_blob.assert_not_called()
    db.close()
