from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from frisket.engine.store import blob_backend
from frisket.engine.store.blob_backend import (
    BlobStoreError,
    FilesystemProjectBlobStore,
    S3ProjectBlobStore,
)
from frisket.project_identity import ProjectStorageKey


def test_local_range_seeks_without_materializing_or_hashing_again(
    tmp_path: Path, monkeypatch
) -> None:
    store = FilesystemProjectBlobStore(root=tmp_path)
    contents = b"start" + b"x" * 1_000_000 + b"end"
    digest = store.put(contents)
    monkeypatch.setattr(
        blob_backend, "_sha256_path", Mock(side_effect=AssertionError("full hash"))
    )
    monkeypatch.setattr(
        store, "materialize", Mock(side_effect=AssertionError("materialize"))
    )
    part = store.read_range(digest, 900_000, 916_383)
    assert part.data == contents[900_000:916_384]
    assert (part.start, part.end, part.total) == (900_000, 916_383, len(contents))
    assert store.read_range(digest, len(contents), len(contents) + 1).data == b""
    with pytest.raises(ValueError):
        store.read_range(digest, 0, 65_536)


@pytest.mark.parametrize("fault", ["none", "ignored", "oversized"])
def test_s3_range_uses_scoped_key_and_bounded_read_then_closes(fault: str) -> None:
    contents = b"x" * 100_000
    body = io.BytesIO(contents if fault == "oversized" else contents[10:26])
    read = Mock(wraps=body.read)
    close = Mock(wraps=body.close)
    response = {
        "Body": SimpleNamespace(read=read, close=close),
        "ContentRange": "bytes 10-25/100000" if fault != "ignored" else None,
    }
    client = SimpleNamespace(
        head_object=Mock(return_value={"ContentLength": len(contents)}),
        get_object=Mock(return_value=response),
    )
    store = S3ProjectBlobStore(
        client=client,
        bucket="private",
        prefix="orgs",
        project_storage_key=ProjectStorageKey(storage_org_id=7, project_slug="project"),
    )
    digest = "a" * 64
    if fault == "ignored":
        with pytest.raises(BlobStoreError, match="invalid byte range"):
            store.read_range(digest, 10, 25)
        read.assert_not_called()
    elif fault == "oversized":
        with pytest.raises(BlobStoreError, match="invalid range length"):
            store.read_range(digest, 10, 25)
        read.assert_called_once_with(17)
    else:
        assert store.read_range(digest, 10, 25).data == contents[10:26]
        read.assert_called_once_with(17)
    close.assert_called_once()
    client.get_object.assert_called_once_with(
        Bucket="private", Key=f"orgs/7/project/sha256/{digest}", Range="bytes=10-25"
    )
    client.get_object.reset_mock()
    assert store.read_range(digest, 100_000, 100_001).total == 100_000
    client.get_object.assert_not_called()
