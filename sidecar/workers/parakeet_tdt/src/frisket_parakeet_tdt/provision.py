"""Bake the three immutable model artifacts into the worker image."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from frisket_models.transcription.parakeet_tdt import (
    DIARIZER_FILENAME,
    DIARIZER_MODEL_ID,
    DIARIZER_MODEL_REVISION,
    PARAKEET_MODEL_ID,
    PARAKEET_MODEL_REVISION,
    PARAKEET_VAD_ID,
    PARAKEET_VAD_REVISION,
)
from huggingface_hub import hf_hub_download, snapshot_download

from .adapter import ASR_FILES, VAD_FILES

_MODEL_ROOT_ENV = "FRISKET_PARAKEET_TDT_MODEL_ROOT"


def main() -> None:
    root = Path(os.environ.get(_MODEL_ROOT_ENV, "/models/parakeet-tdt"))
    snapshot_download(
        repo_id=PARAKEET_MODEL_ID,
        revision=PARAKEET_MODEL_REVISION,
        local_dir=root / "asr",
        allow_patterns=list(ASR_FILES),
    )
    snapshot_download(
        repo_id=PARAKEET_VAD_ID,
        revision=PARAKEET_VAD_REVISION,
        local_dir=root / "vad",
        allow_patterns=list(VAD_FILES),
    )
    hf_hub_download(
        repo_id=DIARIZER_MODEL_ID,
        revision=DIARIZER_MODEL_REVISION,
        filename=DIARIZER_FILENAME,
        local_dir=root / "diarizer",
    )
    for cache in root.glob("**/.cache"):
        shutil.rmtree(cache)


if __name__ == "__main__":
    main()
