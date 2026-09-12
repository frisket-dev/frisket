from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from frisket_models.transcription import TranscribeOptions
from frisket_models.transcription.parakeet_tdt import PARAKEET_DESCRIPTOR

from frisket_parakeet_tdt.adapter import (
    DESCRIPTOR,
    ENGINE,
    ParakeetConfig,
    ParakeetRuntime,
    _default_runtime_factory,
    _merge_speaker_turns,
    _sortformer_turns,
    _word_segments,
    build_registration,
)


class FakeClock:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class FakeRecognizer:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[Any, int]] = []

    def recognize(self, waveform: Any, *, sample_rate: int) -> Any:
        self.calls.append((waveform, sample_rate))
        return self.result


class FakeModel:
    def __init__(self) -> None:
        self.vad_recognizer = FakeRecognizer(
            [
                SimpleNamespace(start=0.0, end=0.8, text=" hello"),
                SimpleNamespace(start=0.8, end=1.4, text="world "),
            ]
        )
        self.timestamp_recognizer = FakeRecognizer(
            SimpleNamespace(
                text="hello world",
                tokens=["▁hello", "▁world"],
                timestamps=[0.0, 0.5],
            )
        )
        self.vad_calls: list[tuple[Any, int]] = []

    def with_vad(self, vad: Any, *, batch_size: int) -> FakeRecognizer:
        self.vad_calls.append((vad, batch_size))
        return self.vad_recognizer

    def with_timestamps(self) -> FakeRecognizer:
        return self.timestamp_recognizer


class FakeDiarizer:
    def __init__(self, predictions: Any) -> None:
        self.predictions = predictions
        self.calls: list[tuple[list[str], int]] = []

    def diarize(self, *, audio: list[str], batch_size: int) -> Any:
        self.calls.append((audio, batch_size))
        return self.predictions


def _registration(
    model: FakeModel,
    *,
    diarizer: FakeDiarizer | None = None,
    clock: FakeClock | None = None,
):
    return build_registration(
        ParakeetConfig(Path("/fixed/models")),
        runtime_factory=lambda _config: ParakeetRuntime(model, "vad"),
        diarizer_factory=lambda _config: diarizer or FakeDiarizer([]),
        dependency_probe=lambda _config: (True, None),
        clock=clock or FakeClock(1.0, 1.2),
    )


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFFmock")
    return path


def test_leaf_uses_exact_code_owned_descriptor() -> None:
    assert DESCRIPTOR is PARAKEET_DESCRIPTOR


def test_default_runtime_uses_pinned_v3_int8_cpu_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    fake_onnx_asr = ModuleType("onnx_asr")

    def load_model(*args: Any, **kwargs: Any) -> object:
        calls["model"] = (args, kwargs)
        return object()

    def load_vad(*args: Any, **kwargs: Any) -> object:
        calls["vad"] = (args, kwargs)
        return object()

    fake_onnx_asr.load_model = load_model  # type: ignore[attr-defined]
    fake_onnx_asr.load_vad = load_vad  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnx_asr", fake_onnx_asr)

    config = ParakeetConfig(Path("/fixed/models"))
    runtime = _default_runtime_factory(config)

    assert runtime.model is not None and runtime.vad is not None
    assert calls["model"] == (
        ("nemo-parakeet-tdt-0.6b-v3",),
        {
            "path": config.asr_path,
            "quantization": "int8",
            "providers": ["CPUExecutionProvider"],
        },
    )
    assert calls["vad"] == (
        ("silero",),
        {"path": config.vad_path, "providers": ["CPUExecutionProvider"]},
    )


def test_probe_does_not_construct_runtime_or_diarizer() -> None:
    calls: list[str] = []
    registration = build_registration(
        ParakeetConfig(Path("/fixed/models")),
        runtime_factory=lambda _config: calls.append("runtime"),  # type: ignore[arg-type,return-value]
        diarizer_factory=lambda _config: calls.append("diarizer"),
        dependency_probe=lambda _config: (True, None),
    )

    assert registration.probe().model_dump() == {
        "available": True,
        "loaded": False,
        "error": None,
    }
    assert calls == []


