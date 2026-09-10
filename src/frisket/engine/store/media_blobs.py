"""Blob metadata and typed media/file cell helpers.

Project remains the owner of bundle filesystem lifecycle: adding blob bytes,
calculating blob paths, import/export, and garbage collection stay there. This
store owns the database metadata envelope and reusable cell-shape helpers.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from frisket.engine.store.blob_metadata import (
    BlobMetadataDocumentStore,
    BlobMetadataNamespaceCASDecision,
    canonical_blob_metadata_json,
)


MEDIA_METADATA_CACHE_NAMESPACE = "_media_metadata_v1"
MEDIA_PROBE_NAMESPACE = "_media_probe_v1"
MEDIA_ACQUISITION_NAMESPACE = "_media_acquisition_v1"
_MEDIA_METADATA_CACHE_QUALITIES = frozenset(
    {
        "complete",
        "terminal_partial",
        "degraded_missing_dependency",
        "terminal_unsupported",
        "retryable_failure",
    }
)
_MEDIA_METADATA_REUSABLE_QUALITIES = frozenset(
    {"complete", "terminal_partial", "terminal_unsupported"}
)
_MEDIA_METADATA_CACHE_MAX_FACTS_BYTES = 256 * 1024


@dataclass(frozen=True)
class MediaMetadataCacheCASResult:
    """Result of replacing the extractor-owned blob metadata namespace."""

    written: bool
    entry: dict[str, Any] | None
    reason: str


def _media_metadata_cache_state(value: Mapping[str, Any]) -> tuple[str, bool]:
    facts = value.get("content_facts")
    envelope = facts.get("envelope") if isinstance(facts, Mapping) else None
    extraction = envelope.get("extraction") if isinstance(envelope, Mapping) else None
    if not isinstance(extraction, Mapping):
        raise ValueError("media metadata cache requires hashed extraction state")
    completeness = extraction.get("completeness")
    if completeness not in _MEDIA_METADATA_CACHE_QUALITIES:
        raise ValueError("invalid media metadata cache completeness")
    retryable = extraction.get("retryable")
    expected_retryable = completeness == "retryable_failure"
    if type(retryable) is not bool or retryable is not expected_retryable:
        raise ValueError("media metadata cache completeness and retryable disagree")
    return str(completeness), retryable


def _validated_cache_entry(
    value: Mapping[str, Any],
    *,
    require_generation: bool,
    target_digest: str | None = None,
    target_size_bytes: int | None = None,
) -> dict[str, Any]:
    try:
        candidate = copy.deepcopy(dict(value))
    except RecursionError as exc:
        raise ValueError(
            "media metadata cache exceeds the nesting-depth limit"
        ) from exc
    content_facts_hash = candidate.get("content_facts_hash")
    if (
        not isinstance(content_facts_hash, str)
        or len(content_facts_hash) != 71
        or not content_facts_hash.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in content_facts_hash[7:])
    ):
        raise ValueError("media metadata cache requires a sha256 content_facts_hash")
    content_facts = candidate.get("content_facts")
    if not isinstance(content_facts, dict):
        raise ValueError("media metadata cache content_facts must be an object")
    try:
        facts_json = canonical_blob_metadata_json(content_facts)
        facts_bytes = facts_json.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError(
            "media metadata cache content_facts are not canonical JSON"
        ) from exc
    if len(facts_bytes) > _MEDIA_METADATA_CACHE_MAX_FACTS_BYTES:
        raise ValueError("media metadata cache content_facts exceed the cache bound")
    _media_metadata_cache_state(candidate)
    expected_hash = "sha256:" + hashlib.sha256(facts_bytes).hexdigest()
    if not hmac.compare_digest(content_facts_hash, expected_hash):
        raise ValueError(
            "media metadata cache content_facts_hash does not match content_facts"
        )
    if (target_digest is None) != (target_size_bytes is None):
        raise ValueError("media metadata cache target binding is incomplete")
    if target_digest is not None and target_size_bytes is not None:
        # The envelope identity is part of ``content_facts_hash``. Checking it
        # against the owning blob row prevents a valid namespace copied from a
        # different blob from becoming a reusable cache hit.
        envelope = content_facts.get("envelope")
        normalized = envelope.get("normalized") if isinstance(envelope, dict) else None
        sources = envelope.get("sources") if isinstance(envelope, dict) else None
        blob_source = sources.get("blob") if isinstance(sources, dict) else None
        qualified_digest = f"sha256:{target_digest}"
        if (
            not isinstance(normalized, dict)
            or not isinstance(blob_source, dict)
            or normalized.get("blob_hash") != qualified_digest
            or type(normalized.get("size_bytes")) is not int
            or normalized.get("size_bytes") != target_size_bytes
            or blob_source.get("blob_hash") != qualified_digest
            or type(blob_source.get("size_bytes")) is not int
            or blob_source.get("size_bytes") != target_size_bytes
        ):
            raise ValueError(
                "media metadata cache does not match the target blob digest and size"
            )
    if require_generation:
        generation = candidate.get("generation")
        if type(generation) is not int or generation < 1:
            raise ValueError("media metadata cache generation must be positive")
    # Validate JSON safety before taking a write lock.  The canonical encoding
    # also rejects NaN/Infinity instead of persisting non-portable JSON.
    canonical_blob_metadata_json(candidate)
    return candidate


def _validated_cache_proposal(
    proposal: Mapping[str, Any], *, target_digest: str, target_size_bytes: int
) -> dict[str, Any]:
    candidate = _validated_cache_entry(
        proposal,
        require_generation=False,
        target_digest=target_digest,
        target_size_bytes=target_size_bytes,
    )
    candidate["generation"] = 0
    return candidate


PROBE_DISPLAY_METADATA_KEYS = (
    "kind",
    "duration_seconds",
    "width",
    "height",
    "pages",
    "audio_codec",
    "video_codec",
    "sample_rate",
    "channels",
)
ACQUISITION_DISPLAY_METADATA_KEYS = ("title", "duration_seconds")


def owned_media_metadata_document(
    *,
    probe: Mapping[str, Any] | None = None,
    acquisition: Mapping[str, Any] | None = None,
    owner: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical blob metadata document for a newly stored blob."""

    document = copy.deepcopy(dict(owner or {}))
    if probe is not None:
        document[MEDIA_PROBE_NAMESPACE] = copy.deepcopy(dict(probe))
    if acquisition is not None:
        document[MEDIA_ACQUISITION_NAMESPACE] = copy.deepcopy(dict(acquisition))
    canonical_blob_metadata_json(document)
    return document


