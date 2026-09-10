from __future__ import annotations

import hashlib

from frisket.sample_content.audio import SAMPLE_COUNCIL_AUDIO, council_audio_bytes


def test_sample_council_audio_matches_checked_in_provenance() -> None:
    assert len(SAMPLE_COUNCIL_AUDIO) == 2
    for item in SAMPLE_COUNCIL_AUDIO:
        content = council_audio_bytes(item.filename)
        assert len(content) > 1_000_000
        assert content.startswith(b"ID3")
        assert hashlib.sha256(content).hexdigest() == item.clip_sha256
        assert item.clip_end_seconds - item.clip_start_seconds == 300
        assert item.license == "CC0-1.0"
