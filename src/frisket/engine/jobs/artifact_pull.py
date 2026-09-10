"""HTTP-artifact pull backend.

The ``model.pull`` handler dispatches by ref scheme (``artifact_ref``): an
Ollama ref keeps the existing ``model_pull._run_pull`` flow verbatim; an
``opus-mt:``/``hf:``/``hf-snapshot:``/``spacy:`` ref routes here. This backend OWNS the
bytes (unlike Ollama, whose daemon owns its blob store), through one of TWO
distinct download paths depending on the pinned entry's integrity guarantee
(``PinnedArtifact.integrity`` -- see ``artifact_manifest``'s docstring):

- ``checksum`` (``ct2_pair``/``hf_file``/``spacy_model``): steps 3-6 below -- this backend
  streams and verifies the bytes itself.
- ``hf_revision_pinned`` (``hf_snapshot``): a THIN delegation to
  ``huggingface_hub.snapshot_download`` (``_run_hf_snapshot_pull``) -- no
  streaming, no per-file verify, no ``model_cache`` promote; HF's own cache
  layout, dedup, and resume already do that job.

1. Resolves the ref against the pinned manifest (``artifact_manifest``). An
   ``opus-mt:`` ref is manifest-gated (a miss is terminal ``manifest_missing``);
   an ``hf-snapshot:`` ref is ALSO always manifest-gated (no free-form path --
   see the manifest docstring); a ``hf:`` ref MAY be pinned, else it is pulled
   as a free-form **unpinned** artifact (no checksum and no license; the
   operator accepts responsibility for that path).
2. Records the artifact provenance on the pull row.
3. Fast-paths an already-installed artifact (cheap presence+size probe).
4. Streams each file into ``.pull-tmp/<pull_id>/``, computing the running
   SHA256, updating byte progress, checking cooperative cancel between chunks.
5. Verifies each pinned file's SHA256 before promotion (mismatch is terminal
   ``checksum_mismatch``).
6. Atomically promotes the staging tree into the install dir and records the
   composite digest + total size.

It reuses the model-pull worker's failure discipline wholesale: the ``_fail``
terminal/retryable taxonomy, the ``_redact`` secret net, and the "canonical
copy only, never raw upstream body" rule.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from frisket.engine.jobs import model_pull_store
from frisket.engine.jobs.artifact_ref import ArtifactRef
from frisket.engine.jobs.model_pull import _fail
from frisket.ai.models import artifact_manifest, model_cache
from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

LOG = logging.getLogger("frisket.jobs.artifact_pull")

_STREAM_CHUNK_BYTES = 1024 * 256
_ARTIFACT_CONNECT_TIMEOUT_SECONDS = 15.0
_ARTIFACT_READ_TIMEOUT_SECONDS = 60.0
_PROGRESS_WRITE_MIN_INTERVAL_SECONDS = 0.5

# HF ``resolve`` URL for an unpinned free-form hf: file.
_HF_RESOLVE_BASE = "https://huggingface.co"


def default_manifest_lookup(ref: str) -> PinnedArtifact | None:
    return artifact_manifest.lookup(ref)


def _int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _content_range_total(value: str | None) -> int | None:
    """Parse the total size out of a ``Content-Range: bytes N-M/T`` header
    (Q9 resume). Returns None if absent or ``*`` (unknown total)."""
    if not value:
        return None
    tail = value.rsplit("/", 1)[-1].strip()
    if not tail or tail == "*":
        return None
    try:
        return int(tail)
    except ValueError:
        return None


def _sha256_file(path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _installed_composite_digest(
    art: ArtifactRef, files: list[PinnedFile], cache_root
) -> str | None:
    """Hash the on-disk installed bytes and return the composite digest, or
    None if any file is missing. The fast path must not trust presence and
    size or stamp the manifest digest without hashing. Uses the
    same order-independent composite as PinnedArtifact.composite_digest."""
    install_dir = model_cache.artifact_install_dir(art, root=cache_root)
    per_file: list[tuple[str, str]] = []
    for pinned_file in files:
        path = install_dir / pinned_file.repo_relpath
        if not path.is_file():
            return None
        per_file.append((_sha256_file(path), pinned_file.repo_relpath))
    if len(per_file) == 1:
        return f"sha256:{per_file[0][0]}"
    lines = sorted(f"{d}  {p}" for d, p in per_file)
    return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class ArtifactProvisionError(RuntimeError):
    """A worker-local on-use provisioning (``provision_pinned``) failed --
    download error or checksum mismatch. Distinct from the durable pull
    backend's store-recorded failures (this path has no ``model_pulls`` row).

    ``code`` preserves the durable pull's integrity taxonomy so the caller can
    tell TAMPERING (``checksum_mismatch``, terminal) from a transport/HTTP miss
    (``transport`` / ``source_http_error``, retryable) instead of collapsing
    every failure to "not installed"."""

    def __init__(self, message: str, *, code: str = "transport") -> None:
        super().__init__(message)
        self.code = code


class ProvisionCancelled(RuntimeError):
    """The cooperative cancel signal fired mid-download during a worker-local
    ``provision_pinned``. Raised so a cancelled run aborts the
    ~1.1GB pull promptly instead of blocking the row until the stream finishes;
    the staging bytes are discarded, so the next attempt re-provisions."""


def provision_pinned(
    art: ArtifactRef,
    pinned: PinnedArtifact,
    *,
    cache_root,
    client: httpx.Client,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Download + verify + atomically install a pinned artifact worker-locally,
    without a ``model_pulls`` row (lazy pull-on-first-use).

    Used by the runtime (opus_mt/hy_mt2) when a translate row lands on a worker
    that does not yet have the pinned artifact -- the manifest guarantees
    checksum + license, so no acknowledgment is needed. Streams each file with
    SHA256 verification into an isolated staging dir and promotes it atomically;
    raises :class:`ArtifactProvisionError` (never leaves a partial install).

    ``should_cancel`` is polled between chunks so the cooperative
    run-cancel aborts the download promptly; on cancel the staging is discarded
    and :class:`ProvisionCancelled` is raised."""
    import uuid

    token = f"prov-{uuid.uuid4().hex}"
    model_cache.discard_tmp(token, root=cache_root)
    staging = model_cache.tmp_dir(token, root=cache_root)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for pinned_file in pinned.files:
            dest = staging / pinned_file.repo_relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            with client.stream(
                "GET",
                pinned_file.source,
                timeout=httpx.Timeout(
                    _ARTIFACT_READ_TIMEOUT_SECONDS,
                    connect=_ARTIFACT_CONNECT_TIMEOUT_SECONDS,
                ),
                follow_redirects=True,
            ) as response:
                if response.status_code != 200:
                    raise ArtifactProvisionError(
                        f"artifact source returned HTTP {response.status_code}",
                        code="source_http_error",
                    )
                with dest.open("wb") as handle:
                    for chunk in response.iter_bytes(_STREAM_CHUNK_BYTES):
                        if should_cancel is not None and should_cancel():
                            response.close()
                            handle.close()
                            model_cache.discard_tmp(token, root=cache_root)
                            raise ProvisionCancelled(
                                "artifact provisioning cancelled mid-download"
                            )
                        handle.write(chunk)
                        hasher.update(chunk)
            if pinned_file.sha256 and hasher.hexdigest() != pinned_file.sha256:
                raise ArtifactProvisionError(
                    f"checksum mismatch for {pinned_file.repo_relpath}",
                    code="checksum_mismatch",
                )
        model_cache.promote(token, art, root=cache_root)
    except httpx.HTTPError as exc:
        model_cache.discard_tmp(token, root=cache_root)
        raise ArtifactProvisionError(
            f"could not reach the artifact source ({type(exc).__name__})",
            code="transport",
        ) from exc
    except Exception:
        model_cache.discard_tmp(token, root=cache_root)
        raise


