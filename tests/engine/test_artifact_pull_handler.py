"""Artifact pull backend pins -- no live network, only MockTransport."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.artifact_pull import run_artifact_pull
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.queue import open_queue
from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache
from frisket.ai.models import artifact_manifest, model_cache
from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

_BASE = "https://frisket.test"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pair_entry(files: dict[str, bytes]) -> PinnedArtifact:
    pinned = tuple(
        PinnedFile(
            repo_relpath=name,
            source=f"{_BASE}/en-es/{name}",
            sha256=_sha(data),
            size=len(data),
        )
        for name, data in files.items()
    )
    return PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://example/cc-by-4.0",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-es-ct2@rev",
        files=pinned,
    )


def _transport(bodies: dict[str, bytes], *, status: dict[str, int] | None = None):
    status = status or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in status:
            return httpx.Response(status[url])
        if url in bodies:
            return httpx.Response(200, content=bodies[url])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _client(transport) -> httpx.Client:
    return httpx.Client(transport=transport)


def _make_row(engine, ref: str) -> int:
    row, created = store.create_or_get_active(
        engine, workspace_root="/ws", model_ref=ref
    )
    assert created
    return row.id


def _no_cancel() -> bool:
    return False


def test_happy_path_two_file_pair(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        files = {"model.bin": b"weights-bytes", "source.spm": b"spm-bytes"}
        entry = _pair_entry(files)
        pull_id = _make_row(engine, "opus-mt:en-es")
        bodies = {f"{_BASE}/en-es/{n}": d for n, d in files.items()}
        client = _client(_transport(bodies))
        art = normalize_artifact_ref("opus-mt:en-es")

        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=_no_cancel,
            is_final_attempt=False,
            manifest_lookup=lambda ref: entry if ref == "opus-mt:en-es" else None,
            cache_root=tmp_path / "cache",
        )
        assert result["status"] == "done"
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_DONE
        assert row.resolved_digest == entry.composite_digest
        assert row.resolved_size == entry.total_size
        assert row.artifact_kind == "ct2_pair"
        assert row.artifact_license == "CC-BY-4.0"
        install = model_cache.artifact_install_dir(art, root=tmp_path / "cache")
        assert (install / "model.bin").read_bytes() == b"weights-bytes"
        assert (install / "source.spm").read_bytes() == b"spm-bytes"
        # staging is gone (moved into place)
        assert not model_cache.tmp_dir(pull_id, root=tmp_path / "cache").exists()
    finally:
        queue.close()


def test_checksum_mismatch_is_terminal_and_cleans_up(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        entry = _pair_entry({"model.bin": b"correct-bytes"})
        pull_id = _make_row(engine, "opus-mt:en-es")
        # Serve WRONG bytes for the pinned file.
        bodies = {f"{_BASE}/en-es/model.bin": b"tampered-bytes"}
        client = _client(_transport(bodies))
        art = normalize_artifact_ref("opus-mt:en-es")

        with pytest.raises(Exception):  # noqa: B017 -- NonRetryableJobError
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                manifest_lookup=lambda ref: entry,
                cache_root=tmp_path / "cache",
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_FAILED
        assert row.error_code == "checksum_mismatch"
        assert not model_cache.artifact_install_dir(
            art, root=tmp_path / "cache"
        ).exists()
        assert not model_cache.tmp_dir(pull_id, root=tmp_path / "cache").exists()
    finally:
        queue.close()


def test_already_installed_fast_path(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        files = {"model.bin": b"weights", "source.spm": b"spm"}
        entry = _pair_entry(files)
        pull_id = _make_row(engine, "opus-mt:en-es")
        art = normalize_artifact_ref("opus-mt:en-es")
        # Pre-install the artifact at expected sizes.
        install = model_cache.artifact_install_dir(art, root=tmp_path / "cache")
        install.mkdir(parents=True)
        for n, d in files.items():
            (install / n).write_bytes(d)

        def _boom(request):
            raise AssertionError("must not download when already installed")

        client = httpx.Client(transport=httpx.MockTransport(_boom))
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=_no_cancel,
            is_final_attempt=False,
            manifest_lookup=lambda ref: entry,
            cache_root=tmp_path / "cache",
        )
        assert result == {"status": "done", "already_installed": True}
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_DONE
        assert row.resolved_digest == entry.composite_digest
    finally:
        queue.close()


def test_cancel_between_chunks_discards_partial(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        entry = _pair_entry({"model.bin": b"x" * 2_000_000})
        pull_id = _make_row(engine, "opus-mt:en-es")
        bodies = {f"{_BASE}/en-es/model.bin": b"x" * 2_000_000}
        client = _client(_transport(bodies))
        art = normalize_artifact_ref("opus-mt:en-es")

        calls = {"n": 0}

        def cancel_soon() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # cancel after the first chunk

        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=cancel_soon,
            is_final_attempt=False,
            manifest_lookup=lambda ref: entry,
            cache_root=tmp_path / "cache",
        )
        assert result["status"] == "cancelled"
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_CANCELLED
        assert not model_cache.artifact_install_dir(
            art, root=tmp_path / "cache"
        ).exists()
        assert not model_cache.tmp_dir(pull_id, root=tmp_path / "cache").exists()
    finally:
        queue.close()


def test_source_unreachable_is_retryable_and_row_stays_active(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        entry = _pair_entry({"model.bin": b"data"})
        pull_id = _make_row(engine, "opus-mt:en-es")
        store.mark_running(engine, pull_id, job_id=1)

        def connect_error(request):
            raise httpx.ConnectError("boom")

        client = httpx.Client(transport=httpx.MockTransport(connect_error))
        art = normalize_artifact_ref("opus-mt:en-es")
        from frisket.engine.jobs.model_pull import ModelPullTerminalError

        with pytest.raises(ModelPullTerminalError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,  # attempts remain -> row stays active
                manifest_lookup=lambda ref: entry,
                cache_root=tmp_path / "cache",
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_RUNNING  # not finalized
        assert row.error_code == "artifact_source_unreachable"
    finally:
        queue.close()


def test_404_at_pinned_revision_is_terminal(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        entry = _pair_entry({"model.bin": b"data"})
        pull_id = _make_row(engine, "opus-mt:en-es")
        client = _client(_transport({}, status={f"{_BASE}/en-es/model.bin": 404}))
        art = normalize_artifact_ref("opus-mt:en-es")
        from frisket.engine.jobs.worker import NonRetryableJobError

        with pytest.raises(NonRetryableJobError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                manifest_lookup=lambda ref: entry,
                cache_root=tmp_path / "cache",
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_FAILED
        assert row.error_code == "artifact_not_found"
    finally:
        queue.close()


def test_opus_mt_not_in_manifest_is_terminal(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        pull_id = _make_row(engine, "opus-mt:xx-yy")
        client = _client(_transport({}))
        art = normalize_artifact_ref("opus-mt:xx-yy")
        from frisket.engine.jobs.worker import NonRetryableJobError

        with pytest.raises(NonRetryableJobError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                manifest_lookup=lambda ref: None,
                cache_root=tmp_path / "cache",
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_FAILED
        assert row.error_code == "manifest_missing"
    finally:
        queue.close()


def test_unpinned_hf_requires_acknowledgment(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        ref = "hf:owner/repo@abc123/model.gguf"
        pull_id = _make_row(engine, ref)
        client = _client(_transport({}))
        art = normalize_artifact_ref(ref)
        from frisket.engine.jobs.worker import NonRetryableJobError

        with pytest.raises(NonRetryableJobError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                manifest_lookup=lambda ref: None,
                cache_root=tmp_path / "cache",
                unpinned_acknowledged=False,
            )
        row = store.get(engine, pull_id)
        assert row.error_code == "unpinned_unacknowledged"
    finally:
        queue.close()


def test_unpinned_hf_acknowledged_downloads_without_verification(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        ref = "hf:owner/repo@abc123/model.gguf"
        pull_id = _make_row(engine, ref)
        art = normalize_artifact_ref(ref)
        payload = b"unpinned-gguf-bytes"
        url = "https://huggingface.co/owner/repo/resolve/abc123/model.gguf"
        client = _client(_transport({url: payload}))
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=_no_cancel,
            is_final_attempt=False,
            manifest_lookup=lambda ref: None,
            cache_root=tmp_path / "cache",
            unpinned_acknowledged=True,
        )
        assert result["status"] == "done"
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_DONE
        assert row.artifact_kind == "hf_file"
        assert row.artifact_license is None  # unpinned -> no license
        assert row.resolved_digest == f"sha256:{_sha(payload)}"
        assert (
            model_cache.hf_install_path(art, root=tmp_path / "cache").read_bytes()
            == payload
        )
    finally:
        queue.close()


def test_handler_dispatches_opus_mt_ref_to_artifact_backend(tmp_path):
    """End-to-end through the registered model.pull handler: an opus-mt: ref
    routes to the artifact backend (not the ollama daemon flow)."""
    from frisket.engine.jobs.model_pull import register_model_pull_handler
    from frisket.engine.jobs.worker import HandlerRegistry

    root = tmp_path / "ws"
    root.mkdir()
    queue = open_queue(workspace=root)
    try:
        engine = queue.engine
        files = {"model.bin": b"disp-weights", "source.spm": b"disp-spm"}
        entry = _pair_entry(files)
        bodies = {f"{_BASE}/en-es/{n}": d for n, d in files.items()}

        registry = HandlerRegistry()
        register_model_pull_handler(
            registry,
            workspace_root=root,
            queue=queue,
            client_factory=lambda: _client(_transport(bodies)),
            artifact_cache_root=tmp_path / "cache",
            artifact_manifest_lookup=lambda ref: (
                entry if ref == "opus-mt:en-es" else None
            ),
        )
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(root), model_ref="opus-mt:en-es"
        )
        job_id = queue.enqueue(
            "model.pull",
            {"pull_id": row.id, "workspace_root": str(root)},
            max_attempts=3,
        )
        store.set_job_id(engine, row.id, job_id=job_id)
        queue.claim("w1")
        handler = registry.get("model.pull")
        result = handler(
            {"pull_id": row.id, "workspace_root": str(root), "job_id": job_id},
            JobHandlerContext.without_job_row(),
        )
        assert result["status"] == "done"
        fresh = store.get(engine, row.id)
        assert fresh.status == store.STATUS_DONE
        assert fresh.artifact_kind == "ct2_pair"
    finally:
        queue.close()


# --- fast path hashes content rather than trusting presence and size -------


def test_fast_path_rehashes_and_rejects_tampered_install(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        files = {"model.bin": b"correct-weights"}
        entry = _pair_entry(files)
        pull_id = _make_row(engine, "opus-mt:en-es")
        art = normalize_artifact_ref("opus-mt:en-es")
        # Pre-install a TAMPERED file at the SAME size (presence+size would pass).
        install = model_cache.artifact_install_dir(art, root=tmp_path / "cache")
        install.mkdir(parents=True)
        (install / "model.bin").write_bytes(b"tampered-weight")  # same length
        assert len(b"tampered-weight") == len(b"correct-weights")

        # The re-download serves the CORRECT bytes.
        bodies = {f"{_BASE}/en-es/model.bin": b"correct-weights"}
        client = _client(_transport(bodies))
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=_no_cancel,
            is_final_attempt=False,
            manifest_lookup=lambda ref: entry,
            cache_root=tmp_path / "cache",
        )
        # Did NOT trust the tampered install; re-downloaded and installed clean.
        assert result == {"status": "done"}  # not already_installed
        assert (install / "model.bin").read_bytes() == b"correct-weights"
        row = store.get(engine, pull_id)
        assert row.resolved_digest == entry.composite_digest
    finally:
        queue.close()


def test_fast_path_hashes_clean_install_and_short_circuits(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        files = {"model.bin": b"weights", "source.spm": b"spm"}
        entry = _pair_entry(files)
        pull_id = _make_row(engine, "opus-mt:en-es")
        art = normalize_artifact_ref("opus-mt:en-es")
        install = model_cache.artifact_install_dir(art, root=tmp_path / "cache")
        install.mkdir(parents=True)
        for n, d in files.items():
            (install / n).write_bytes(d)

        def _boom(request):
            raise AssertionError(
                "a clean, correctly-hashing install must not re-download"
            )

        client = httpx.Client(transport=httpx.MockTransport(_boom))
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=_no_cancel,
            is_final_attempt=False,
            manifest_lookup=lambda ref: entry,
            cache_root=tmp_path / "cache",
        )
        assert result == {"status": "done", "already_installed": True}
    finally:
        queue.close()


# --- truncated Content-Length mismatch is retryable ------------------------


def test_truncated_unpinned_download_is_rejected_retryable(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        ref = "hf:owner/repo@abc123/model.gguf"
        pull_id = _make_row(engine, ref)
        store.mark_running(engine, pull_id, job_id=1)
        art = normalize_artifact_ref(ref)

        # Server declares 100 bytes but sends only 10 (truncated stream).
        def handler(request):
            return httpx.Response(
                200, content=b"0123456789", headers={"content-length": "100"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        from frisket.engine.jobs.model_pull import ModelPullTerminalError

        with pytest.raises(ModelPullTerminalError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,  # attempts remain -> stays active
                manifest_lookup=lambda ref: None,
                cache_root=tmp_path / "cache",
                unpinned_acknowledged=True,
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_RUNNING
        assert row.error_code == "artifact_source_unreachable"
        # nothing promoted
        assert not model_cache.hf_install_path(art, root=tmp_path / "cache").exists()
    finally:
        queue.close()


# --- byte-range resume -----------------------------------------------------


def _hf_entry(name: str, data: bytes) -> PinnedArtifact:
    url = f"{_BASE}/gguf/{name}"
    return PinnedArtifact(
        ref=f"hf:owner/repo@rev1/{name}",
        kind="hf_file",
        display_name="Model",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/owner/repo@rev1",
        files=(PinnedFile(name, url, _sha(data), len(data)),),
    )


def test_resume_appends_from_partial_via_range_206(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        full = bytes(range(256)) * 8  # 2048 bytes
        entry = _hf_entry("model.gguf", full)
        ref = "hf:owner/repo@rev1/model.gguf"
        art = normalize_artifact_ref(ref)
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref=ref
        )
        cache = tmp_path / "cache"
        # Pre-seed a partial download of the first 800 bytes.
        staging = model_cache.tmp_dir(row.id, root=cache)
        staging.mkdir(parents=True)
        (staging / "model.gguf").write_bytes(full[:800])

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            rng = request.headers.get("range")
            seen["range"] = rng
            assert rng == "bytes=800-", rng  # resumes from the partial length
            start = 800
            return httpx.Response(
                206,
                content=full[start:],
                headers={
                    "content-range": f"bytes {start}-{len(full) - 1}/{len(full)}",
                    "content-length": str(len(full) - start),
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=row.id,
            art=art,
            should_cancel=lambda: False,
            is_final_attempt=False,
            manifest_lookup=lambda r: entry,
            cache_root=cache,
        )
        assert result["status"] == "done"
        assert seen["range"] == "bytes=800-"
        installed = model_cache.hf_install_path(art, root=cache)
        assert installed.read_bytes() == full  # partial + resumed == full
        assert store.get(engine, row.id).resolved_digest == f"sha256:{_sha(full)}"
    finally:
        queue.close()


def test_resume_falls_back_to_full_rewrite_on_200(tmp_path):
    # Server ignores the Range header and returns 200 full -> the code must
    # rewrite the whole file and re-hash from scratch (not append onto the
    # partial, which would corrupt the file).
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        full = b"COMPLETE-BODY-VALUE-1234567890"
        entry = _hf_entry("m.gguf", full)
        ref = "hf:owner/repo@rev1/m.gguf"
        art = normalize_artifact_ref(ref)
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref=ref
        )
        cache = tmp_path / "cache"
        staging = model_cache.tmp_dir(row.id, root=cache)
        staging.mkdir(parents=True)
        (staging / "m.gguf").write_bytes(b"STALE-PARTIAL")  # wrong prefix

        def handler(request: httpx.Request) -> httpx.Response:
            # Ignore Range; return the whole file with 200.
            return httpx.Response(
                200, content=full, headers={"content-length": str(len(full))}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=row.id,
            art=art,
            should_cancel=lambda: False,
            is_final_attempt=False,
            manifest_lookup=lambda r: entry,
            cache_root=cache,
        )
        assert result["status"] == "done"
        assert model_cache.hf_install_path(art, root=cache).read_bytes() == full
    finally:
        queue.close()


def test_truncated_resume_keeps_partial_for_next_attempt(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        full = b"X" * 1000
        entry = _hf_entry("m.gguf", full)
        ref = "hf:owner/repo@rev1/m.gguf"
        art = normalize_artifact_ref(ref)
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref=ref
        )
        store.mark_running(engine, row.id, job_id=1)
        cache = tmp_path / "cache"

        def handler(request: httpx.Request) -> httpx.Response:
            # Declare 1000 total but only send 400 (truncated), 200 fresh.
            return httpx.Response(
                200, content=b"X" * 400, headers={"content-length": "1000"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        from frisket.engine.jobs.model_pull import ModelPullTerminalError

        with pytest.raises(ModelPullTerminalError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=row.id,
                art=art,
                should_cancel=lambda: False,
                is_final_attempt=False,
                manifest_lookup=lambda r: entry,
                cache_root=cache,
            )
        row2 = store.get(engine, row.id)
        assert row2.status == store.STATUS_RUNNING  # retryable, still active
        assert row2.error_code == "artifact_source_unreachable"
        # the partial is KEPT (not discarded) so the next attempt resumes
        partial = model_cache.tmp_dir(row.id, root=cache) / "m.gguf"
        assert partial.exists() and partial.stat().st_size == 400
    finally:
        queue.close()


# --- hf_snapshot: thin delegation to huggingface_hub.snapshot_download -----
#
# These tests parse REAL pinned refs (the parser's manifest allowlist means an
# arbitrary hf-snapshot ref never becomes an ArtifactRef at all), proving the
# route -> parse -> handler path end-to-end rather than hand-constructing refs
# the parser could never produce.


def _snapshot_ref_and_entry():
    entry = artifact_manifest.parakeet_vad_artifact()
    assert entry is not None and entry.hf_snapshot is not None
    return normalize_artifact_ref(entry.ref), entry


def test_hf_snapshot_delegates_to_snapshot_download(tmp_path, monkeypatch):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        art, entry = _snapshot_ref_and_entry()
        assert art.canonical == entry.ref
        pull_id = _make_row(engine, art.canonical)

        calls = []

        def fake_snapshot_download(repo_id, **kwargs):
            calls.append({"repo_id": repo_id, **kwargs})
            return str(tmp_path / "snapshot")

        import huggingface_hub

        monkeypatch.setattr(
            huggingface_hub, "snapshot_download", fake_snapshot_download
        )

        def _boom(request):
            raise AssertionError("hf_snapshot must not stream bytes itself")

        client = httpx.Client(transport=httpx.MockTransport(_boom))

        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=_no_cancel,
            is_final_attempt=False,
            cache_root=tmp_path / "cache",
        )
        assert result == {"status": "done"}
        snapshot = entry.hf_snapshot
        # `cache_dir` joined this exact-kwargs assertion deliberately, and it
        # is not a cosmetic addition: without it huggingface_hub resolves the
        # destination ITSELF, by a rule that is not frisket's
        # (`huggingface_hub_cache()` goes HF_HUB_CACHE ->
        # HUGGINGFACE_HUB_CACHE -> HF_HOME/hub -> ~/.cache/huggingface/hub;
        # the library inserts XDG_CACHE_HOME under HF_HOME's fallback). The
        # kwarg dict was pinned here while nothing anywhere asserted the pull
        # wrote where the readers read, so the divergence was invisible --
        # see test_hf_snapshot_pull_lands_where_the_liveness_probe_looks below,
        # which is the assertion that actually matters.
        assert calls == [
            {
                "repo_id": snapshot.repo_id,
                "revision": snapshot.revision,
                "cache_dir": huggingface_hub_cache(),
                "allow_patterns": list(snapshot.files),
                "token": False,
            }
        ]
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_DONE
        assert row.artifact_kind == "hf_snapshot"
        assert row.artifact_license == entry.license
        assert row.resolved_digest == (
            f"hf-revision:{snapshot.repo_id}@{snapshot.revision}"
        )
        # no model_cache install dir is created for a thin-delegated snapshot
        assert not model_cache.tmp_dir(pull_id, root=tmp_path / "cache").exists()
    finally:
        queue.close()


def test_hf_snapshot_pull_lands_where_the_liveness_probe_looks(tmp_path, monkeypatch):
    """The pull must write into the SAME Hub cache every reader reads.

    Nothing asserted this before, and the pull was the one hf_snapshot site
    that did not pass ``cache_dir=``: it let huggingface_hub resolve the
    destination by the library's own rule while the local-onnx liveness probe
    (``execution/definitions.parakeet_artifacts_present``), the Parakeet
    resolver child, the faster-whisper worker and ``operability/diagnostics``
    all read ``huggingface_hub_cache()``. Under XDG_CACHE_HOME with no HF_*
    variable set (a plain pip or bare install) those two answers differ, so
    Parakeet/Silero/whisper-base -- every ``hf_snapshot`` ref, i.e. all of
    them -- downloaded ~600MB, marked the row done, and left the Models page
    showing installed models that routing could not see: ``parakeet-tdt``
    kept going to the paid gateway.

    The fake stands in for huggingface_hub's downloader only. What it does
    NOT stand in for is the thing under test: it materializes the snapshot in
    the directory the handler NAMES, and refuses to guess one, so a handler
    that stops naming it fails here rather than quietly writing elsewhere.
    """
    for name in ("HF_HOME", "HUGGINGFACE_HUB_CACHE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))

    from frisket.execution.definitions import parakeet_artifacts_present

    assert parakeet_artifacts_present() is False

    def fake_snapshot_download(repo_id, **kwargs):
        assert "cache_dir" in kwargs, (
            "the pull must NAME the hub cache; letting huggingface_hub "
            "resolve its own puts the bytes where no reader looks"
        )
        snapshot = (
            Path(kwargs["cache_dir"])
            / f"models--{repo_id.replace('/', '--')}"
            / "snapshots"
            / kwargs["revision"]
        )
        for relpath in kwargs["allow_patterns"]:
            target = snapshot / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"pinned")
        return str(snapshot)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("hf_snapshot must not stream bytes itself")
                )
            )
        )
        for entry in (
            artifact_manifest.parakeet_model_artifact(),
            artifact_manifest.parakeet_vad_artifact(),
        ):
            assert entry is not None and entry.hf_snapshot is not None
            art = normalize_artifact_ref(entry.ref)
            result = run_artifact_pull(
                client,
                engine=engine,
                pull_id=_make_row(engine, art.canonical),
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                cache_root=tmp_path / "cache",
            )
            assert result == {"status": "done"}
        # The completed pull is what makes the local ONNX venue live -- the
        # exact claim the local-onnx liveness remedy makes to the user.
        assert parakeet_artifacts_present() is True
    finally:
        queue.close()


def test_hf_snapshot_not_in_manifest_is_terminal(tmp_path):
    """Defense in depth behind the parser's allowlist: if the manifest lookup
    misses at RUN time (a manifest edit between enqueue and execution), the
    handler still terminal-fails rather than free-form downloading."""
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        art, _entry = _snapshot_ref_and_entry()
        pull_id = _make_row(engine, art.canonical)
        client = _client(_transport({}))
        from frisket.engine.jobs.worker import NonRetryableJobError

        with pytest.raises(NonRetryableJobError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                manifest_lookup=lambda ref: None,
                cache_root=tmp_path / "cache",
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_FAILED
        assert row.error_code == "manifest_missing"
    finally:
        queue.close()


def test_hf_snapshot_cancel_before_start_short_circuits(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        art, _entry = _snapshot_ref_and_entry()
        pull_id = _make_row(engine, art.canonical)
        client = _client(_transport({}))

        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=pull_id,
            art=art,
            should_cancel=lambda: True,
            is_final_attempt=False,
            cache_root=tmp_path / "cache",
        )
        assert result == {"status": "cancelled"}
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_CANCELLED
    finally:
        queue.close()


def test_hf_snapshot_download_error_is_retryable(tmp_path, monkeypatch):
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        art, _entry = _snapshot_ref_and_entry()
        pull_id = _make_row(engine, art.canonical)
        store.mark_running(engine, pull_id, job_id=1)
        client = _client(_transport({}))

        import huggingface_hub

        def boom(repo_id, **kwargs):
            raise OSError("network unreachable")

        monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
        from frisket.engine.jobs.model_pull import ModelPullTerminalError

        with pytest.raises(ModelPullTerminalError):
            run_artifact_pull(
                client,
                engine=engine,
                pull_id=pull_id,
                art=art,
                should_cancel=_no_cancel,
                is_final_attempt=False,
                cache_root=tmp_path / "cache",
            )
        row = store.get(engine, pull_id)
        assert row.status == store.STATUS_RUNNING  # retryable, not finalized
        assert row.error_code == "artifact_source_unreachable"
    finally:
        queue.close()
