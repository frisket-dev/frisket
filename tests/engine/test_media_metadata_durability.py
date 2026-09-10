from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pytest

import frisket.engine.store.blob_metadata as blob_metadata_module
from frisket.engine.store.media_blobs import update_blob_metadata
from frisket.ops.media_metadata import (
    MEDIA_METADATA_CACHE_SCHEMA_VERSION,
    MEDIA_METADATA_EXTRACTOR_VERSION,
)
from frisket.engine.store import Project
from frisket.engine.store.blob_metadata import (
    BLOB_METADATA_MAX_JSON_BYTES,
    BLOB_METADATA_MAX_JSON_DEPTH,
    BlobMetadataDocumentStore,
    canonical_blob_metadata_json,
)
from frisket.engine.store.media_blobs import (
    MEDIA_ACQUISITION_NAMESPACE,
    MEDIA_METADATA_CACHE_NAMESPACE,
    MEDIA_PROBE_NAMESPACE,
    MediaBlobStore,
    owned_media_metadata_document,
)


def _cache_proposal(
    digest: str,
    size_bytes: int,
    *,
    completeness: str = "complete",
    retryable: bool = False,
) -> dict[str, Any]:
    qualified_digest = f"sha256:{digest}"
    facts = {
        "envelope": {
            "normalized": {
                "blob_hash": qualified_digest,
                "size_bytes": size_bytes,
            },
            "sources": {
                "blob": {
                    "blob_hash": qualified_digest,
                    "size_bytes": size_bytes,
                }
            },
            "extraction": {
                "completeness": completeness,
                "retryable": retryable,
            },
        }
    }
    return {
        "cache_schema_version": MEDIA_METADATA_CACHE_SCHEMA_VERSION,
        "generation": 0,
        "extractor_version": MEDIA_METADATA_EXTRACTOR_VERSION,
        "content_facts_hash": "sha256:"
        + hashlib.sha256(
            canonical_blob_metadata_json(facts).encode("utf-8")
        ).hexdigest(),
        "content_facts": facts,
    }


