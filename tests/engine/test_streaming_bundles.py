"""Streaming bundles retain atomic publication without corpus-sized buffers."""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from frisket.engine.store import Project


@pytest.mark.parametrize("legacy_zip_restore", [True, False])
def test_legacy_inventory_does_not_poison_new_backup(tmp_path, legacy_zip_restore):
    source = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        manifest = json.loads((source.path / "manifest.json").read_text())
        manifest.update(
            blobs=["a" * 64] * 18000, include_media=True, include_traces=False
        )
        assert len(json.dumps(manifest)) > 1024 * 1024
        if legacy_zip_restore:
            database = source.export_database(tmp_path / "source.db")
            archive = tmp_path / "legacy.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("manifest.json", json.dumps(manifest))
                output.write(database, "project.db")
            project = Project.import_bundle(archive, tmp_path / "legacy.frisket")
        else:
            # Existing installations may already have imported the legacy manifest.
            (source.path / "manifest.json").write_text(json.dumps(manifest))
            project = source
        try:
            before = (project.path / "manifest.json").read_text()
            backup = project.export(tmp_path / "new.tar.gz")
            restored = Project.import_bundle(backup, tmp_path / "restored.frisket")
            try:
                clean = json.loads((restored.path / "manifest.json").read_text())
                assert not {"blobs", "include_media", "include_traces"} & clean.keys()
                assert clean["name"] == "Source"
            finally:
                restored.close()
            assert (project.path / "manifest.json").read_text() == before
            if legacy_zip_restore:
                assert (
                    not {"blobs", "include_media", "include_traces"}
                    & json.loads(before).keys()
                )
        finally:
            if project is not source:
                project.close()
    finally:
        source.close()


def _rewrite(source, target, *, omit=None, extra=None):
    with tarfile.open(source) as original, tarfile.open(target, "w:gz") as output:
        for info in original:
            if info.name != omit:
                output.addfile(info, original.extractfile(info))
        if extra is not None:
            output.addfile(extra, io.BytesIO(b"x" * extra.size))


def test_many_objects_roundtrip_without_member_cache_or_whole_blob_reads(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        for index in range(300):
            project.add_blob(f"object {index}".encode())
        original_manifest = (project.path / "manifest.json").read_text()
        addfile = tarfile.TarFile.addfile
        next_member = tarfile.TarFile.next

        def bounded_addfile(self, *args, **kwargs):
            assert len(self.members) <= 1
            return addfile(self, *args, **kwargs)

        def bounded_next(self):
            assert len(self.members) <= 1
            return next_member(self)

        monkeypatch.setattr(tarfile.TarFile, "addfile", bounded_addfile)
        monkeypatch.setattr(tarfile.TarFile, "next", bounded_next)
        archive = project.export(tmp_path / "backup.tar.gz")
        with monkeypatch.context() as context:

            def no_read_bytes(self):
                raise AssertionError("restore must stream blob hashing")

            context.setattr(Path, "read_bytes", no_read_bytes)
            restored = Project.import_bundle(archive, tmp_path / "restored.frisket")
        try:
            assert (
                restored.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 300
            )
        finally:
            restored.close()
        # Inspect sequentially too: getmembers would violate the bounded cache assertion.
        with tarfile.open(archive, "r|gz") as stream:
            assert stream.next().name == "bundle.json"
            stream.members.clear()
            info = stream.next()
            assert info.name == "manifest.json"
            assert stream.extractfile(info).read().decode() == original_manifest
    finally:
        project.close()


@pytest.mark.parametrize("include_media", [True, False])
def test_required_media_missing_is_rejected_unless_omitted(tmp_path, include_media):
    project = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        digest = project.add_blob(b"hello")
        archive = project.export(
            tmp_path / "backup.tar.gz", include_media=include_media
        )
    finally:
        project.close()
    missing = tmp_path / "missing.tar.gz"
    _rewrite(archive, missing, omit=f"blobs/{digest[:2]}/{digest}")
    target = tmp_path / "restored.frisket"
    if include_media:
        with pytest.raises(ValueError, match="missing required blob"):
            Project.import_bundle(missing, target)
        assert not target.exists()
        assert not list(tmp_path.glob(".restored.frisket.import-*"))
    else:
        Project.import_bundle(missing, target).close()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/escape", tarfile.REGTYPE),
        ("manifest.json", tarfile.REGTYPE),
        ("traces/run-link.jsonl.gz", tarfile.SYMTYPE),
        ("traces/run-device.jsonl.gz", tarfile.CHRTYPE),
    ],
)
def test_unsafe_or_duplicate_member_never_publishes(tmp_path, name, kind):
    project = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        archive = project.export(tmp_path / "backup.tar.gz")
    finally:
        project.close()
    extra = tarfile.TarInfo(name)
    extra.type = kind
    extra.linkname = "manifest.json" if kind == tarfile.SYMTYPE else ""
    damaged = tmp_path / "damaged.tar.gz"
    _rewrite(archive, damaged, extra=extra)
    target = tmp_path / "restored.frisket"
    with pytest.raises((ValueError, FileExistsError)):
        Project.import_bundle(damaged, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.frisket.import-*"))


def test_failed_export_preserves_destination_and_cleans_temporary_files(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "source.frisket", name="Source")
    target = tmp_path / "backup.tar.gz"
    target.write_bytes(b"existing backup")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "addfile", fail)
    try:
        with pytest.raises(OSError, match="disk full"):
            project.export(target)
        assert target.read_bytes() == b"existing backup"
        assert not list(tmp_path.glob(".backup.tar.gz.*"))
    finally:
        project.close()


def test_large_tar_metadata_is_rejected_before_loading_json(tmp_path):
    archive = tmp_path / "oversized.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("manifest.json")
        info.size = 1024 * 1024 + 1
        output.addfile(info, io.BytesIO(b" " * info.size))
    target = tmp_path / "restored.frisket"
    with pytest.raises(ValueError, match="metadata exceeds"):
        Project.import_bundle(archive, target)
    assert not target.exists()


def test_export_refuses_oversized_project_metadata_without_replacing_backup(tmp_path):
    project = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        path = project.path / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["description"] = "x" * (1024 * 1024)
        path.write_text(json.dumps(manifest))
        target = tmp_path / "backup.tar.gz"
        target.write_bytes(b"previous backup")
        with pytest.raises(ValueError, match="metadata exceeds"):
            project.export(target)
        assert target.read_bytes() == b"previous backup"
    finally:
        project.close()


@pytest.mark.parametrize("legacy_zip", [True, False])
def test_restore_headroom_refusal_never_publishes(tmp_path, monkeypatch, legacy_zip):
    import errno
    from frisket.engine.store import bundle_io

    project = Project.create(tmp_path / "source.frisket", name="Source")
    try:
        archive = project.export(tmp_path / "backup.tar.gz")
    finally:
        project.close()
    if legacy_zip:
        converted = tmp_path / "legacy.zip"
        with tarfile.open(archive) as source, zipfile.ZipFile(converted, "w") as output:
            for info in source:
                output.writestr(info.name, source.extractfile(info).read())
        archive = converted
    writes = []

    def refuse(destination, size):
        writes.append((destination, size))
        raise OSError(errno.ENOSPC, "insufficient disk space")

    monkeypatch.setattr(bundle_io, "require_disk_headroom", refuse)
    target = tmp_path / "restored.frisket"
    with pytest.raises(OSError) as error:
        Project.import_bundle(archive, target)
    assert error.value.errno == errno.ENOSPC
    assert writes and writes[0][1] > 0
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.frisket.import-*"))
