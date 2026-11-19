"""fetch_whisper_model.py fetches exactly the snapshot the worker looks up."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.ai.models.artifact_manifest import whisper_base_artifact
from scripts.e2e import fetch_whisper_model


@pytest.mark.parametrize(("cached", "code"), [(Path("/hub/base"), 0), (None, 1)])
def test_fetches_the_pinned_revision_into_the_worker_cache(monkeypatch, cached, code):
    calls = []
    monkeypatch.setattr(
        fetch_whisper_model,
        "snapshot_download",
        lambda repo_id, **kwargs: calls.append((repo_id, kwargs)),
    )
    monkeypatch.setattr(
        fetch_whisper_model, "huggingface_hub_cache", lambda: Path("/hub")
    )
    monkeypatch.setattr(fetch_whisper_model, "cached_whisper_path", lambda: cached)

    assert fetch_whisper_model.main() == code

    pinned = whisper_base_artifact()
    assert pinned is not None and pinned.hf_snapshot is not None
    snapshot = pinned.hf_snapshot
    assert calls == [
        (
            snapshot.repo_id,
            {
                "revision": snapshot.revision,
                "cache_dir": Path("/hub"),
                "allow_patterns": list(snapshot.files),
                "token": False,
            },
        )
    ]
