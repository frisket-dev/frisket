"""The offline convert pipeline's pure helpers + an end-to-end proof that its
emitted manifest entry drives the real artifact pull path against
locally-produced artifacts, with no torch and no network."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import httpx

from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.artifact_pull import run_artifact_pull
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.queue import open_queue
from frisket.ai.models import model_cache
from frisket.ai.models.artifact_manifest import PinnedArtifact, PinnedFile

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "convert_opus_mt.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("convert_opus_mt", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ct2_dir(directory: Path) -> dict[str, bytes]:
    directory.mkdir(parents=True)
    files = {
        "model.bin": b"fake-ct2-weights-0123456789",
        "config.json": b'{"model_type":"marian"}',
        "shared_vocabulary.json": b'["a","b","c"]',
        "source.spm": b"source-sp-model",
        "target.spm": b"target-sp-model",
    }
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return files


def test_checksum_dir_and_emit_entry_are_deterministic(tmp_path):
    mod = _load_script()
    directory = tmp_path / "opus-mt-en-es-ct2"
    files = _make_ct2_dir(directory)

    checks = mod.checksum_dir(directory)
    assert [c[0] for c in checks] == sorted(files.keys())
    for relpath, sha, size in checks:
        assert sha == hashlib.sha256(files[relpath]).hexdigest()
        assert size == len(files[relpath])

    entry_src = mod.emit_manifest_entry("en", "es", "abc123", checks)
    assert 'ref="opus-mt:en-es"' in entry_src
    assert 'kind="ct2_pair"' in entry_src
    assert "resolve/abc123/model.bin" in entry_src


def test_emitted_entry_drives_the_real_pull_path(tmp_path):
    """End-to-end proof: the script's emitted manifest entry, evaluated back
    into a PinnedArtifact, pulls successfully against the locally-produced
    bytes served at the emitted source URLs -- the operator step that remains
    is uploading these exact bytes to the frisket-models HF org."""
    mod = _load_script()
    directory = tmp_path / "opus-mt-en-es-ct2"
    files = _make_ct2_dir(directory)
    checks = mod.checksum_dir(directory)
    entry_src = mod.emit_manifest_entry("en", "es", "deadbeef", checks)

    # Evaluate the emitted literal into a PinnedArtifact.
    namespace: dict = {"PinnedArtifact": PinnedArtifact, "PinnedFile": PinnedFile}
    exec(f"ENTRY = {{\n{entry_src}\n}}", namespace)  # noqa: S102 -- trusted generated literal
    entry = namespace["ENTRY"]["opus-mt:en-es"]
    assert isinstance(entry, PinnedArtifact)

    # Serve the locally-produced bytes at each pinned source URL.
    by_url = {f.source: files[f.repo_relpath] for f in entry.files}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in by_url:
            return httpx.Response(200, content=by_url[url])
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    queue = open_queue(workspace=tmp_path)
    try:
        engine = queue.engine
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="opus-mt:en-es"
        )
        art = normalize_artifact_ref("opus-mt:en-es")
        result = run_artifact_pull(
            client,
            engine=engine,
            pull_id=row.id,
            art=art,
            should_cancel=lambda: False,
            is_final_attempt=False,
            manifest_lookup=lambda ref: entry if ref == "opus-mt:en-es" else None,
            cache_root=tmp_path / "cache",
        )
        assert result["status"] == "done"
        fresh = store.get(engine, row.id)
        assert fresh.resolved_digest == entry.composite_digest
        install = model_cache.artifact_install_dir(art, root=tmp_path / "cache")
        for name, data in files.items():
            assert (install / name).read_bytes() == data
    finally:
        queue.close()
