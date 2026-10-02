from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from frisket.engine.store import Project
from frisket.engine.store.cells import add_sheet
from frisket.engine.store.import_intake import (
    import_intake_dir,
    import_worker_lock,
    read_import_header,
)
from frisket.engine.store.import_inventory import ImportInventory
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
        "resolution",
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


@pytest.mark.asyncio
async def test_upload_failure_before_canonical_admission_is_not_acknowledged(
    project, monkeypatch
):
    service = ImportSessionService(SimpleNamespace(get=lambda _project_id: project))
    created = service.create("project-1", "Failure")
    directory = import_intake_dir(project.path, created.import_ref)

    def fail_admission(*_args, **_kwargs):
        raise OSError("canonical admission failed")

    monkeypatch.setattr(project.blob_store, "put_path", fail_admission)
    with pytest.raises(OSError, match="canonical admission failed"):
        await service.upload(
            "project-1",
            created.import_ref,
            [_upload("one.bin", b"payload")],
            batch_id="failed",
        )

    status = service.status("project-1", created.import_ref)
    assert (status.admitted_files, status.admitted_bytes, status.through) == (0, 0, 0)
    assert not any(path.name.startswith(".batch-") for path in directory.iterdir())

    monkeypatch.undo()
    retried = await service.upload(
        "project-1",
        created.import_ref,
        [_upload("one.bin", b"payload")],
        batch_id="failed",
    )
    assert (retried.admitted_files, retried.through) == (1, 1)


def test_cancel_sets_durable_intent_even_while_worker_lock_is_busy(project):
    service = ImportSessionService(SimpleNamespace(get=lambda _project_id: project))
    created = service.create("project-1", "Cancelled")
    directory = import_intake_dir(project.path, created.import_ref)
    with import_worker_lock(directory):
        status = service.cancel("project-1", created.import_ref)
    assert status.state == "cancelling" and status.cancel_requested
    assert read_import_header(directory).cancel_requested


def test_create_allocates_around_existing_sheet_and_admitted_sessions(project):
    add_sheet(project, "files")
    service = ImportSessionService(SimpleNamespace(get=lambda _project_id: project))

    first = service.create("project-1", "files")
    second = service.create("project-1", "files")

    assert first.sheet_name == "files-2"
    assert second.sheet_name == "files-3"
    assert (
        read_import_header(import_intake_dir(project.path, second.import_ref)).envelope[
            "action"
        ]["sheet_name"]
        == "files-3"
    )


def test_failure_before_first_page_is_paused_until_latest_retry_is_queued(project):
    class Queue:
        status = "failed"

        def find_job_by_refs(self, kind, **kwargs):
            assert kwargs["statuses"] == (
                "queued",
                "running",
                "failed",
                "done",
                "cancelled",
            )
            assert kwargs["dedupe_key"].endswith(":0:0")
            return SimpleNamespace(status=self.status)

    queue = Queue()
    workspace = SimpleNamespace(
        get=lambda _project_id: project,
        queue=queue,
        queue_storage_org_id=None,
    )
    service = ImportSessionService(workspace)
    created = service.create("project-1", "files")

    failed = service.status("project-1", created.import_ref)
    assert failed.state == "paused"
    assert failed.error == "Import processing failed. Retry the import to continue."

    queue.status = "queued"
    retried = service.status("project-1", created.import_ref)
    assert retried.state == "admitting"
    assert retried.error is None


def test_early_cancel_and_resolution_are_durable_and_idempotent(project):
    service = ImportSessionService(SimpleNamespace(get=lambda _project_id: project))
    created = service.create("project-1", "Never started")
    directory = import_intake_dir(project.path, created.import_ref)
    with ImportInventory(directory / "inventory.db") as inventory:
        inventory.append(
            [
                {
                    "logical_path": "unpublished.bin",
                    "mime": "application/octet-stream",
                    "sha256": "0" * 64,
                    "size": 7,
                    "kind": "files",
                }
            ]
        )
    cancelled = service.cancel("project-1", created.import_ref)
    assert cancelled.state == "cancelled" and cancelled.sheet_id is None
    with ImportInventory(directory / "inventory.db") as inventory:
        assert len(inventory.page(limit=1)) == 1
    receipt_id = read_import_header(
        import_intake_dir(project.path, created.import_ref)
    ).envelope["receipt_id"]
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()["status"]
        == "cancelled"
    )

    kept = service.resolve("project-1", created.import_ref, decision="keep")
    assert kept.state == "kept" and kept.sheet_id is None
    assert (kept.admitted_files, kept.admitted_bytes, kept.through) == (1, 7, 1)
    with ImportInventory(directory / "inventory.db") as inventory:
        assert inventory.sealed and inventory.page(limit=1) == []
    assert (
        service.resolve("project-1", created.import_ref, decision="keep").state
        == "kept"
    )
    with pytest.raises(ValueError, match="differently"):
        service.resolve("project-1", created.import_ref, decision="remove")
    assert service.list("project-1").sessions == []


def test_terminal_resolution_survives_inventory_compaction_failure(
    project, monkeypatch
):
    from frisket.server.services import import_sessions as service_module

    service = ImportSessionService(SimpleNamespace(get=lambda _project_id: project))
    created = service.create("project-1", "Failure is nonfatal")
    assert service.cancel("project-1", created.import_ref).state == "cancelled"

    def fail_compaction(_directory):
        raise OSError("synthetic compaction failure")

    monkeypatch.setattr(service_module, "compact_terminal_inventory", fail_compaction)
    assert (
        service.resolve("project-1", created.import_ref, decision="remove").state
        == "removed"
    )


@pytest.mark.asyncio
async def test_multi_file_decoded_batch_cap_allows_one_oversized_file(
    project, monkeypatch
):
    from frisket.server.services import import_sessions as service_module

    monkeypatch.setattr(service_module, "_MULTI_FILE_BATCH_BYTES", 5)
    service = ImportSessionService(
        SimpleNamespace(get=lambda _project_id: project),
        limits=BulkImportLimits(max_upload_bytes=10),
    )
    multi = service.create("project-1", "Multi")
    with pytest.raises(ImportBulkRouteError, match="upload bytes"):
        await service.upload(
            "project-1",
            multi.import_ref,
            [_upload("one.bin", b"123"), _upload("two.bin", b"456")],
            batch_id="multi",
        )
    single = service.create("project-1", "Single")
    status = await service.upload(
        "project-1",
        single.import_ref,
        [_upload("large.bin", b"123456")],
        batch_id="single",
    )
    assert status.admitted_bytes == 6
