from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project
from frisket.engine.store import blob_backend
from frisket.engine.store.blob_backend import (
    BlobIntegrityError,
    FilesystemProjectBlobStore,
    S3ProjectBlobStore,
)
from frisket.engine.store.media_blobs import owned_media_metadata_document


class _MissingObject(Exception):
    def __init__(self) -> None:
        super().__init__("missing")
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class _StreamingS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads = 0
        self.extra_args: dict[str, Any] | None = None

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            payload = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _MissingObject from exc
        return {"Body": io.BytesIO(payload)}

    def upload_fileobj(
        self,
        *,
        Fileobj: Any,
        Bucket: str,
        Key: str,
        ExtraArgs: dict[str, Any],
    ) -> None:
        self.uploads += 1
        self.extra_args = ExtraArgs
        chunks: list[bytes] = []
        while chunk := Fileobj.read(64 * 1024):
            chunks.append(chunk)
        self.objects[(Bucket, Key)] = b"".join(chunks)


def test_directory_fsync_is_optional_on_unsupported_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_directory_open(*_args: Any, **_kwargs: Any) -> int:
        raise PermissionError("directories cannot be opened on this platform")

    monkeypatch.setattr(blob_backend.os, "open", reject_directory_open)
    blob_backend._fsync_directory(tmp_path)


def test_project_add_blob_from_path_streams_and_records_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "stream.frisket", name="stream")
    source = tmp_path / "large-enough-to-chunk.bin"
    payload = (b"0123456789abcdef" * (1024 * 128)) + b"tail"
    source.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    original_read_bytes = Path.read_bytes

    def reject_whole_file_read(path: Path) -> bytes:
        if path == source:
            raise AssertionError("path ingest read the whole source into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_read)
    try:
        digest = project.add_blob_from_path(
            source,
            filename="clip.mp4",
            mime="video/mp4",
            source_url="https://example.test/source",
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 2.5}
            ),
        )
        assert digest == expected
        row = project.db.execute(
            "SELECT filename, mime, size, source_url, metadata FROM blobs WHERE hash=?",
            (digest,),
        ).fetchone()
        assert row is not None
        assert {
            key: row[key] for key in ("filename", "mime", "size", "source_url")
        } == {
            "filename": "clip.mp4",
            "mime": "video/mp4",
            "size": len(payload),
            "source_url": "https://example.test/source",
        }
        assert json.loads(row["metadata"]) == owned_media_metadata_document(
            probe={"kind": "video", "duration_seconds": 2.5}
        )
        with project.materialize_blob(digest) as canonical:
            with Path(canonical).open("rb") as stored:
                assert hashlib.sha256(stored.read()).hexdigest() == digest
    finally:
        project.close()


def test_filesystem_path_ingest_is_atomic_and_fails_closed_on_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"canonical clip bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FilesystemProjectBlobStore(root=tmp_path / "blobs")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic atomic install failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic install failure"):
        store.put_path(source, expected_digest=digest)
    target = store.root / digest[:2] / digest
    assert not target.exists()
    assert not list(store.root.glob(".blob-path.*"))

    monkeypatch.undo()
    assert store.put_path(source, expected_digest=digest) == digest
    assert store.put_path(source, expected_digest=digest) == digest
    target.write_bytes(b"corrupt canonical bytes")
    with pytest.raises(BlobIntegrityError, match="canonical blob .* is corrupt"):
        store.put_path(source, expected_digest=digest)
    assert target.read_bytes() == b"corrupt canonical bytes"


def test_add_blob_from_path_commit_false_obeys_caller_transaction(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "transaction.frisket", name="transaction")
    source = tmp_path / "clip.bin"
    source.write_bytes(b"transactional metadata, durable canonical bytes")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        project.db.execute("BEGIN")
        digest = project.add_blob_from_path(
            source,
            filename="clip.bin",
            metadata={"renderer": "exact"},
            commit=False,
        )
        assert digest == expected
        assert project.db.in_transaction
        stored_metadata = project.db.execute(
            "SELECT metadata FROM blobs WHERE hash=?", (digest,)
        ).fetchone()["metadata"]
        assert json.loads(stored_metadata) == {"renderer": "exact"}
        project.db.rollback()
        assert (
            project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
            is None
        )
        with project.materialize_blob(digest) as canonical:
            assert Path(canonical).read_bytes() == source.read_bytes()
    finally:
        project.close()


def test_s3_path_ingest_streams_verifies_and_does_not_reupload_duplicate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "remote.bin"
    source.write_bytes(b"remote streaming payload" * 10_000)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    client = _StreamingS3Client()
    store = S3ProjectBlobStore(
        client=client,
        bucket="bucket",
        prefix="prefix",
        project_storage_key=ProjectStorageKey(
            storage_org_id=4,
            project_slug="path-ingest",
        ),
    )

    assert store.put_path(source, expected_digest=digest) == digest
    assert client.uploads == 1
    assert client.extra_args == {
        "Metadata": {"sha256": digest},
        "ChecksumAlgorithm": "SHA256",
    }
    assert store.put_path(source, expected_digest=digest) == digest
    assert client.uploads == 1

    remote_key = next(key for key in client.objects if key[1].endswith(digest))
    client.objects[remote_key] = b"corrupt remote bytes"
    with pytest.raises(BlobIntegrityError, match="canonical blob .* is corrupt"):
        store.put_path(source, expected_digest=digest)
    assert client.uploads == 1


def test_project_rejects_a_path_store_that_returns_the_wrong_digest(
    tmp_path: Path,
) -> None:
    class WrongDigestStore(FilesystemProjectBlobStore):
        def put_path(
            self,
            path: str | Path,
            *,
            expected_digest: str | None = None,
        ) -> str:
            del path, expected_digest
            return "0" * 64

    source = tmp_path / "source.bin"
    source.write_bytes(b"submitted bytes")
    project = Project.create(
        tmp_path / "wrong.frisket",
        name="wrong",
        blob_store=WrongDigestStore(root=tmp_path / "wrong-blobs"),
    )
    try:
        with pytest.raises(BlobIntegrityError, match="does not match"):
            project.add_blob_from_path(source)
        assert project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
    finally:
        project.close()
