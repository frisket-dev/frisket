from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from frisket_models.transcription import TranscribeOptions, TranscriptionInputError
from frisket_models.transcription.vibevoice_asr import VIBEVOICE_ASR_DESCRIPTOR

from frisket_vibevoice_asr.adapter import (
    _PINNED_SHARD_FILENAMES,
    ENGINE,
    MODEL_ID,
    MODEL_REVISION,
    TRANSFORMERS_VERSION,
    VibeVoiceAsrAdapter,
    VibeVoiceAsrConfig,
    _decode_audio,
    _normalise_segments,
    _pinned_snapshot_path,
    _snapshot_is_present,
    _validated_waveform,
    build_registration,
)


class FakeClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class FakeProcessor:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.tokenizer = type("Tokenizer", (), {"eos_token_id": 5})()
        self.requests: list[dict[str, Any]] = []
        self.decode_calls: list[dict[str, Any]] = []
        self.inputs: FakeInputs | None = None

    def apply_transcription_request(self, **kwargs: Any) -> FakeInputs:
        self.requests.append(kwargs)
        self.inputs = FakeInputs(input_ids=FakeTensor([[1, 2, 3]]))
        return self.inputs

    def decode(self, _ids: Any, **kwargs: Any) -> list[Any]:
        self.decode_calls.append(kwargs)
        return [self.parsed]


class FakeTensor:
    def __init__(self, values: list[list[int]]) -> None:
        self.values = values
        self.shape = (len(values), len(values[0]))

    def __getitem__(self, item: Any) -> FakeTensor:
        assert item[0] == slice(None)
        return FakeTensor([row[item[1]] for row in self.values])

    def tolist(self) -> list[list[int]]:
        return self.values

    def to(self, *_args: Any, **_kwargs: Any) -> FakeTensor:
        return self


class FakeInputs(dict[str, FakeTensor]):
    def __init__(self, *, input_ids: FakeTensor) -> None:
        super().__init__(input_ids=input_ids)
        self.move_calls: list[tuple[object, object]] = []

    def to(self, device: object, dtype: object) -> FakeInputs:
        self.move_calls.append((device, dtype))
        return self


class FakeModel:
    device = "cuda:0"
    dtype = "bfloat16"

    def __init__(self, *, output: list[list[int]] | None = None) -> None:
        self.output = FakeTensor(output or [[1, 2, 3, 4, 5]])
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> FakeTensor:
        self.calls.append(kwargs)
        return self.output


class FakeRuntime:
    def __init__(self, processor: FakeProcessor, model: FakeModel) -> None:
        self.processor = processor
        self.model = model
        self.eos_token_id = processor.tokenizer.eos_token_id


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFFmock")
    return path


def test_identity_is_the_shared_code_owned_descriptor() -> None:
    assert ENGINE == "vibevoice-asr"
    assert VIBEVOICE_ASR_DESCRIPTOR.engine == ENGINE
    assert VIBEVOICE_ASR_DESCRIPTOR.model_ids == [MODEL_ID]
    assert VIBEVOICE_ASR_DESCRIPTOR.revision == MODEL_REVISION


def test_adapter_passes_context_to_native_api_and_normalises_speaker_zero(
    tmp_path: Path,
) -> None:
    processor = FakeProcessor(
        [
            {"Start": 0, "End": 0.5, "Speaker": 0, "Content": "Hello"},
            {"Start": 0.5, "End": 1.25, "Speaker": "Speaker 7", "Content": "world"},
        ]
    )
    model = FakeModel()
    adapter = VibeVoiceAsrAdapter(
        VibeVoiceAsrConfig(model_cache=Path("/models/hf")),
        runtime_factory=lambda _config: FakeRuntime(processor, model),
        audio_decoder=lambda _path: [0.0, 0.1] * 24_000,
        clock=FakeClock(10.0, 10.25),
    )

    result = adapter.transcribe(
        _audio(tmp_path), TranscribeOptions(context="Names: Ana")
    )

    assert len(processor.requests) == 1
    assert processor.requests[0]["prompt"] == "Names: Ana"
    assert processor.requests[0]["audio"].dtype == np.float32
    assert processor.requests[0]["audio"].shape == (48_000,)
    assert processor.inputs is not None
    assert processor.inputs.move_calls == [("cuda:0", "bfloat16")]
    assert len(model.calls) == 1
    assert model.calls[0]["input_ids"].shape == (1, 3)
    assert model.calls[0] | {"input_ids": None} == {
        "input_ids": None,
        "do_sample": False,
        "max_new_tokens": 32768,
        "use_cache": True,
        "acoustic_tokenizer_chunk_size": 1_440_000,
    }
    assert processor.decode_calls == [{"return_format": "parsed"}]
    assert result.text == "Hello world"
    assert [
        (segment.speaker, segment.start, segment.end) for segment in result.segments
    ] == [
        ("S1", 0.0, 0.5),
        ("S2", 0.5, 1.25),
    ]
    assert result.language is None
    assert result.duration == 2.0
    assert result.dtype == "bfloat16"
    assert result.accepted_options == {"context": "Names: Ana"}
    assert result.timings == {"adapter.inference_seconds": 0.25}