def test_default_vad_path_returns_pinned_provenance(tmp_path: Path) -> None:
    model = FakeModel()
    registration = _registration(model, clock=FakeClock(10.0, 10.25))

    result = registration.factory().transcribe(_audio(tmp_path), TranscribeOptions())

    assert model.vad_calls == [("vad", 1)]
    assert result.engine == ENGINE
    assert result.text == "hello world"
    assert result.language is None
    assert result.duration == 1.4
    assert result.model_ids == DESCRIPTOR.model_ids
    assert result.revision == DESCRIPTOR.revision
    assert result.device == "cpu"
    assert result.dtype == "int8"
    assert result.timings == {"adapter.asr_seconds": 0.25}
    assert result.accepted_options == {}
    assert all(segment.speaker is None for segment in result.segments)


def test_diarization_resegments_words_and_labels_every_segment(tmp_path: Path) -> None:
    model = FakeModel()
    diarizer = FakeDiarizer([["0.00 0.40 speaker_8", "0.45 1.00 speaker_2"]])
    registration = _registration(
        model,
        diarizer=diarizer,
        clock=FakeClock(2.0, 2.1, 3.0, 3.4),
    )
    options = TranscribeOptions(vad=False, diarize=True)

    result = registration.factory().transcribe(_audio(tmp_path), options)

    assert [segment.text for segment in result.segments] == ["hello", "world"]
    assert [segment.speaker for segment in result.segments] == ["S1", "S2"]
    assert result.warnings == []
    assert result.device == "cpu+cuda:0"
    assert result.timings == {
        "adapter.asr_seconds": pytest.approx(0.1),
        "adapter.diarization_seconds": pytest.approx(0.4),
    }
    assert result.accepted_options == {"diarize": True, "vad": False}
    assert diarizer.calls == [([str(tmp_path / "input.wav")], 1)]


def test_diarization_uses_unknown_for_unmatched_segments(tmp_path: Path) -> None:
    model = FakeModel()
    diarizer = FakeDiarizer([["10.0 11.0 speaker_0"]])
    registration = _registration(
        model,
        diarizer=diarizer,
        clock=FakeClock(1.0, 1.1, 2.0, 2.2),
    )

    result = registration.factory().transcribe(
        _audio(tmp_path), TranscribeOptions(diarize=True)
    )

    assert [segment.speaker for segment in result.segments] == ["UNKNOWN", "UNKNOWN"]
    assert all(segment.speaker_confidence == "unknown" for segment in result.segments)
    assert len(result.warnings) == 1
    assert "UNKNOWN" in result.warnings[0]


def test_unsupported_options_fail_before_inference(tmp_path: Path) -> None:
    registration = _registration(FakeModel())

    with pytest.raises(ValueError, match="unsupported transcription option.*language"):
        registration.factory().transcribe(
            _audio(tmp_path), TranscribeOptions(language="fr")
        )


def test_word_tail_is_positive_and_turn_shapes_are_normalized() -> None:
    words = _word_segments(["▁hello", "▁world"], [0.0, 0.5])
    assert words[-1]["end"] > words[-1]["start"]
    assert _sortformer_turns(
        [["0.00 1.20 speaker_9", (1.2, 2.0, "speaker_2"), "bad"]]
    ) == [
        {"speaker": "S1", "start": 0.0, "end": 1.2},
        {"speaker": "S2", "start": 1.2, "end": 2.0},
    ]


def test_majority_overlap_unions_duplicate_turns() -> None:
    segments = [{"start": 0.0, "end": 10.0, "text": "long"}]
    turns = [
        {"speaker": "S1", "start": 0.0, "end": 4.0},
        {"speaker": "S1", "start": 0.0, "end": 4.0},
        {"speaker": "S2", "start": 4.0, "end": 9.0},
    ]

    merged, warnings = _merge_speaker_turns(segments, turns, has_word_timestamps=False)

    assert merged[0]["speaker"] == "S2"
    assert merged[0]["speaker_confidence"] == "approximate"
    assert len(warnings) == 1


def test_factory_failure_is_sticky_and_redacted() -> None:
    secret = "private-model-path"
    registration = build_registration(
        ParakeetConfig(Path("/fixed/models")),
        runtime_factory=lambda _config: (_ for _ in ()).throw(RuntimeError(secret)),
        dependency_probe=lambda _config: (True, None),
    )

    with pytest.raises(RuntimeError, match="Parakeet model failed to load") as exc:
        registration.factory()
    assert secret not in str(exc.value)
    assert registration.probe().error == "Parakeet model failed to load"
