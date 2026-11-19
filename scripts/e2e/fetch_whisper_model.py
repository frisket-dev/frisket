"""Download the pinned Whisper base model into the Hugging Face cache.

Usage:
    python scripts/e2e/fetch_whisper_model.py

The seeded "Council audio" project transcribes with the local faster-whisper
engine, whose worker runs without network and loads only the pinned Whisper
base snapshot from the cache ``huggingface_hub_cache()`` names. An
environment synced with the `standard` extra has faster-whisper but not the
model. This fetches the manifest's exact revision and files, the same
snapshot the in-app setup downloads, and exits nonzero unless the worker's
lookup then finds it.
"""

from __future__ import annotations

import sys

from huggingface_hub import snapshot_download

from frisket.ai.models.artifact_manifest import whisper_base_artifact
from frisket.engine._workers.parakeet_artifacts import (
    cached_whisper_path,
    huggingface_hub_cache,
)


def main() -> int:
    pinned = whisper_base_artifact()
    if pinned is None or pinned.hf_snapshot is None:
        print("the pinned Whisper base manifest entry is missing", file=sys.stderr)
        return 1
    snapshot = pinned.hf_snapshot
    snapshot_download(
        snapshot.repo_id,
        revision=snapshot.revision,
        cache_dir=huggingface_hub_cache(),
        allow_patterns=list(snapshot.files),
        token=False,
    )
    path = cached_whisper_path()
    print(f"{pinned.ref}: {'present' if path else 'missing'} ({path})")
    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