@pytest.mark.parametrize(
    "parsed",
    [
        "not structured output",
        [{"Start": 0, "End": 1, "Content": "missing speaker"}],
        [{"Start": 0, "End": float("nan"), "Speaker": 0, "Content": "bad"}],
        [
            {"Start": 2, "End": 3, "Speaker": 0, "Content": "late"},
            {"Start": 1, "End": 2, "Speaker": 1, "Content": "early"},
        ],
    ],
)
def test_native_malformed_or_partial_output_fails_closed(parsed: Any) -> None:
    with pytest.raises(ValueError, match="VibeVoice-ASR returned"):
        _normalise_segments(parsed, decoded_duration=10)


def test_labelled_acoustic_tags_are_preserved_as_segment_text() -> None:
    segments = _normalise_segments(
        [{"Start": 0, "End": 2, "Speaker": "Speaker 0", "Content": "[Music]"}],
        decoded_duration=2,
    )

    assert segments[0].speaker == "S1"
    assert segments[0].text == "[Music]"


def test_native_output_rejects_non_scalar_speaker_and_timestamps_after_audio() -> None:
    with pytest.raises(ValueError, match="invalid speaker"):
        _normalise_segments(
            [{"Start": 0, "End": 1, "Speaker": {"id": 0}, "Content": "bad"}],
            decoded_duration=2,
        )
    with pytest.raises(ValueError, match="past decoded audio"):
        _normalise_segments(
            [{"Start": 0, "End": 3, "Speaker": 0, "Content": "late"}],
            decoded_duration=1,
        )


def test_snapshot_probe_requires_actual_assets_and_every_indexed_shard(
    tmp_path: Path,
) -> None:
    config = VibeVoiceAsrConfig(model_cache=tmp_path)
    snapshot = _pinned_snapshot_path(config)
    snapshot.mkdir(parents=True)
    for filename in (
        "config.json",
        "generation_config.json",
        "processor_config.json",
        "chat_template.jinja",
        "tokenizer.json",
        "tokenizer_config.json",
    ):
        (snapshot / filename).write_text("{}")
    (snapshot / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "weight_map": {
                    f"layer.{index}": filename
                    for index, filename in enumerate(sorted(_PINNED_SHARD_FILENAMES))
                }
            }
        )
    )
    first, *remaining = sorted(_PINNED_SHARD_FILENAMES)
    (snapshot / first).write_bytes(b"one")

    assert _snapshot_is_present(config) is False

    for filename in remaining:
        (snapshot / filename).write_bytes(b"shard")
    assert _snapshot_is_present(config) is True


def test_decode_uses_a_one_sample_guard_without_silent_cropping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append(args)
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, stdout="3600")
        return subprocess.CompletedProcess(
            args, 0, stdout=np.array([0.0], dtype=np.float32).tobytes()
        )

    monkeypatch.setattr("frisket_vibevoice_asr.adapter.subprocess.run", fake_run)

    assert _decode_audio(_audio(tmp_path)).tolist() == [0.0]
    assert calls[1][calls[1].index("-t") + 1] == "3600.000041667"


