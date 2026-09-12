#!/usr/bin/env python3
"""Populate the installed beta's pinned ASR snapshots during networked setup."""

from __future__ import annotations

import json
import sys
from pathlib import Path


resources_python = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(resources_python))

from frisket.ai.models import artifact_manifest  # noqa: E402
from frisket.engine._workers.parakeet_artifacts import (  # noqa: E402
    _resolve_snapshot,
    _snapshot_payload,
    huggingface_hub_cache,
)


def main() -> None:
    cache_dir = huggingface_hub_cache()
    resolved: dict[str, str] = {}
    for label, getter in (
        ("whisper", artifact_manifest.whisper_base_artifact),
        ("parakeet", artifact_manifest.parakeet_model_artifact),
        ("vad", artifact_manifest.parakeet_vad_artifact),
    ):
        entry = getter()
        if entry is None or entry.hf_snapshot is None:
            raise RuntimeError(f"the pinned {label} snapshot is missing")
        snapshot = entry.hf_snapshot
        snapshot_path = _resolve_snapshot(
            _snapshot_payload(
                repo_id=snapshot.repo_id,
                revision=snapshot.revision,
                files=snapshot.files,
                cache_dir=cache_dir,
            )
        )
        resolved[label] = Path(snapshot_path).name
    print(json.dumps(resolved, separators=(",", ":")))


main()