def _rehash_cache_proposal(proposal: dict[str, Any]) -> None:
    encoded = canonical_blob_metadata_json(proposal["content_facts"]).encode("utf-8")
    proposal["content_facts_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_cache_rejects_forged_content_hash(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "forged-cache.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")
        forged = _cache_proposal(digest, len(b"blob"))
        forged["content_facts_hash"] = "sha256:" + ("f" * 64)
        with pytest.raises(ValueError, match="does not match content_facts"):
            MediaBlobStore(project).compare_and_swap_media_metadata_cache(
                digest, forged, expected_generation=0
            )
        assert MediaBlobStore(project).media_metadata_cache(digest) is None
    finally:
        project.close()


def test_nonreusable_cache_outcomes_are_not_persisted(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "nonreusable-cache.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")
        result = MediaBlobStore(project).compare_and_swap_media_metadata_cache(
            digest,
            _cache_proposal(
                digest,
                len(b"blob"),
                completeness="retryable_failure",
                retryable=True,
            ),
            expected_generation=0,
        )
        assert result.written is False
        assert result.reason == "not_reusable"
        assert MediaBlobStore(project).media_metadata_cache(digest) is None
    finally:
        project.close()


def test_terminal_unsupported_cache_outcome_is_persisted(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "unsupported-cache.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")
        result = MediaBlobStore(project).compare_and_swap_media_metadata_cache(
            digest,
            _cache_proposal(
                digest,
                len(b"blob"),
                completeness="terminal_unsupported",
            ),
            expected_generation=0,
        )
        assert result.written is True
        assert result.entry is not None
        assert result.entry["generation"] == 1
    finally:
        project.close()


def test_invalid_current_cache_is_replaceable_without_clobbering_document(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "invalid-cache.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob", metadata={"source_id": "kept"})
        project.db.execute(
            "UPDATE blobs SET metadata=? WHERE hash=?",
            (
                json.dumps(
                    {
                        "source_id": "kept",
                        MEDIA_METADATA_CACHE_NAMESPACE: {
                            "generation": 999,
                            "completeness": "complete",
                            "retryable": False,
                        },
                    }
                ),
                digest,
            ),
        )
        project.db.commit()

        assert MediaBlobStore(project).media_metadata_cache(digest) is None
        result = MediaBlobStore(project).compare_and_swap_media_metadata_cache(
            digest, _cache_proposal(digest, len(b"blob")), expected_generation=0
        )

        assert result.written is True
        assert result.entry is not None and result.entry["generation"] == 1
        assert MediaBlobStore(project).metadata(digest)["source_id"] == "kept"
    finally:
        project.close()


def test_cache_namespace_is_bound_to_its_blob_digest_and_stored_size(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "cache-blob-binding.frisket", name="metadata")
    try:
        first_payload = b"first-blob"
        second_payload = b"second-blob-is-larger"
        first_digest = project.add_blob(first_payload)
        second_digest = project.add_blob(second_payload)
        store = MediaBlobStore(project)
        first = store.compare_and_swap_media_metadata_cache(
            first_digest,
            _cache_proposal(first_digest, len(first_payload)),
            expected_generation=0,
        )
        assert first.written is True
        assert first.entry is not None

        copied = copy.deepcopy(first.entry)
        project.db.execute(
            "UPDATE blobs SET metadata=? WHERE hash=?",
            (
                json.dumps({MEDIA_METADATA_CACHE_NAMESPACE: copied}),
                second_digest,
            ),
        )
        project.db.commit()

        # A structurally valid, content-hashed namespace is still a miss when
        # its embedded blob identity does not match the row that contains it.
        assert store.media_metadata_cache(first_digest) is not None
        assert store.media_metadata_cache(second_digest) is None
        with pytest.raises(ValueError, match="target blob digest and size"):
            store.compare_and_swap_media_metadata_cache(
                second_digest, copied, expected_generation=0
            )
        with pytest.raises(ValueError, match="target blob digest and size"):
            store.compare_and_swap_media_metadata_cache(
                second_digest,
                _cache_proposal(second_digest, len(first_payload)),
                expected_generation=0,
            )
        with pytest.raises(ValueError, match="target blob digest and size"):
            store.compare_and_swap_media_metadata_cache(
                second_digest,
                _cache_proposal(first_digest, len(second_payload)),
                expected_generation=0,
            )

        replacement = store.compare_and_swap_media_metadata_cache(
            second_digest,
            _cache_proposal(second_digest, len(second_payload)),
            expected_generation=0,
        )
        assert replacement.written is True
        assert replacement.entry is not None
        assert replacement.entry["generation"] == 1
        assert store.media_metadata_cache(second_digest) == replacement.entry
    finally:
        project.close()


def test_cache_only_blob_still_receives_owned_probe_metadata(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "cache-only-backfill.frisket", name="metadata")
    try:
        digest = project.add_blob(
            b"one two\nthree\n", filename="notes.txt", mime="text/plain"
        )
        cached = MediaBlobStore(project).compare_and_swap_media_metadata_cache(
            digest,
            _cache_proposal(digest, len(b"one two\nthree\n")),
            expected_generation=0,
        )
        assert cached.written is True
        assert digest in MediaBlobStore(project).hashes_needing_metadata()

        probe = update_blob_metadata(project, digest)

        assert probe["kind"] == "text"
        document = MediaBlobStore(project).metadata(digest)
        assert document[MEDIA_PROBE_NAMESPACE]["kind"] == "text"
        assert document[MEDIA_METADATA_CACHE_NAMESPACE]["generation"] == 1
        assert digest not in MediaBlobStore(project).hashes_needing_metadata()
    finally:
        project.close()


def test_hashes_needing_metadata_uses_probe_namespace_presence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "malformed-backfill.frisket", name="metadata")
    try:
        malformed = project.add_blob(b"malformed")
        populated = project.add_blob(
            b"populated",
            metadata=owned_media_metadata_document(probe={"kind": "file"}),
        )
        acquisition_only = project.add_blob(
            b"acquisition",
            metadata=owned_media_metadata_document(
                acquisition={"title": "not a probe"}
            ),
        )
        project.db.execute(
            "UPDATE blobs SET metadata=? WHERE hash=?",
            ('{"unterminated"', malformed),
        )
        project.db.commit()

        needing = MediaBlobStore(project).hashes_needing_metadata()

        assert malformed in needing
        assert acquisition_only in needing
        assert populated not in needing
    finally:
        project.close()


