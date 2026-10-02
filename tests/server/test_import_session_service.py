from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from frisket.engine.store import Project
from frisket.engine.store.import_intake import (
    import_intake_dir,
    import_worker_lock,
    read_import_header,
)
from frisket.server.services.import_bulk_types import (
    BulkImportLimits,
    BulkUpload,
    ImportBulkRouteError,
)
from frisket.server.services.import_sessions import ImportSessionService


def _upload(name: str, payload: bytes) -> BulkUpload:
    return BulkUpload(
        filename=name,
        logical_path=name,
        mime="application/octet-stream",
        file=UploadFile(file=io.BytesIO(payload), filename=name),
    )


@pytest.fixture
def project(tmp_path):
    with closing(Project.create(tmp_path / "project.frisket", name="Intake")) as value:
        yield value


@pytest.mark.asyncio
async def test_create_upload_retry_seal_uses_compact_header_and_canonical_blobs(
    project,
):
    enqueued = []

    async def enqueue(project_id, ref, through, sealed):
        enqueued.append((project_id, ref, through, sealed))

    service = ImportSessionService(
        SimpleNamespace(get=lambda _project_id: project), enqueue=enqueue
    )
    created = service.create("project-1", "Documents", max_rows=10)
    directory = import_intake_dir(project.path, created.import_ref)
    header = read_import_header(directory)
    assert header.project_id == "project-1"
    assert header.storage_identity == project.storage_identity
    assert header.max_rows == 10
    assert header.envelope["action"]["params"] == {"inventory_ref": created.import_ref}
    manifest = json.loads((directory / "manifest.json").read_text())
    assert set(manifest) == {
        "cancel_requested",
        "envelope",
        "max_bytes",
        "max_rows",
        "project_id",
        "storage_identity",
    }

    payload = b"canonical bytes"
    admitted = await service.upload(
        "project-1", created.import_ref, [_upload("one.bin", payload)], batch_id="b1"
    )
    assert (admitted.admitted_files, admitted.admitted_bytes, admitted.through) == (
        1,
        len(payload),
        1,
    )
    retried = await service.upload(
        "project-1",
        created.import_ref,
        [_upload("one.bin", payload)],
        batch_id="b1",
    )
    assert retried.admitted_files == 1
    digest = hashlib.sha256(payload).hexdigest()
    assert project.read_blob(digest) == payload
    sealed = await service.seal("project-1", created.import_ref)
    assert sealed.state == "admitting" and sealed.sealed
    assert enqueued == [
        ("project-1", created.import_ref, 1, False),
        ("project-1", created.import_ref, 1, False),
        ("project-1", created.import_ref, 1, True),
    ]
    assert not any(path.name.startswith(".batch-") for path in directory.iterdir())

    with pytest.raises(ValueError, match="different files"):
        await service.upload(
            "project-1",
            created.import_ref,
            [_upload("changed.bin", b"different")],
            batch_id="b1",
        )


@pytest.mark.asyncio
async def test_total_limits_apply_across_bounded_upload_chunks(project):
    service = ImportSessionService(
        SimpleNamespace(get=lambda _project_id: project),
        limits=BulkImportLimits(max_upload_files=2, max_upload_bytes=5),
    )
    created = service.create("project-1", "Limited", max_rows=20)
    assert (
        read_import_header(import_intake_dir(project.path, created.import_ref)).max_rows
        == 2
    )
    await service.upload(
        "project-1", created.import_ref, [_upload("one.bin", b"123")], batch_id="b1"
    )
    with pytest.raises(
        ImportBulkRouteError, match="upload bytes exceeds deployment limit"
    ):
        await service.upload(
            "project-1",
            created.import_ref,
            [_upload("two.bin", b"456")],
            batch_id="b2",
        )
    status = service.status("project-1", created.import_ref)
    assert (status.admitted_files, status.admitted_bytes) == (1, 3)


def test_cancel_sets_durable_intent_even_while_worker_lock_is_busy(project):
    service = ImportSessionService(SimpleNamespace(get=lambda _project_id: project))
    created = service.create("project-1", "Cancelled")
    directory = import_intake_dir(project.path, created.import_ref)
    with import_worker_lock(directory):
        status = service.cancel("project-1", created.import_ref)
    assert status.state == "cancelling" and status.cancel_requested
    assert read_import_header(directory).cancel_requested
