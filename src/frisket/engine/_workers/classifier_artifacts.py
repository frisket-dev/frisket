"""Passive readiness for the optional classifier runtime and pinned weights."""

from __future__ import annotations

from pathlib import Path

from frisket.ai.models.artifact_manifest import PinnedArtifact, lookup
from frisket.contracts.classification import LOCAL_CLASSIFIERS
from frisket.engine._workers.parakeet_artifacts import cached_pinned_snapshot
from frisket.runtime.classifier_install import is_installed


def classifier_artifact(engine_id: str) -> PinnedArtifact:
    spec = LOCAL_CLASSIFIERS[engine_id]
    artifact = lookup(f"hf-snapshot:{spec.model_repo}@{spec.revision}")
    if artifact is None or artifact.hf_snapshot is None:
        raise RuntimeError("The classifier's pinned model is missing from the catalog.")
    return artifact


def cached_classifier_path(engine_id: str) -> Path | None:
    return cached_pinned_snapshot(classifier_artifact(engine_id))


def classifier_runtime_present() -> bool:
    return is_installed()


def classifier_ready(engine_id: str) -> bool:
    return (
        classifier_runtime_present() and cached_classifier_path(engine_id) is not None
    )