def test_cache_cas_requires_an_explicit_expected_generation(tmp_path: Path) -> None:
    """The guard cannot be turned off by forgetting it.

    ``expected_generation`` used to default to ``None``, and ``None`` skipped
    the mismatch check even when a current entry existed -- omitting the
    argument silently downgraded a compare-and-swap into an unconditional
    overwrite. It is required now; "I have no generation" is spelled ``0``,
    the insert-only expectation, which refuses once an entry exists.
    """
    project = Project.create(tmp_path / "cas-required.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")
        store = MediaBlobStore(project)
        proposal = _cache_proposal(digest, len(b"blob"))

        with pytest.raises(TypeError, match="expected_generation"):
            store.compare_and_swap_media_metadata_cache(digest, proposal)

        # insert-only against an EMPTY namespace writes...
        inserted = store.compare_and_swap_media_metadata_cache(
            digest, proposal, expected_generation=0
        )
        assert inserted.written is True
        assert inserted.entry is not None and inserted.entry["generation"] == 1

        # ...and against an OCCUPIED one refuses, leaving the winner in place.
        blocked = store.compare_and_swap_media_metadata_cache(
            digest, proposal, expected_generation=0
        )
        assert blocked.written is False
        assert blocked.reason == "generation_mismatch"
        assert store.media_metadata_cache(digest)["generation"] == 1
    finally:
        project.close()


def test_cache_generation_guards_racing_replacements(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "cache-generation.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")
        store = MediaBlobStore(project)
        proposal = _cache_proposal(digest, len(b"blob"))
        first = store.compare_and_swap_media_metadata_cache(
            digest, proposal, expected_generation=0
        )
        assert first.entry is not None and first.entry["generation"] == 1

        stale = store.compare_and_swap_media_metadata_cache(
            digest,
            proposal,
            expected_generation=0,
        )
        assert stale.written is False
        assert stale.reason == "generation_mismatch"

        second = store.compare_and_swap_media_metadata_cache(
            digest,
            proposal,
            expected_generation=1,
        )
        assert second.entry is not None and second.entry["generation"] == 2
    finally:
        project.close()


def test_cache_completeness_and_retryable_must_agree(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "cache-state.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")
        proposal = _cache_proposal(digest, len(b"blob"))
        proposal["content_facts"]["envelope"]["extraction"]["retryable"] = True
        _rehash_cache_proposal(proposal)
        with pytest.raises(ValueError, match="completeness and retryable disagree"):
            MediaBlobStore(project).compare_and_swap_media_metadata_cache(
                digest, proposal, expected_generation=0
            )
    finally:
        project.close()


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps("x" * BLOB_METADATA_MAX_JSON_BYTES),
        ('{"x":' * (BLOB_METADATA_MAX_JSON_DEPTH + 1))
        + "0"
        + ("}" * (BLOB_METADATA_MAX_JSON_DEPTH + 1)),
    ],
)
def test_blob_metadata_decode_rejects_oversized_and_deep_json(
    tmp_path: Path, raw: str
) -> None:
    project = Project.create(
        tmp_path / "hostile-blob-metadata.frisket", name="metadata"
    )
    try:
        digest = project.add_blob(b"blob")
        project.db.execute("UPDATE blobs SET metadata=? WHERE hash=?", (raw, digest))
        project.db.commit()
        with pytest.raises(ValueError, match="limit"):
            BlobMetadataDocumentStore(project).document(digest)
        assert MediaBlobStore(project).media_metadata_cache(digest) is None
    finally:
        project.close()


