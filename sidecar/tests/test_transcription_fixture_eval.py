from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    GatewayTranscriptionResponse,
    TranscribeResult,
    TranscribeSegment,
    WorkerTranscriptionResponse,
)

_PATH = Path(__file__).resolve().parents[1] / "scripts/transcription_fixture_eval.py"
_SPEC = importlib.util.spec_from_file_location("fixture_eval", _PATH)
assert _SPEC and _SPEC.loader
evaluator = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = evaluator
_SPEC.loader.exec_module(evaluator)

AUDIO = b"RIFF pinned overlap fixture"
TURNS = [
    {
        "start": 0.0,
        "end": 2.0,
        "speaker": "A",
        "text": "alpha [laugh]",
        "kind": "mixed",
    },
    {"start": 0.5, "end": 0.75, "speaker": "B", "text": "", "kind": "gap"},
    {"start": 1.0, "end": 3.0, "speaker": "B", "text": "beta", "kind": "speech"},
    {"start": 3.0, "end": 4.0, "speaker": "A", "text": "ending", "kind": "speech"},
]


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.wav"
    path.write_bytes(AUDIO)
    return path


def _manifest(audio_hash: str | None = None) -> bytes:
    return json.dumps(
        {
            "schema": evaluator.SCHEMA,
            "source": {"ignored_provenance": True},
            "clip": {
                "sha256": audio_hash or hashlib.sha256(AUDIO).hexdigest(),
                "duration_seconds": 4.0,
            },
            "reference": {"turns": TURNS},
        }
    ).encode()


def _response(flattened: bool, gateway: bool) -> bytes:
    segments = [
        TranscribeSegment(
            start=0.0, end=1.0 if flattened else 2.0, text="alpha", speaker="S02"
        ),
        TranscribeSegment(start=1.0, end=3.0, text="beta", speaker="S01"),
        TranscribeSegment(start=3.0, end=4.0, text="ending", speaker="S02"),
    ]
    result = TranscribeResult(
        engine="moss",
        text=" ".join(item.text for item in segments),
        segments=segments,
        duration=4.0,
        model_ids=["OpenMOSS-Team/MOSS-Transcribe-Diarize"],
        revision="4a1af868018e7974197f4f018730758012b28c27",
        device="cuda",
        dtype="bfloat16",
        timings={},
        warnings=[],
        accepted_options={},
    )
    envelope = (
        GatewayTranscriptionResponse(
            contract_version=CONTRACT_VERSION, results=[result]
        )
        if gateway
        else WorkerTranscriptionResponse(
            contract_version=CONTRACT_VERSION, result=result
        )
    )
    return envelope.model_dump_json().encode()


@pytest.mark.parametrize("gateway", [False, True])
def test_permuted_overlap_scores_zero_without_leaking_text(
    tmp_path: Path, gateway: bool
) -> None:
    artifact = evaluator.evaluate(
        _manifest(), _response(flattened=False, gateway=gateway), _audio(tmp_path)
    )

    assert artifact["structural"]["passed"] is True
    assert artifact["structural"]["reference_segment_count"] == 3
    assert artifact["metrics"]["wer"]["rate"] == 0.0
    assert artifact["metrics"]["cpcer"]["rate"] == 0.0
    assert artifact["metrics"]["der"]["rate"] == 0.0
    assert not any(word in json.dumps(artifact) for word in ("alpha", "beta", "ending"))


def test_flattened_overlap_fails_and_bad_audio_hash_is_rejected(tmp_path: Path) -> None:
    artifact = evaluator.evaluate(
        _manifest(), _response(flattened=True, gateway=False), _audio(tmp_path)
    )
    assert artifact["structural"]["passed"] is False
    assert "no_cross_speaker_overlap" in artifact["structural"]["issues"]

    out_of_bounds = json.loads(_response(flattened=False, gateway=False))
    out_of_bounds["result"]["segments"][-1]["end"] = 10000.0
    out_of_bounds["result"]["duration"] = 10000.0
    artifact = evaluator.evaluate(
        _manifest(), json.dumps(out_of_bounds).encode(), _audio(tmp_path)
    )
    assert "timestamp_outside_fixture" in artifact["structural"]["issues"]

    spans = [
        TranscribeSegment(start=0.0, end=1.5, text="a", speaker="S01"),
        TranscribeSegment(start=1.0, end=2.0, text="b", speaker="S01"),
    ]
    assert evaluator._intervals(spans, 4.0) == {"S01": [(0.0, 2.0)]}

    with pytest.raises(evaluator.EvaluationError, match="SHA-256"):
        evaluator.evaluate(
            _manifest("0" * 64),
            _response(flattened=False, gateway=False),
            _audio(tmp_path),
        )