def test_decode_resamples_a_real_stereo_wav_to_24khz_mono(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(b"\x00\x00\x00\x00" * 48_000)

    waveform = _decode_audio(path)

    assert waveform.dtype == np.float32
    assert waveform.ndim == 1
    assert waveform.size == 24_000


@pytest.mark.parametrize(
    "waveform, error",
    [
        ([], "empty"),
        ([0.0, float("nan")], "non-finite"),
        (np.zeros((1, 2), dtype=np.float32), "empty or invalid"),
    ],
)
def test_adapter_boundary_rejects_empty_or_nonfinite_waveform(
    waveform: Any, error: str
) -> None:
    with pytest.raises(TranscriptionInputError, match=error):
        _validated_waveform(waveform)


def test_eos_terminated_empty_native_output_is_valid_silence(tmp_path: Path) -> None:
    processor = FakeProcessor([])
    adapter = VibeVoiceAsrAdapter(
        VibeVoiceAsrConfig(model_cache=Path("/models/hf")),
        runtime_factory=lambda _config: FakeRuntime(processor, FakeModel()),
        audio_decoder=lambda _path: [0.0] * 24_000,
        clock=FakeClock(1.0, 1.1),
    )

    result = adapter.transcribe(_audio(tmp_path), TranscribeOptions())

    assert result.text == ""
    assert result.segments == []
    assert result.duration == 1.0


def test_rejects_audio_over_one_hour_without_prefix_cropping(tmp_path: Path) -> None:
    adapter = VibeVoiceAsrAdapter(
        VibeVoiceAsrConfig(model_cache=Path("/models/hf")),
        runtime_factory=lambda _config: FakeRuntime(FakeProcessor([]), FakeModel()),
        audio_decoder=lambda _path: [0.0] * (24_000 * 60 * 60 + 1),
    )

    with pytest.raises(TranscriptionInputError, match="60 minutes") as exc:
        adapter.transcribe(_audio(tmp_path), TranscribeOptions())
    assert exc.value.status_code == 413


def test_generation_without_eos_is_never_decoded(tmp_path: Path) -> None:
    processor = FakeProcessor(
        [{"Start": 0, "End": 1, "Speaker": 0, "Content": "would be partial"}]
    )
    adapter = VibeVoiceAsrAdapter(
        VibeVoiceAsrConfig(model_cache=Path("/models/hf")),
        runtime_factory=lambda _config: FakeRuntime(
            processor, FakeModel(output=[[1, 2, 3, 4]])
        ),
        audio_decoder=lambda _path: [0.0],
    )

    with pytest.raises(ValueError, match="without EOS"):
        adapter.transcribe(_audio(tmp_path), TranscribeOptions())
    assert processor.decode_calls == []


def test_config_refuses_model_drift_cpu_and_invalid_chunk_size() -> None:
    with pytest.raises(ValueError, match="MODEL_ID is fixed"):
        VibeVoiceAsrConfig.from_environ({"FRISKET_VIBEVOICE_ASR_MODEL_ID": "other"})
    with pytest.raises(ValueError, match="GPU-only"):
        VibeVoiceAsrConfig.from_environ({"FRISKET_VIBEVOICE_ASR_DEVICE": "cpu"})
    with pytest.raises(ValueError, match="multiple of 3200"):
        VibeVoiceAsrConfig.from_environ(
            {"FRISKET_VIBEVOICE_ASR_ACOUSTIC_TOKENIZER_CHUNK_SIZE": "3201"}
        )


def test_probe_is_lazy_and_factory_failure_is_redacted() -> None:
    registration = build_registration(
        VibeVoiceAsrConfig(model_cache=Path("/models/hf")),
        runtime_factory=lambda _config: (_ for _ in ()).throw(
            RuntimeError("private path")
        ),
        dependency_probe=lambda _config: (True, None),
    )

    assert registration.probe().model_dump() == {
        "available": True,
        "loaded": False,
        "error": None,
    }
    with pytest.raises(RuntimeError, match="VibeVoice-ASR model failed to load") as exc:
        registration.factory()
    assert "private path" not in str(exc.value)
    assert registration.probe().error == "VibeVoice-ASR model failed to load"


def test_runtime_probe_checks_the_exact_transformers_pin_and_gpu() -> None:
    from frisket_vibevoice_asr.adapter import _configured_runtime_probe

    config = VibeVoiceAsrConfig(model_cache=Path("/models/hf"))
    assert _configured_runtime_probe(
        config,
        version_lookup=lambda _name: TRANSFORMERS_VERSION,
        snapshot_probe=lambda _config: True,
        cuda_probe=lambda _config: (True, None),
    ) == (True, None)
    assert _configured_runtime_probe(
        config,
        version_lookup=lambda _name: "0.0.0",
        snapshot_probe=lambda _config: True,
        cuda_probe=lambda _config: (True, None),
    ) == (False, "transformers package version does not match the worker pin")