def test_blob_metadata_decode_translates_recursion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "recursive-decode.frisket", name="metadata")
    try:
        digest = project.add_blob(b"blob")

        def recurse(_value: Any) -> Any:
            raise RecursionError("fixture")

        monkeypatch.setattr(blob_metadata_module.json, "loads", recurse)
        with pytest.raises(ValueError, match="not valid JSON"):
            BlobMetadataDocumentStore(project).document(digest)
    finally:
        project.close()


@pytest.mark.realtime
def test_probe_and_cache_cas_cannot_lose_each_others_namespaces(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "metadata-cas.frisket", name="metadata")
    locked_read = threading.Event()
    release_probe_writer = threading.Event()
    cache_writer_started = threading.Event()
    digest = project.add_blob(
        b"blob",
        filename="asset.bin",
        metadata=owned_media_metadata_document(
            acquisition={"title": "acquired"}, owner={"source_id": "source-1"}
        ),
    )

    class PausingProbeStore(MediaBlobStore):
        @staticmethod
        def _pause_after_read() -> None:
            locked_read.set()
            if not release_probe_writer.wait(timeout=5):
                raise TimeoutError("test did not release probe metadata writer")

        def transactionally_update_document(
            self,
            digest: str,
            update: Callable[[dict[str, Any]], dict[str, Any]],
            *,
            commit: bool = True,
        ) -> dict[str, Any]:
            def pause_after_locked_read(document: dict[str, Any]) -> dict[str, Any]:
                self._pause_after_read()
                return update(document)

            return super().transactionally_update_document(
                digest,
                pause_after_locked_read,
                commit=commit,
            )

    def write_probe() -> dict[str, Any]:
        return PausingProbeStore(project).replace_probe_metadata(
            digest, {"kind": "image", "height": 17}
        )

    def write_cache() -> None:
        cache_writer_started.set()
        result = MediaBlobStore(project).compare_and_swap_media_metadata_cache(
            digest, _cache_proposal(digest, len(b"blob")), expected_generation=0
        )
        assert result.written is True

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            probe = pool.submit(write_probe)
            assert locked_read.wait(timeout=5)
            cache = pool.submit(write_cache)
            assert cache_writer_started.wait(timeout=5)
            time.sleep(0.05)
            assert not cache.done()
            release_probe_writer.set()
            assert probe.result(timeout=5)[MEDIA_PROBE_NAMESPACE]["height"] == 17
            cache.result(timeout=5)

        document = MediaBlobStore(project).metadata(digest)
        assert document[MEDIA_ACQUISITION_NAMESPACE]["title"] == "acquired"
        assert document["source_id"] == "source-1"
        assert document[MEDIA_PROBE_NAMESPACE]["height"] == 17
        assert document[MEDIA_METADATA_CACHE_NAMESPACE]["generation"] == 1
    finally:
        release_probe_writer.set()
        project.close()


def test_display_projection_has_no_flat_fallback_and_acquisition_wins(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "display-projection.frisket", name="metadata")
    try:
        digest = project.add_blob(
            b"blob",
            metadata=owned_media_metadata_document(
                probe={
                    "kind": "video",
                    "duration_seconds": 10.0,
                    "width": 640,
                },
                acquisition={"title": "Acquired", "duration_seconds": 12.0},
                owner={"title": "capture-owned", "height": 999},
            ),
        )
        store = MediaBlobStore(project)

        assert store.display_metadata(digest) == {
            "kind": "video",
            "duration_seconds": 12.0,
            "width": 640,
            "title": "Acquired",
        }
    finally:
        project.close()
