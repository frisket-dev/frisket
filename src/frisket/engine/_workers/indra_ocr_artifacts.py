"""Passive readiness for the optional Indra OCR runtime and weights."""

from __future__ import annotations

from pathlib import Path

from frisket.ai.models.artifact_manifest import PinnedArtifact, lookup
from frisket.engine._workers.indra_ocr_model import MODEL_REF
from frisket.engine._workers.parakeet_artifacts import (
    ParakeetArtifactUnavailable,
    _validate_snapshot,
    huggingface_hub_cache,
)
from frisket.runtime.indra_ocr_install import is_installed


def indra_ocr_artifact() -> PinnedArtifact:
    artifact = lookup(MODEL_REF)
    if artifact is None or artifact.hf_snapshot is None:
        raise RuntimeError("Indra OCR's pinned model is missing from the catalog.")
    return artifact


def cached_indra_ocr_path() -> Path | None:
    snapshot = indra_ocr_artifact().hf_snapshot
    assert snapshot is not None
    cache = huggingface_hub_cache()
    path = (
        cache
        / f"models--{snapshot.repo_id.replace('/', '--')}"
        / "snapshots"
        / snapshot.revision
    )
    try:
        return _validate_snapshot(
            path,
            cache_dir=cache,
            repo_id=snapshot.repo_id,
            revision=snapshot.revision,
            files=snapshot.files,
        )
    except (OSError, ParakeetArtifactUnavailable):
        return None


def indra_ocr_runtime_present() -> bool:
    return is_installed()


def indra_ocr_ready() -> bool:
    return indra_ocr_runtime_present() and cached_indra_ocr_path() is not None


__all__ = [
    "cached_indra_ocr_path",
    "indra_ocr_artifact",
    "indra_ocr_ready",
    "indra_ocr_runtime_present",
]
