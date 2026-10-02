from __future__ import annotations

import errno
import io
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store import blob_backend, disk_capacity
from frisket.engine.store.blob_backend import (
    FilesystemProjectBlobStore,
    S3ProjectBlobStore,
)
from frisket.project_identity import ProjectStorageKey
from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk_types import BulkImportLimits, BulkUpload


def _usage(free: int) -> SimpleNamespace:
    return SimpleNamespace(total=free, used=0, free=free)


def test_headroom_is_current_free_space_plus_only_the_imminent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    margin = disk_capacity.DISK_HEADROOM_BYTES
    monkeypatch.setattr(
        disk_capacity.shutil, "disk_usage", lambda _path: _usage(margin + 3)
    )

    disk_capacity.require_disk_headroom(tmp_path, 3)
    with pytest.raises(OSError) as raised:
        disk_capacity.require_disk_headroom(tmp_path, 4)
    assert raised.value.errno == errno.ENOSPC


@pytest.mark.asyncio
async def test_bulk_staging_checks_each_chunk_without_double_counting_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = tmp_path / "draft"
    (draft / "files").mkdir(parents=True)
    margin = disk_capacity.DISK_HEADROOM_BYTES
    monkeypatch.setattr(import_bulk_sources, "CHUNK", 3)
    monkeypatch.setattr(
        disk_capacity.shutil, "disk_usage", lambda _path: _usage(margin + 3)
    )
    upload = BulkUpload(
        filename="six.bin",
        logical_path="six.bin",
        mime="application/octet-stream",
        file=UploadFile(filename="six.bin", file=io.BytesIO(b"123456")),
    )

    staged = await import_bulk_sources.stage_uploads(
        [upload], draft, False, BulkImportLimits()
    )

    assert staged[0]["size"] == 6
    assert list((draft / "files").iterdir())


@pytest.mark.asyncio
async def test_bulk_staging_headroom_failure_is_fatal_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = tmp_path / "draft"
    (draft / "files").mkdir(parents=True)
    monkeypatch.setattr(
        disk_capacity.shutil,
        "disk_usage",
        lambda _path: _usage(disk_capacity.DISK_HEADROOM_BYTES),
    )
    upload = BulkUpload(
        filename="payload.bin",
        logical_path="payload.bin",
        mime="application/octet-stream",
        file=UploadFile(filename="payload.bin", file=io.BytesIO(b"payload")),
    )

    with pytest.raises(OSError) as raised:
        await import_bulk_sources.stage_uploads(
            [upload], draft, False, BulkImportLimits()
        )

    assert raised.value.errno == errno.ENOSPC
    assert list(draft.iterdir()) == [draft / "files"]
    assert not list((draft / "files").iterdir())


@pytest.mark.asyncio
async def test_zip_expansion_checks_member_destination_and_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("document.txt", b"expanded bytes")
    archive_bytes = payload.getvalue()
    draft = tmp_path / "draft"
    (draft / "files").mkdir(parents=True)
    margin = disk_capacity.DISK_HEADROOM_BYTES

    def capacity(path: Path) -> SimpleNamespace:
        free = margin if Path(path) == draft / "files" else margin + len(archive_bytes)
        return _usage(free)

    monkeypatch.setattr(disk_capacity.shutil, "disk_usage", capacity)
    upload = BulkUpload(
        filename="archive.zip",
        logical_path="archive.zip",
        mime="application/zip",
        file=UploadFile(filename="archive.zip", file=io.BytesIO(archive_bytes)),
    )

    with pytest.raises(OSError) as raised:
        await import_bulk_sources.stage_uploads(
            [upload], draft, True, BulkImportLimits()
        )

    assert raised.value.errno == errno.ENOSPC
    assert list(draft.iterdir()) == [draft / "files"]
    assert not list((draft / "files").iterdir())


def test_filesystem_blob_copy_checks_destination_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source bytes")
    root = tmp_path / "blobs"
    store = FilesystemProjectBlobStore(root=root)
    seen = []

    def no_space(path: Path, size: int) -> None:
        seen.append((Path(path), size))
        raise OSError(errno.ENOSPC, "synthetic no space")

    monkeypatch.setattr(blob_backend, "require_disk_headroom", no_space)

    with pytest.raises(OSError) as raised:
        store.put_path(source)

    assert raised.value.errno == errno.ENOSPC
    assert seen == [(root, source.stat().st_size)]
    assert not list(root.glob(".blob-path.*"))


def test_s3_snapshot_checks_its_temporary_filesystem_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        def upload_fileobj(self, **_kwargs) -> None:
            raise AssertionError("capacity failure must precede upload")

    source = tmp_path / "source.bin"
    source.write_bytes(b"remote source")
    store = S3ProjectBlobStore(
        client=Client(),
        bucket="bucket",
        prefix="prefix",
        project_storage_key=ProjectStorageKey(storage_org_id=1, project_slug="project"),
    )
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen = []

    def no_space(path: Path, size: int) -> None:
        seen.append((Path(path), size))
        raise OSError(errno.ENOSPC, "synthetic no space")

    monkeypatch.setattr(blob_backend, "require_disk_headroom", no_space)

    with pytest.raises(OSError) as raised:
        store.put_path(source)

    assert raised.value.errno == errno.ENOSPC
    assert seen[0][0].parent == tmp_path
    assert seen[0][1] == source.stat().st_size
    assert not list(tmp_path.glob("frisket-blob-upload-*"))


def test_import_blob_stager_checks_scratch_and_removes_partial_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor import import_blob_stage

    seen = []

    def no_space(path: Path, size: int) -> None:
        seen.append((Path(path), size))
        raise OSError(errno.ENOSPC, "synthetic no space")

    monkeypatch.setattr(import_blob_stage, "require_disk_headroom", no_space)
    with AdmittedImportBlobStager(probe_metadata=False) as stager:
        scratch = Path(stager._directory.name)
        with pytest.raises(OSError) as raised:
            stager.stage(
                io.BytesIO(b"attachment"),
                filename="attachment.bin",
                mime="application/octet-stream",
            )
        assert raised.value.errno == errno.ENOSPC
        assert seen == [(scratch, len(b"attachment"))]
        assert not list(scratch.iterdir())
