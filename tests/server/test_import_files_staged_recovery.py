from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from frisket.engine.jobs.import_files import _ensure_staged_blob
from frisket.engine.store.blob_backend import FilesystemProjectBlobStore


def test_missing_staged_digest_reuses_verified_canonical_blob(tmp_path: Path) -> None:
    payload = b"duplicate PDF bytes"
    digest = hashlib.sha256(payload).hexdigest()
    staged_root = tmp_path / "intake" / "staged"
    staged_root.mkdir(parents=True)
    staged = staged_root / digest
    staged.write_bytes(payload)
    store = FilesystemProjectBlobStore(root=tmp_path / "blobs")
    assert store.put_path(staged, expected_digest=digest) == digest
    staged.unlink()
    project = SimpleNamespace(blob_store=store)

    # A later occurrence with the same digest and a retry of that page both
    # succeed after bootstrap has consumed the one content-addressed staged file.
    _ensure_staged_blob(project, tmp_path / "intake", digest)
    _ensure_staged_blob(project, tmp_path / "intake", digest)
