from __future__ import annotations

import asyncio
import io
import struct
import zlib
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile
import pytest

from frisket.server.app import create_app
from frisket.server.services import import_files as files_service
from frisket.server.services.import_uploads import (
    AdmittedUpload,
    ImportUploadRouteError,
    admit_upload,
)


def _png_header(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    )
    iend = (
        struct.pack(">I", 0)
        + b"IEND"
        + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    )
    return signature + ihdr + iend


def test_upload_admission_stops_on_the_first_chunk_that_exceeds_its_limit() -> None:
    class BoundedReader:
        def __init__(self, data: bytes):
            self._source = io.BytesIO(data)
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 1024 * 1024
            self.read_sizes.append(size)
            return self._source.read(size)

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._source.seek(offset, whence)

        def tell(self) -> int:
            return self._source.tell()

    reader = BoundedReader(b"x" * (2 * 1024 * 1024))
    upload = UploadFile(file=reader, filename="large.txt", size=2 * 1024 * 1024)

    with pytest.raises(ImportUploadRouteError, match="upload bytes"):
        asyncio.run(admit_upload(upload, max_bytes=1))

    assert reader.read_sizes == [1024 * 1024]
    assert reader.tell() == 0


def test_import_files_route_preserves_transport_response_and_uses_typed_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requests = []
    execute = files_service.run_action_spec

    def record_request(project, request, **kwargs):
        requests.append(request)
        assert set(request) == {
            "action_id",
            "scope",
            "sheet_name",
            "params",
            "output_names",
            "idempotency_key",
        }
        assert request["action_id"] == "import.files"
        assert request["scope"] == {"kind": "project"}
        assert request["sheet_name"] == "assets"
        assert request["output_names"] == {}
        assert set(request["params"]) == {"files"}
        for source in request["params"]["files"]:
            assert set(source) == {"path", "filename", "mime"}
            admitted = kwargs["deps"].local_file_sources[source["path"]]
            assert not admitted.stream.closed
            assert admitted.sha256.startswith("sha256:")
        return execute(project, request, **kwargs)

    monkeypatch.setattr(files_service, "run_action_spec", record_request)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Files Upload"}).json()["id"]

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/files"
    ]["post"]
    assert operation.get("x-frisket-v1-transport") == {
        "state": "v1_product_transport",
        "transport": "multipart_upload",
        "action_kind": "import.files",
        "v1_task": "v1-http-import-files-upload-bridge",
        "canonical_action_route": "/api/projects/{pid}/actions/v1/run#import.files",
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportFilesResponse"
    }
    assert operation["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActionResult"
    }

    response = client.post(
        f"/api/projects/{pid}/import/files?sheet_name=assets",
        files=[
            ("files", ("card.png", _png_header(11, 7), "image/png")),
            ("files", ("notes.txt", b"source notes", "text/plain")),
        ],
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"sheet_id": 1, "rows": 2}
    project = client.app.state.workspace.get(pid)
    receipt = project.db.execute("SELECT action_kind FROM receipts").fetchone()
    assert receipt is not None
    assert receipt["action_kind"] == "import.files"
    replay = client.post(
        f"/api/projects/{pid}/import/files?sheet_name=assets",
        files=[
            ("files", ("card.png", _png_header(11, 7), "image/png")),
            ("files", ("notes.txt", b"source notes", "text/plain")),
        ],
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    assert requests[0] == requests[1]
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_held_file_upload_reads_bounded_original_handle_not_replaced_path(tmp_path):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Pinned files"}).json()["id"]
    raw = b"original source\n" * 100_000
    path = tmp_path / "original.txt"
    path.write_bytes(raw)
    reads = []

    class BoundedReader:
        def __init__(self, source):
            self.source = source

        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024
            reads.append(size)
            return self.source.read(size)

        def __getattr__(self, name):
            return getattr(self.source, name)

    with path.open("rb") as original:
        path.rename(tmp_path / "held-original.txt")
        path.write_bytes(b"replacement must never be imported")
        response = files_service.ImportFilesUploadService(
            app.state.workspace
        ).upload_files(
            pid,
            files=[
                AdmittedUpload(
                    filename="report.txt",
                    mime="text/plain",
                    source=BoundedReader(original),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size=len(raw),
                )
            ],
            sheet_name="Reports",
        )
        assert not original.closed
    assert response.status_code == 200, response.payload
    assert response.payload["rows"] == 1
    assert reads
    project = app.state.workspace.get(pid)
    columns = {
        row["name"]: row["id"] for row in project.columns(response.payload["sheet_id"])
    }
    values = project.get_values(response.payload["sheet_id"], columns["media"])
    (cell,) = values.values()
    assert cell["filename"] == "report.txt"
    assert project.read_blob(cell["blob"]) == raw


def test_files_upload_default_name_allocates_and_replays_but_explicit_name_refuses(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Files names"}).json()["id"]
    endpoint = f"/api/projects/{pid}/import/files"
    first = client.post(
        endpoint, files=[("files", ("notes.txt", b"first", "text/plain"))]
    )
    assert first.status_code == 200, first.text
    upload = [("files", ("notes.txt", b"second", "text/plain"))]
    allocated = client.post(endpoint, files=upload)
    assert allocated.status_code == 200, allocated.text
    project = client.app.state.workspace.get(pid)
    assert [sheet["name"] for sheet in project.sheets()] == ["files", "files-2"]

    replay = client.post(endpoint, files=upload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == allocated.json()
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2

    # Explicitly requesting either occupied name must not allocate or replay a
    # differently named default upload, even when the uploaded bytes match.
    for name in ("files", "files-2"):
        refused = client.post(endpoint, files=upload, params={"sheet_name": name})
        assert refused.status_code == 409, refused.text
        assert [error["code"] for error in refused.json()["errors"]] == [
            "duplicate_sheet_name"
        ]
    assert [sheet["name"] for sheet in project.sheets()] == ["files", "files-2"]
