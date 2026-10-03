"""Passive readiness for the optional classifier runtime and pinned weights."""

from __future__ import annotations

from pathlib import Path

from frisket.ai.models.artifact_manifest import PinnedArtifact, lookup
from frisket.contracts.classification import LOCAL_CLASSIFIERS
from frisket.engine._workers.parakeet_artifacts import (
    ParakeetArtifactUnavailable,
    _validate_snapshot,
    huggingface_hub_cache,
)
from frisket.runtime.classifier_install import is_installed


def classifier_artifact(engine_id: str) -> PinnedArtifact:
    spec = LOCAL_CLASSIFIERS[engine_id]
    artifact = lookup(f"hf-snapshot:{spec.model_repo}@{spec.revision}")
    if artifact is None or artifact.hf_snapshot is None:
        raise RuntimeError("The classifier's pinned model is missing from the catalog.")
    return artifact


def cached_classifier_path(engine_id: str) -> Path | None:
    snapshot = classifier_artifact(engine_id).hf_snapshot
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


def classifier_runtime_present() -> bool:
    return is_installed()


def classifier_ready(engine_id: str) -> bool:
    return (
        classifier_runtime_present() and cached_classifier_path(engine_id) is not None
    )
