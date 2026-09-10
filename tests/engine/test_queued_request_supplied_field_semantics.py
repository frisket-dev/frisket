"""Queued transcription preserves supplied-versus-absent engine options.

Minimal valid typed requests retain queued placement without materializing
unsupported defaults as explicitly supplied parameters.
"""

from __future__ import annotations

import pytest

from frisket.engine.executor.queued_actions import (
    queued_v1_action_request,
)


def test_moss_media_transcribe_minimal_action_stays_queued() -> None:
    """A minimal moss transcribe action
    keeps its queued placement, and the synthetic ``vad``/``diarize`` defaults
    never register as supplied options (moss rejects supplied knobs)."""

    request = queued_v1_action_request(
        {
            "action_id": "media.transcribe",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {
                "source": "media",
                "engine": "moss",
            },
            "output_names": {"text": "transcript"},
            "idempotency_key": "media_transcribe@sha256:moss-queued-semantics",
        }
    )
    assert request is not None
    assert request.params.engine.root == "moss"
    assert {
        "vad",
        "diarize",
        "num_speakers",
        "min_speakers",
        "max_speakers",
    }.isdisjoint(request.params.model_fields_set)
    assert request.params.language == []


@pytest.mark.parametrize("engine", ["faster_whisper", "moss"])
def test_transcribe_queue_preserves_authorship_separately_from_effective_options(
    engine,
):
    base = {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"source": "media", "engine": engine},
        "idempotency_key": "transcribe-options",
    }
    request = queued_v1_action_request(base)
    assert request is not None
    spec = request.expected_runner_spec
    assert "vad" not in spec["params"]
    if engine == "moss":
        assert "vad" not in spec
        # An explicitly unsupported knob must not become silent inline fallback.
        assert (
            queued_v1_action_request(
                {**base, "params": {**base["params"], "vad": True}}
            )
            is None
        )
    else:
        assert "vad" not in spec  # workers own the omitted default
        for vad in (False, True):
            supplied = queued_v1_action_request(
                {**base, "params": {**base["params"], "vad": vad}}
            )
            assert supplied is not None
            assert supplied.expected_runner_spec["params"]["vad"] is vad
            assert supplied.expected_runner_spec["vad"] is vad
