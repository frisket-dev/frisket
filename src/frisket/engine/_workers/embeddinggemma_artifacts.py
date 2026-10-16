"""Passive readiness for the pinned, optional local retrieval model."""

from __future__ import annotations

from pathlib import Path

from frisket.ai.models.artifact_manifest import PinnedArtifact, lookup
from frisket.engine._workers.embeddinggemma_model import MODEL_REF
from frisket.engine._workers.parakeet_artifacts import (
    ParakeetArtifactUnavailable,
    _validate_snapshot,
    huggingface_hub_cache,
)
from frisket.runtime.embeddinggemma_install import is_installed


def embeddinggemma_artifact() -> PinnedArtifact:
    artifact = lookup(MODEL_REF)
    if artifact is None or artifact.hf_snapshot is None:
        raise RuntimeError("The search model is missing from the artifact catalog.")
    return artifact


def cached_embeddinggemma_path() -> Path | None:
    snapshot = embeddinggemma_artifact().hf_snapshot
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


def embeddinggemma_ready() -> bool:
    return is_installed() and cached_embeddinggemma_path() is not None
