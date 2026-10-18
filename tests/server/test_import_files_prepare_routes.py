from __future__ import annotations

import asyncio
import io
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from frisket.server.app import create_app
from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk import ImportBulkService
from frisket.server.services.import_bulk_types import BulkImportLimits, BulkUpload
from frisket.server.services.pdf_imports.service import PdfImportService


def _project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "Prepared files"})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _zip(members: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for path, content in members.items():
            archive.writestr(path, content)
    return payload.getvalue()


def _prepare(
    client: TestClient,
    project_id: str,
    uploads: list[tuple[str, bytes, str, str]],
    *,
    expand_archive: bool,
):
    parts: list[tuple[str, tuple[Any, ...]]] = []
    for filename, content, mime, logical_path in uploads:
        parts.extend(
            [
                ("files", (filename, content, mime)),
                ("logical_paths", (None, logical_path)),
            ]
        )
    parts.append(("expand_archive", (None, str(expand_archive).lower())))
    return client.post(f"/api/projects/{project_id}/import/files/prepare", files=parts)


def _private_directories(client: TestClient, project_id: str) -> list[Path]:
    root = client.app.state.workspace.get(project_id).path / ".bulk_import_staging"
    return (
        [] if not root.exists() else [path for path in root.iterdir() if path.is_dir()]
    )


def test_prepare_routes_direct_and_mixed_zip_pdf_selections_without_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted: list[dict[str, Any]] = []

    def adopt(
        service: PdfImportService,
        project_id: str,
        staging_dir: Path,
        entries: list[dict[str, Any]],
        *,
        source_label: str,
    ) -> dict[str, Any]:
        adopted.append(
            {
                "paths": [entry["logical_path"] for entry in entries],
                "contents": {
                    entry["logical_path"]: (staging_dir / entry["path"]).read_bytes()
                    for entry in entries
                },
                "stored_files": len(list((staging_dir / "files").iterdir())),
            }
        )
        status = service.create(
            project_id,
            expected_files=len(entries),
            expected_bytes=sum(int(entry["size"]) for entry in entries),
            source_label=source_label,
        )
        shutil.rmtree(staging_dir)
        return status

    monkeypatch.setattr(PdfImportService, "adopt_staged", adopt, raising=False)
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = _project(client)

    repeated_pdf = b"%PDF-1.4 same bytes"
    direct = _prepare(
        client,
        project_id,
        [
            ("a.pdf", repeated_pdf, "application/pdf", "folder/a.pdf"),
            ("b.pdf", repeated_pdf, "application/pdf", "folder/b.pdf"),
            ("note.txt", b"note", "text/plain", "folder/note.txt"),
        ],
        expand_archive=False,
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["kind"] == "pdf_import"
    assert adopted[0]["paths"] == ["folder/a.pdf", "folder/b.pdf", "folder/note.txt"]
    assert adopted[0]["stored_files"] == 2

    nested = _zip({"inside.pdf": b"nested PDF stays archived"})
    mixed = _prepare(
        client,
        project_id,
        [
            (
                "bundle.zip",
                _zip(
                    {
                        "docs/a.pdf": b"%PDF-a",
                        "docs/b.pdf": b"%PDF-b",
                        "docs/nested.zip": nested,
                    }
                ),
                "application/zip",
                "bundle.zip",
            ),
            ("sibling.txt", b"sibling", "text/plain", "sibling.txt"),
        ],
        expand_archive=True,
    )
    assert mixed.status_code == 200, mixed.text
    assert mixed.json()["kind"] == "pdf_import"
    assert adopted[1]["paths"] == [
        "docs/a.pdf",
        "docs/b.pdf",
        "docs/nested.zip",
        "sibling.txt",
    ]
    assert adopted[1]["contents"]["docs/nested.zip"] == nested
    assert adopted[1]["contents"]["sibling.txt"] == b"sibling"
    assert _private_directories(client, project_id) == []


def test_prepare_with_fewer_than_two_pdfs_publishes_existing_bulk_plan(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = _project(client)

    response = _prepare(
        client,
        project_id,
        [
            (
                "bundle.zip",
                _zip({"one.pdf": b"%PDF-one", "rows.csv": b"name\nAda\n"}),
                "application/zip",
                "bundle.zip",
            ),
            ("notes.txt", b"notes", "text/plain", "notes.txt"),
        ],
        expand_archive=True,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "bulk"
    assert body["plan"]["message"] == "Ready to import 3 files."
    assert len(_private_directories(client, project_id)) == 1


@pytest.mark.parametrize(
    ("limits", "uploads", "expected_status"),
    [
        (
            BulkImportLimits(max_archive_members=1),
            [
                ("a.zip", _zip({"a.pdf": b"a"}), "application/zip", "a.zip"),
                ("b.zip", _zip({"b.pdf": b"b"}), "application/zip", "b.zip"),
            ],
            413,
        ),
        (
            BulkImportLimits(),
            [
                (
                    "bundle.zip",
                    _zip({"same.pdf": b"archive"}),
                    "application/zip",
                    "bundle.zip",
                ),
                ("same.pdf", b"direct", "application/pdf", "same.pdf"),
            ],
            400,
        ),
    ],
)
def test_prepare_rejects_aggregate_archive_limits_and_path_collisions_atomically(
    tmp_path: Path,
    limits: BulkImportLimits,
    uploads: list[tuple[str, bytes, str, str]],
    expected_status: int,
) -> None:
    client = TestClient(
        create_app(tmp_path / "workspace", bulk_import_limits=limits),
        raise_server_exceptions=False,
    )
    project_id = _project(client)

    response = _prepare(client, project_id, uploads, expand_archive=True)

    assert response.status_code == expected_status
    assert _private_directories(client, project_id) == []


def test_prepare_cancellation_cleans_private_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    project_id = _project(client)

    async def cancelled(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise asyncio.CancelledError

    monkeypatch.setattr(import_bulk_sources, "stage_uploads", cancelled)
    service = ImportBulkService(app.state.workspace)
    upload = BulkUpload(
        filename="a.pdf",
        logical_path="a.pdf",
        mime="application/pdf",
        file=UploadFile(io.BytesIO(b"%PDF-a"), filename="a.pdf"),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.prepare(project_id, uploads=[upload], expand_archive=False))

    assert _private_directories(client, project_id) == []