def _run_hf_snapshot_pull(
    engine,
    pull_id: int,
    pinned: PinnedArtifact,
    *,
    should_cancel: Callable[[], bool],
    is_final_attempt: bool,
) -> dict:
    """Provision an ``hf_snapshot`` (revision-pinned) artifact for a durable
    pull row -- a THIN delegation to ``huggingface_hub.snapshot_download``.

    Deliberately NOT the byte-streaming/verify/promote path above: this
    entry's integrity guarantee is the pinned git revision
    (``pinned.integrity == "hf_revision_pinned"``), not a per-file checksum,
    so there is nothing here to hash before trusting, and reimplementing
    HF's cache layout/dedup/resume would be exactly the over-engineering the
    maintainer decision rejected for HF-sourced weights. The files land in
    HF's own hub cache layout, not ``model_cache`` -- under the directory
    ``huggingface_hub_cache()`` names (see the ``cache_dir=`` note below).

    Trade-off of thin delegation: ``snapshot_download`` is one opaque call
    with no per-chunk progress or cancel hook, so ``should_cancel`` is only
    checked BEFORE starting it, not while it runs -- a cooperative cancel
    requested mid-download is honored on the NEXT attempt, not immediately
    (unlike the streaming path's between-chunk checks).
    """
    snapshot = pinned.hf_snapshot
    assert snapshot is not None
    if should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    model_pull_store.set_artifact_metadata(
        engine,
        pull_id,
        artifact_kind=pinned.kind,
        artifact_source_url=pinned.source_url,
        artifact_license=pinned.license,
        artifact_manifest_version=pinned.manifest_version,
    )
    model_pull_store.update_progress(
        engine, pull_id, phase="downloading", total_bytes=None, completed_bytes=0
    )
    try:
        from huggingface_hub import snapshot_download

        from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache

        snapshot_download(
            snapshot.repo_id,
            revision=snapshot.revision,
            # THE Hub cache, resolved by frisket's one resolver and PASSED IN
            # -- never left to huggingface_hub to resolve for itself.
            #
            # Every surface that READS these bytes (the local-onnx liveness
            # probe in execution/definitions.py, the Parakeet resolver child,
            # the faster-whisper worker, operability/diagnostics) asks
            # ``huggingface_hub_cache()``, which consults
            # HF_HUB_CACHE -> HUGGINGFACE_HUB_CACHE -> HF_HOME/hub ->
            # ~/.cache/huggingface/hub. The library's own default inserts
            # XDG_CACHE_HOME under HF_HOME's fallback, so with XDG_CACHE_HOME
            # set and no HF_* variable (a plain pip or bare install; the
            # Docker image is safe because its Dockerfile pins HF_HUB_CACHE)
            # the two answers DIVERGE: the pull downloaded ~600MB into the
            # XDG path, marked the row done, and every reader kept looking in
            # ~/.cache and reported the model not installed.
            #
            # Same defect class as sdk/ops/transcribe_engines.py's faster_whisper_env
            # and _workers/parakeet_session.py's parakeet_inference_env, and
            # the same fix those already carry: resolve once, pass the answer.
            cache_dir=huggingface_hub_cache(),
            allow_patterns=list(snapshot.files),
            token=False,
        )
    except Exception as exc:  # huggingface_hub raises its own hierarchy
        raise _fail(
            engine,
            pull_id,
            error_code="artifact_source_unreachable",
            message=(
                f"could not resolve the pinned HF snapshot ({type(exc).__name__})"
            ),
            terminal=False,
            is_final_attempt=is_final_attempt,
        ) from None
    model_pull_store.mark_done(
        engine,
        pull_id,
        resolved_digest=pinned.composite_digest,
        resolved_size=None,
    )
    return {"status": "done"}


