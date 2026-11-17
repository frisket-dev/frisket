"""Passive readiness for the optional Indra OCR runtime and weights."""

from __future__ import annotations

from pathlib import Path

from frisket.ai.models.artifact_manifest import PinnedArtifact, lookup
from frisket.engine._workers.indra_ocr_model import MODEL_REF
from frisket.engine._workers.parakeet_artifacts import cached_pinned_snapshot
from frisket.runtime.indra_ocr_install import is_installed


def indra_ocr_artifact() -> PinnedArtifact:
    artifact = lookup(MODEL_REF)
    if artifact is None or artifact.hf_snapshot is None:
        raise RuntimeError("Indra OCR's pinned model is missing from the catalog.")
    return artifact


def cached_indra_ocr_path() -> Path | None:
    return cached_pinned_snapshot(indra_ocr_artifact())


def indra_ocr_runtime_present() -> bool:
    return is_installed()


__all__ = [
    "cached_indra_ocr_path",
    "indra_ocr_artifact",
    "indra_ocr_runtime_present",
]
