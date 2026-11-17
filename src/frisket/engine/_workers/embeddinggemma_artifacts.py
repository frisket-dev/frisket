"""Passive readiness for the pinned, optional local retrieval model."""

from __future__ import annotations

from pathlib import Path

from frisket.ai.models.artifact_manifest import PinnedArtifact, lookup
from frisket.engine._workers.embeddinggemma_model import MODEL_REF
from frisket.engine._workers.parakeet_artifacts import cached_pinned_snapshot
from frisket.runtime.embeddinggemma_install import is_installed


def embeddinggemma_artifact() -> PinnedArtifact:
    artifact = lookup(MODEL_REF)
    if artifact is None or artifact.hf_snapshot is None:
        raise RuntimeError("The search model is missing from the artifact catalog.")
    return artifact


def cached_embeddinggemma_path() -> Path | None:
    return cached_pinned_snapshot(embeddinggemma_artifact())


def embeddinggemma_ready() -> bool:
    return is_installed() and cached_embeddinggemma_path() is not None