class MediaBlobStore(BlobMetadataDocumentStore):
    def metadata(self, digest: str) -> dict[str, Any]:
        return self.document(digest, tolerate_invalid=True)

    def media_metadata_cache(self, digest: str) -> dict[str, Any] | None:
        """Return a detached copy of the owned v1 cache namespace, if present."""

        blob = self.blob_row(digest)
        if blob is None:
            return None
        try:
            document = self.document(digest)
        except ValueError:
            return None
        entry = document.get(MEDIA_METADATA_CACHE_NAMESPACE)
        if not isinstance(entry, dict):
            return None
        try:
            return _validated_cache_entry(
                entry,
                require_generation=True,
                target_digest=str(blob["hash"]),
                target_size_bytes=int(blob["size"]),
            )
        except (TypeError, ValueError, RecursionError):
            return None

    def _owned_metadata(self, digest: str, namespace: str) -> dict[str, Any]:
        """Return one detached media metadata namespace, or an empty object."""

        document = self.document(digest, tolerate_invalid=True)
        value = document.get(namespace)
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def probe_metadata(self, digest: str) -> dict[str, Any]:
        """Return probe-owned classification and measurable media facts."""

        return self._owned_metadata(digest, MEDIA_PROBE_NAMESPACE)

    def acquisition_metadata(self, digest: str) -> dict[str, Any]:
        """Return yt-dlp acquisition and source-provenance facts."""

        return self._owned_metadata(digest, MEDIA_ACQUISITION_NAMESPACE)

    def display_metadata(self, digest: str) -> dict[str, Any]:
        """Project display facts, preferring acquisition title and duration."""

        probe = self.probe_metadata(digest)
        acquisition = self.acquisition_metadata(digest)
        display = {
            key: probe[key]
            for key in PROBE_DISPLAY_METADATA_KEYS
            if probe.get(key) is not None
        }
        for key in ACQUISITION_DISPLAY_METADATA_KEYS:
            if acquisition.get(key) is not None:
                display[key] = acquisition[key]
        return display

    def replace_probe_metadata(
        self,
        digest: str,
        metadata: Mapping[str, Any],
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Atomically replace only the probe-owned namespace."""

        probe = copy.deepcopy(dict(metadata))

        def replace(current: dict[str, Any]) -> dict[str, Any]:
            current[MEDIA_PROBE_NAMESPACE] = probe
            return current

        return self.transactionally_update_document(digest, replace, commit=commit)

    def compare_and_swap_media_metadata_cache(
        self,
        digest: str,
        proposal: Mapping[str, Any],
        *,
        expected_generation: int,
        commit: bool = True,
    ) -> MediaMetadataCacheCASResult:
        """Atomically replace only the extractor-owned cache namespace.

        ``expected_generation`` is the optimistic-concurrency guard and is
        REQUIRED: it used to default to ``None``, and ``None`` skipped the
        mismatch check entirely, so omitting the argument silently turned the
        guard off and overwrote whatever was there. "I have no generation" is
        spelled ``0`` -- the insert-only expectation, which succeeds when no
        (valid) entry exists and reports ``generation_mismatch`` when one does.

        A racing writer wins the shared cache, while the losing extraction
        result remains perfectly usable for its current row. Invalid old cache
        state is disposable and may be replaced by a reusable proposal.
        """

        blob = self.blob_row(digest)
        if blob is None:
            raise KeyError(f"no blob {digest}")
        target_digest = str(blob["hash"])
        target_size_bytes = int(blob["size"])
        candidate = _validated_cache_proposal(
            proposal,
            target_digest=target_digest,
            target_size_bytes=target_size_bytes,
        )
        candidate_completeness, _candidate_retryable = _media_metadata_cache_state(
            candidate
        )
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("expected_generation must be a nonnegative integer")

        def resolve(
            raw_current: Any, raw_candidate: Any
        ) -> BlobMetadataNamespaceCASDecision:
            current: dict[str, Any] | None = None
            if isinstance(raw_current, dict):
                try:
                    current = _validated_cache_entry(
                        raw_current,
                        require_generation=True,
                        target_digest=target_digest,
                        target_size_bytes=target_size_bytes,
                    )
                except (TypeError, ValueError, RecursionError):
                    current = None
            if candidate_completeness not in _MEDIA_METADATA_REUSABLE_QUALITIES:
                return BlobMetadataNamespaceCASDecision(
                    written=False,
                    value=current,
                    reason="not_reusable",
                )
            if current is None:
                next_generation = 1
            else:
                current_generation = int(current["generation"])
                if expected_generation != current_generation:
                    return BlobMetadataNamespaceCASDecision(
                        written=False,
                        value=current,
                        reason="generation_mismatch",
                    )
                next_generation = current_generation + 1

            raw_candidate["generation"] = next_generation
            return BlobMetadataNamespaceCASDecision(
                written=True,
                value=raw_candidate,
                reason="written",
            )

        result = self.compare_and_swap_metadata_namespace(
            digest,
            MEDIA_METADATA_CACHE_NAMESPACE,
            candidate,
            resolve,
            commit=commit,
        )
        return MediaMetadataCacheCASResult(
            written=result.written,
            entry=result.value if isinstance(result.value, dict) else None,
            reason=result.reason,
        )

    def blob_row(self, digest: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT hash, filename, mime, size, source_url, metadata "
            "FROM blobs WHERE hash=?",
            (digest,),
        ).fetchone()

    def blob_exists(self, digest: str) -> bool:
        return self.blob_row(digest) is not None

    def hashes_needing_metadata(
        self, *, force: bool = False, limit: int | None = None
    ) -> list[str]:
        where = ""
        if not force:
            where = (
                "WHERE COALESCE(json_type(CASE WHEN json_valid(metadata) "
                "THEN metadata ELSE '{}' END, '$._media_probe_v1'), '')!='object'"
            )
        sql = f"SELECT hash FROM blobs {where} ORDER BY created_at, hash"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [str(row["hash"]) for row in self.db.execute(sql, params).fetchall()]

    def update_metadata(
        self,
        digest: str,
        metadata: dict[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        if not metadata:
            return
        if replace:
            self.transactionally_update_document(
                digest,
                lambda _current: dict(metadata),
                commit=False,
            )
        else:
            self.merge_metadata(digest, metadata)
        self.db.commit()

    def merge_metadata(self, digest: str, metadata: dict[str, Any]) -> None:
        def merge(current: dict[str, Any]) -> dict[str, Any]:
            current.update(metadata)
            return current

        self.transactionally_update_document(digest, merge, commit=False)

    @staticmethod
    def media_cell(
        digest: str,
        *,
        mime: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        cell: dict[str, Any] = {"blob": digest}
        if mime:
            cell["mime"] = mime
        if filename:
            cell["filename"] = filename
        return cell

    @staticmethod
    def file_cell(
        digest: str,
        *,
        mime: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        return MediaBlobStore.media_cell(digest, mime=mime, filename=filename)

    @staticmethod
    def validate_blob_cell(cell: Any) -> dict[str, Any]:
        if not isinstance(cell, dict):
            raise ValueError("blob cell must be an object")
        digest = cell.get("blob")
        if not isinstance(digest, str) or not digest:
            raise ValueError("blob cell requires a blob digest")
        mime = cell.get("mime")
        if mime is not None and not isinstance(mime, str):
            raise ValueError("blob cell mime must be a string")
        filename = cell.get("filename")
        if filename is not None and not isinstance(filename, str):
            raise ValueError("blob cell filename must be a string")
        return dict(cell)


# The canonical importable name for building media cells; the staticmethod
# stays so store consumers holding a MediaBlobStore can use it directly.
media_cell = MediaBlobStore.media_cell


def update_blob_metadata(
    project: Any,
    digest: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Probe one stored blob and replace only its owned probe metadata."""

    store = MediaBlobStore(project)
    existing = store.probe_metadata(digest)
    if existing and not force:
        return existing
    row = store.blob_row(digest)
    if row is None:
        raise KeyError(f"no blob {digest}")
    from frisket.ops.media_probe import probe_for_ingest

    with project.materialize_blob(digest) as path:
        metadata = probe_for_ingest(
            path,
            mime=row["mime"],
            filename=row["filename"],
            digest=digest,
        )
    store.replace_probe_metadata(digest, metadata)
    return metadata


def backfill_blob_metadata(
    project: Any,
    *,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Probe every blob still missing display metadata; partial by design."""

    store = MediaBlobStore(project)
    digests = store.hashes_needing_metadata(force=force, limit=limit)
    updated = 0
    errors: list[dict[str, str]] = []
    for digest in digests:
        try:
            metadata = update_blob_metadata(project, digest, force=force)
            if metadata:
                updated += 1
        except Exception as exc:  # noqa: BLE001 - batch backfill is partial
            errors.append({"hash": digest, "error": str(exc)[:300]})
    return {
        "scanned": len(digests),
        "updated": updated,
        "errors": errors,
        "force": force,
    }
