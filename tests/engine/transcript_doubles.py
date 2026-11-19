"""A canned faster-whisper transcript for the transcript-to-extract chain tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import frisket.sdk.ops.transcribe_engines as transcribe_engines

TRANSCRIPT_SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "welcome to the show"},
    {"start": 2.0, "end": 4.0, "text": "today we discuss the election"},
    {"start": 4.0, "end": 6.0, "text": "some say the vote was rigged"},
    {"start": 6.0, "end": 8.0, "text": "others question the integrity of the count"},
    {"start": 8.0, "end": 10.0, "text": "officials deny any wrongdoing"},
    {"start": 10.0, "end": 12.0, "text": "thanks for listening"},
]


def fake_faster_whisper(blob_hash: str):
    """A ``FasterWhisperAdapter.transcribe`` that returns TRANSCRIPT_SEGMENTS.

    It asserts the materialized media path is named by ``blob_hash``.
    """

    async def fake(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        assert Path(path).name == blob_hash
        return {
            "text": " ".join(seg["text"] for seg in TRANSCRIPT_SEGMENTS),
            "segments": TRANSCRIPT_SEGMENTS,
            "language": "en",
            "duration": TRANSCRIPT_SEGMENTS[-1]["end"],
        }

    return fake
