"""Opt-in real-runtime smoke tests for local model integrations.

These tests download/load real model weights and run real local inference.
They are ``network`` and ``real`` marked, so the normal suite
deselects them. Each lane has its own explicit admission flag; without the
flag, selecting the markers produces a skip. Once a flag is set, missing
dependencies, input files, deployment configuration, or a broken network/cache
are failures, not skips.

Examples (install the matching optional extras first)::

    FRISKET_OPUS_MT_REAL=1 FRISKET_MODEL_CACHE_DIR=/abs/model-cache \
      uv run --extra translate pytest -m 'network and real' -q \
      tests/test_local_models_real.py

    FRISKET_HY_MT2_REAL=1 FRISKET_MODEL_CACHE_DIR=/abs/model-cache \
      uv run --extra translate-gguf pytest -m 'network and real' -q \
      tests/test_local_models_real.py

    FRISKET_HF_PULL_REAL=1 \
      uv run pytest -m 'network and real' -q tests/test_local_models_real.py

The translation cache is deliberately explicit because Hy-MT2 is about
1.1 GiB. Reusing the same directory exercises real inference against the
installed pinned bytes after the first pull.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import httpx
import pytest

from frisket.ops.integrations import hy_mt2, opus_mt
from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.artifact_pull import run_artifact_pull
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.queue import open_queue
from frisket.ai.models import artifact_manifest, model_cache

pytestmark = [pytest.mark.network, pytest.mark.real]


# A tiny bounded network fixture rather than caller-selected arbitrary bytes.
# These values deliberately duplicate the committed ``opus-mt:en-es`` manifest
# pin: if that pin changes, this test requires an intentional fixture update.
_HF_PULL_REF = (
    "hf:frisket-models/opus-mt-en-es-ct2@010363dc108c7a1ddf2a57880f1592b0584bce8c/"
    "config.json"
)
_HF_PULL_SOURCE = (
    "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/"
    "010363dc108c7a1ddf2a57880f1592b0584bce8c/config.json"
)
_HF_PULL_SHA256 = "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af"
_HF_PULL_SIZE = 223


def _enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def _required_path(name: str, *, directory: bool = False) -> Path:
    raw = os.environ.get(name)
    if not raw:
        pytest.fail(f"enabled real test requires {name} to be an absolute path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        pytest.fail(f"{name} must be absolute, got {raw!r}")
    if directory:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            pytest.fail(f"cannot create model cache {path}: {exc}")
        if not path.is_dir():
            pytest.fail(f"{name} is not a directory: {path}")
    elif not path.is_file():
        pytest.fail(f"{name} does not name a readable file: {path}")
    return path


def _model_cache_root() -> Path:
    return _required_path("FRISKET_MODEL_CACHE_DIR", directory=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_pinned_bytes(pinned, install: Path) -> None:
    for item in pinned.files:
        path = install / item.repo_relpath
        assert path.stat().st_size == item.size
        assert _sha256(path) == item.sha256


@pytest.mark.skipif(
    not _enabled("FRISKET_OPUS_MT_REAL"),
    reason="set FRISKET_OPUS_MT_REAL=1 and FRISKET_MODEL_CACHE_DIR",
)
def test_opus_mt_real_inference_and_installed_pin_integrity() -> None:
    cache_root = _model_cache_root()
    if not opus_mt.runtime_available():
        pytest.fail(opus_mt.REMEDIATION)

    pinned = artifact_manifest.lookup("opus-mt:en-es")
    assert pinned is not None
    opus_mt.ensure_pair_installed("en", "es", cache_root=cache_root)
    translated = opus_mt.translate_texts(
        "en", "es", ["Hello, world."], cache_root=cache_root
    )

    assert translated and translated[0].strip()
    assert "hola" in translated[0].casefold()
    install = model_cache.artifact_install_dir(
        normalize_artifact_ref(pinned.ref), root=cache_root
    )
    _assert_pinned_bytes(pinned, install)


@pytest.mark.skipif(
    not _enabled("FRISKET_HY_MT2_REAL"),
    reason="set FRISKET_HY_MT2_REAL=1 and FRISKET_MODEL_CACHE_DIR",
)
def test_hy_mt2_real_inference_and_installed_pin_integrity() -> None:
    cache_root = _model_cache_root()
    if not hy_mt2.runtime_available():
        pytest.fail(hy_mt2.REMEDIATION)

    pinned = artifact_manifest.hy_mt2_artifact()
    assert pinned is not None
    hy_mt2.ensure_installed(cache_root=cache_root)
    translated = hy_mt2.translate_texts(
        "Spanish", ["Hello, world."], cache_root=cache_root
    )

    assert translated and translated[0].strip()
    assert "hola" in translated[0].casefold()
    path = model_cache.hf_install_path(
        normalize_artifact_ref(pinned.ref), root=cache_root
    )
    assert path.stat().st_size == pinned.total_size
    _assert_pinned_bytes(pinned, path.parent)


@pytest.mark.skipif(
    not _enabled("FRISKET_HF_PULL_REAL"),
    reason="set FRISKET_HF_PULL_REAL=1",
)
def test_hugging_face_artifact_pull_records_tiny_pinned_fixture_bytes_and_provenance(
    tmp_path: Path,
) -> None:
    art = normalize_artifact_ref(_HF_PULL_REF)
    assert art.scheme == "hf"
    manifest = artifact_manifest.lookup("opus-mt:en-es")
    assert manifest is not None
    manifest_file = next(
        item for item in manifest.files if item.repo_relpath == "config.json"
    )
    assert manifest_file.source == _HF_PULL_SOURCE
    assert manifest_file.sha256 == _HF_PULL_SHA256
    assert manifest_file.size == _HF_PULL_SIZE

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache_root = tmp_path / "model-cache"
    queue = open_queue(workspace=workspace)
    try:
        row, created = store.create_or_get_active(
            queue.engine, workspace_root=str(workspace), model_ref=art.canonical
        )
        assert created
        with httpx.Client() as client:
            result = run_artifact_pull(
                client,
                engine=queue.engine,
                pull_id=row.id,
                art=art,
                should_cancel=lambda: False,
                is_final_attempt=True,
                cache_root=cache_root,
                unpinned_acknowledged=True,
            )

        assert result["status"] == "done"
        final = store.get(queue.engine, row.id)
        installed = model_cache.hf_install_path(art, root=cache_root)
        assert final.status == store.STATUS_DONE
        assert final.resolved_digest and re.fullmatch(
            r"sha256:[0-9a-f]{64}", final.resolved_digest
        )
        assert final.resolved_size == installed.stat().st_size == _HF_PULL_SIZE
        assert final.resolved_digest == f"sha256:{_HF_PULL_SHA256}"
        assert final.artifact_kind == "hf_file"
        assert final.artifact_source_url == _HF_PULL_SOURCE
        assert final.artifact_license is None
        assert final.artifact_manifest_version is None
    finally:
        queue.close()