def _unpinned_hf_plan(art: ArtifactRef) -> tuple[str, list[PinnedFile]]:
    """Synthesize a single-file download plan for an unpinned ``hf:`` ref. No
    sha256 pin (empty string == verification skipped), size unknown (-1 ==
    Content-Length-driven progress, no presence-size gate)."""
    assert art.hf_repo and art.hf_revision and art.hf_path
    source = f"{_HF_RESOLVE_BASE}/{art.hf_repo}/resolve/{art.hf_revision}/{art.hf_path}"
    return source, [
        PinnedFile(repo_relpath=art.hf_path, source=source, sha256="", size=-1)
    ]


def run_artifact_pull(
    client: httpx.Client,
    *,
    engine,
    pull_id: int,
    art: ArtifactRef,
    should_cancel: Callable[[], bool],
    is_final_attempt: bool,
    manifest_lookup: Callable[[str], PinnedArtifact | None] = default_manifest_lookup,
    cache_root: Path | None = None,
    unpinned_acknowledged: bool = False,
) -> dict:
    """Run one attempt of an artifact pull. Returns the terminal handler dict
    (``{"status": "done"|"cancelled", ...}``) or raises via ``_fail``."""
    pinned = manifest_lookup(art.canonical)

    # hf_snapshot (revision-pinned, thin delegation to snapshot_download) is a
    # DIFFERENT download mechanism, not just a different verify step -- dispatch
    # to it before any of the streaming/model_cache-oriented logic below runs.
    if pinned is not None and pinned.kind == "hf_snapshot":
        return _run_hf_snapshot_pull(
            engine,
            pull_id,
            pinned,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )

    if pinned is not None:
        kind = pinned.kind
        files = list(pinned.files)
        source_url: str | None = pinned.source_url
        license_id: str | None = pinned.license
        manifest_version: str | None = pinned.manifest_version
        total_size: int | None = pinned.total_size
        composite_expected: str | None = pinned.composite_digest
    else:
        # Not pinned. opus-mt:, hf-snapshot:, and spacy: are manifest-gated --
        # a miss is terminal (hf-snapshot: has no free-form path: a multi-file snapshot
        # pull is a bigger blast radius than a single unpinned file, so the
        # "unpinned, on you" carve-out does not extend to it).
        if art.scheme == "opus-mt":
            raise _fail(
                engine,
                pull_id,
                error_code="manifest_missing",
                message=(
                    "this Opus-MT pair is not in the pinned catalog "
                    "(it must be converted and pinned before it can be installed)"
                ),
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        if art.scheme == "hf-snapshot":
            raise _fail(
                engine,
                pull_id,
                error_code="manifest_missing",
                message=(
                    "this HF snapshot artifact is not in the pinned catalog "
                    "(hf-snapshot: refs have no unpinned path)"
                ),
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        if art.scheme == "spacy":
            raise _fail(
                engine,
                pull_id,
                error_code="manifest_missing",
                message=(
                    "this spaCy model is not in the pinned catalog "
                    "(spacy: refs have no unpinned path)"
                ),
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        # hf: free-form unpinned pull -- requires the explicit
        # "unpinned -- on you" acknowledgment.
        if not unpinned_acknowledged:
            raise _fail(
                engine,
                pull_id,
                error_code="unpinned_unacknowledged",
                message=(
                    "this artifact is not pinned by frisket; pulling it requires "
                    "acknowledging that it has no checksum or license vetting"
                ),
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        source_url, files = _unpinned_hf_plan(art)
        kind = "hf_file"
        license_id = None
        manifest_version = None
        total_size = None
        composite_expected = None

    model_pull_store.set_artifact_metadata(
        engine,
        pull_id,
        artifact_kind=kind,
        artifact_source_url=source_url,
        artifact_license=license_id,
        artifact_manifest_version=manifest_version,
    )
    model_pull_store.update_progress(
        engine, pull_id, phase="manifest", total_bytes=total_size, completed_bytes=0
    )

    # Idempotency fast path: only short-circuit when the ON-DISK bytes actually
    # HASH to the pinned composite -- never trust presence+size and stamp the
    # manifest digest without hashing (that would let a corrupted/tampered
    # install serve while provenance claims it is pinned-clean). A mismatch (or
    # a missing file) falls through to a fresh download rather than trusting
    # the stale bytes.
    if pinned is not None and composite_expected is not None:
        installed_digest = _installed_composite_digest(art, files, cache_root)
        if installed_digest == composite_expected:
            model_pull_store.mark_done(
                engine,
                pull_id,
                resolved_digest=composite_expected,
                resolved_size=total_size,
            )
            return {"status": "done", "already_installed": True}

    if should_cancel():
        model_cache.discard_tmp(pull_id, root=cache_root)
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}

    # Staging PERSISTS across attempts of the same pull_id so an interrupted
    # download resumes: a retried attempt reuses any partial file already on
    # disk via an HTTP Range request rather than re-fetching from byte 0 --
    # material for the ~1.1GB Hy-MT2 GGUF. HF serves ranges (verified:
    # Accept-Ranges: bytes, 206 on an explicit Range). We do NOT discard
    # staging at attempt start; only a cancel or a terminal failure discards,
    # so a retryable drop keeps its progress for the next attempt.
    staging = model_cache.tmp_dir(pull_id, root=cache_root)
    staging.mkdir(parents=True, exist_ok=True)

    prior_files_bytes = 0  # fully-downloaded bytes of files completed this run
    per_file_digests: list[tuple[str, str]] = []  # (sha256, repo_relpath)
    import time

    last_write = 0.0
    for pinned_file in files:
        dest = staging / pinned_file.repo_relpath
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Resume from whatever is already on disk for THIS file.
        resume_from = dest.stat().st_size if dest.exists() else 0
        hasher = hashlib.sha256()
        if resume_from:
            with dest.open("rb") as fh:
                for blk in iter(lambda: fh.read(1024 * 1024), b""):
                    hasher.update(blk)
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

        model_pull_store.update_progress(
            engine,
            pull_id,
            phase="downloading",
            total_bytes=total_size,
            completed_bytes=prior_files_bytes + resume_from,
        )
        try:
            with client.stream(
                "GET",
                pinned_file.source,
                headers=headers,
                timeout=httpx.Timeout(
                    _ARTIFACT_READ_TIMEOUT_SECONDS,
                    connect=_ARTIFACT_CONNECT_TIMEOUT_SECONDS,
                ),
                follow_redirects=True,
            ) as response:
                status = response.status_code
                if status == 404:
                    response.close()
                    model_cache.discard_tmp(pull_id, root=cache_root)
                    raise _fail(
                        engine,
                        pull_id,
                        error_code="artifact_not_found",
                        message=(
                            "the pinned artifact file was not found at its "
                            "recorded revision (HTTP 404)"
                        ),
                        terminal=True,
                        is_final_attempt=is_final_attempt,
                    )
                if status == 416:
                    # Range Not Satisfiable: the partial is >= the full size
                    # (a stale/over-long partial). Drop it and let the retry
                    # re-download this file cleanly from zero.
                    response.close()
                    dest.unlink(missing_ok=True)
                    raise _fail(
                        engine,
                        pull_id,
                        error_code="artifact_source_unreachable",
                        message="a stale partial download was reset; retrying",
                        terminal=False,
                        is_final_attempt=is_final_attempt,
                    )
                if status >= 500:
                    response.close()  # retryable: KEEP the partial for resume
                    raise _fail(
                        engine,
                        pull_id,
                        error_code="artifact_source_unreachable",
                        message=(
                            "the artifact source returned a server error "
                            f"(HTTP {status})"
                        ),
                        terminal=False,
                        is_final_attempt=is_final_attempt,
                    )
                if status not in (200, 206):
                    response.close()
                    model_cache.discard_tmp(pull_id, root=cache_root)
                    raise _fail(
                        engine,
                        pull_id,
                        error_code="artifact_source_http_error",
                        message=(
                            "the artifact source returned an unexpected status "
                            f"(HTTP {status})"
                        ),
                        terminal=True,
                        is_final_attempt=is_final_attempt,
                    )

                # A 206 honors our Range (append); a 200 means the server
                # ignored it (rewrite the whole file, re-hash from scratch).
                resuming = status == 206 and resume_from > 0
                if not resuming:
                    resume_from = 0
                    hasher = hashlib.sha256()
                mode = "ab" if resuming else "wb"

                # The expected FULL size: from Content-Range on a 206, else
                # Content-Length on a 200. Drives the truncation check
                # and works for unpinned pulls too.
                expected_full = _int_header(response.headers.get("content-length"))
                if resuming:
                    expected_full = _content_range_total(
                        response.headers.get("content-range")
                    )

                written = resume_from
                with dest.open(mode) as handle:
                    for chunk in response.iter_bytes(_STREAM_CHUNK_BYTES):
                        if should_cancel():
                            response.close()
                            handle.close()
                            model_cache.discard_tmp(pull_id, root=cache_root)
                            model_pull_store.mark_cancelled(engine, pull_id)
                            return {"status": "cancelled"}
                        handle.write(chunk)
                        hasher.update(chunk)
                        written += len(chunk)
                        now_m = time.monotonic()
                        if (now_m - last_write) >= _PROGRESS_WRITE_MIN_INTERVAL_SECONDS:
                            model_pull_store.update_progress(
                                engine,
                                pull_id,
                                phase="downloading",
                                total_bytes=total_size,
                                completed_bytes=prior_files_bytes + written,
                            )
                            last_write = now_m
                if expected_full is not None and written != expected_full:
                    # Truncated stream: KEEP the partial (retryable) so the next
                    # attempt resumes from `written`, not from zero.
                    raise _fail(
                        engine,
                        pull_id,
                        error_code="artifact_source_unreachable",
                        message=(
                            "the artifact download was truncated (received "
                            f"{written} of {expected_full} bytes)"
                        ),
                        terminal=False,
                        is_final_attempt=is_final_attempt,
                    )
        except httpx.HTTPError as exc:
            # Transport drop: retryable, and the partial STAYS on disk so the
            # requeued attempt resumes via Range.
            raise _fail(
                engine,
                pull_id,
                error_code="artifact_source_unreachable",
                message=(f"could not reach the artifact source ({type(exc).__name__})"),
                terminal=False,
                is_final_attempt=is_final_attempt,
            ) from None

        computed = hasher.hexdigest()
        per_file_digests.append((computed, pinned_file.repo_relpath))
        prior_files_bytes += written

        # Verify the pin (pinned files only; unpinned files carry sha256="").
        if pinned_file.sha256 and computed != pinned_file.sha256:
            model_cache.discard_tmp(pull_id, root=cache_root)
            raise _fail(
                engine,
                pull_id,
                error_code="checksum_mismatch",
                message=(
                    "a downloaded file's checksum did not match the pinned "
                    "value; the artifact was discarded"
                ),
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
    completed_total = prior_files_bytes

    model_pull_store.update_progress(
        engine,
        pull_id,
        phase="verifying",
        total_bytes=total_size,
        completed_bytes=completed_total,
    )

    # Compose the resolved digest. Pinned -> the manifest composite (already
    # verified per-file); unpinned -> compute from what we downloaded.
    if composite_expected is not None:
        resolved_digest = composite_expected
    elif len(per_file_digests) == 1:
        resolved_digest = f"sha256:{per_file_digests[0][0]}"
    else:
        lines = sorted(f"{d}  {p}" for d, p in per_file_digests)
        resolved_digest = (
            "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        )

    model_pull_store.update_progress(
        engine,
        pull_id,
        phase="writing",
        total_bytes=total_size,
        completed_bytes=completed_total,
    )
    try:
        model_cache.promote(pull_id, art, root=cache_root)
    except OSError as exc:
        model_cache.discard_tmp(pull_id, root=cache_root)
        raise _fail(
            engine,
            pull_id,
            error_code="disk_write_failed",
            message=f"could not write the artifact into place ({type(exc).__name__})",
            terminal=True,
            is_final_attempt=is_final_attempt,
        ) from None

    model_pull_store.mark_done(
        engine,
        pull_id,
        resolved_digest=resolved_digest,
        resolved_size=completed_total if total_size is None else total_size,
    )
    return {"status": "done"}


__all__ = [
    "ArtifactProvisionError",
    "ProvisionCancelled",
    "default_manifest_lookup",
    "provision_pinned",
    "run_artifact_pull",
]
