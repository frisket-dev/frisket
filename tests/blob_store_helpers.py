"""Test-only access to filesystem canonical paths for corruption probes."""

from __future__ import annotations

from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.blob_backend import FilesystemProjectBlobStore


def local_blob_path(project: Project, digest: str) -> Path:
    """Return a local path only when a test must remove canonical bytes."""

    store = project.blob_store
    if not isinstance(store, FilesystemProjectBlobStore):
        raise AssertionError("test requires the filesystem project blob backend")
    return store._path_for_digest(digest)
