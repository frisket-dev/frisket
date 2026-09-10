from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MEDIA_PROBE_NAMESPACE,
    MediaBlobStore,
    media_cell,
    owned_media_metadata_document,
)


def test_media_blob_store_owns_metadata_and_typed_cells(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "media-blobs.frisket", name="Media Blobs")
    store = MediaBlobStore(project)

    digest = project.add_blob(
        b"fake image",
        filename="frame.jpg",
        mime="image/jpeg",
        metadata=owned_media_metadata_document(probe={"kind": "image", "width": 640}),
    )

    assert store.probe_metadata(digest) == {"kind": "image", "width": 640}
    store.replace_probe_metadata(digest, {"kind": "image", "height": 480})
    assert store.metadata(digest) == {
        MEDIA_PROBE_NAMESPACE: {"kind": "image", "height": 480}
    }

    cell = media_cell(digest, mime="image/jpeg", filename="frame.jpg")
    assert cell == {
        "blob": digest,
        "mime": "image/jpeg",
        "filename": "frame.jpg",
    }

    file_cell = store.file_cell(digest, mime="application/octet-stream")
    assert file_cell == {
        "blob": digest,
        "mime": "application/octet-stream",
    }
    assert store.validate_blob_cell(cell) == cell
    with pytest.raises(ValueError, match="requires a blob digest"):
        store.validate_blob_cell({"mime": "image/jpeg"})
