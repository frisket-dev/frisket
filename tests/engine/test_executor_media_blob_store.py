from __future__ import annotations


from frisket.engine.store.media_blobs import (
    MEDIA_PROBE_NAMESPACE,
    MediaBlobStore,
    media_cell,
    owned_media_metadata_document,
)


def test_media_blob_store_owns_metadata_and_typed_cells(make_project) -> None:
    project = make_project("Media Blobs")
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
