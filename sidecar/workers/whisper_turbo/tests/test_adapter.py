from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from frisket_models.transcription import TranscribeOptions
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_DESCRIPTOR,
)

from frisket_whisper_turbo.adapter import (
    DESCRIPTOR,
    ENGINE,
    FASTER_WHISPER_VERSION,
    MODEL_ID,
    MODEL_REVISION,
    WhisperTurboConfig,
    _configured_runtime_probe,
    _pinned_snapshot_path,
    build_registration,
)


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(self, path: str, **kwargs: Any):
        self.calls.append((path, kwargs))

        def segments():
            yield SimpleNamespace(
                start=0.0,
                end=0.6,
                text=" Hello",
                words=[
                    SimpleNamespace(word=" Hello", start=0.0, end=0.3),
                ],
            )
            yield SimpleNamespace(
                start=0.6,
                end=1.2,
                text=" world",
                words=[
                    SimpleNamespace(word=" world", start=0.7, end=1.2),
                ],
            )

        return segments(), SimpleNamespace(language="en", duration=1.25)


class FakeClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class FakeRuntime:
    def __init__(
        self,
        *,
        cuda_devices: int = 1,
        supported: set[str] | None = None,
    ) -> None:
        self.cuda_devices = cuda_devices
        self.supported = supported or {"float16", "float32"}
        self.compute_calls: list[tuple[str, int]] = []

    def get_cuda_device_count(self) -> int:
        return self.cuda_devices

    def get_supported_compute_types(
        self, device: str, *, device_index: int
    ) -> set[str]:
        self.compute_calls.append((device, device_index))
        return self.supported


def test_leaf_uses_the_exact_code_owned_gateway_descriptor() -> None:
    assert DESCRIPTOR is WHISPER_TURBO_DESCRIPTOR


def test_registration_probe_never_constructs_or_imports_model_runtime() -> None:
    model = FakeModel()
    factory_calls: list[dict[str, Any]] = []

    def model_factory(**kwargs: Any) -> FakeModel:
        factory_calls.append(kwargs)
        return model

    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=model_factory,
        dependency_probe=lambda _config: (True, None),
        environ={},
    )

    assert "faster_whisper" not in sys.modules
    assert registration.probe().model_dump() == {
        "available": True,
        "loaded": False,
        "error": None,
    }
    assert factory_calls == []
    assert "faster_whisper" not in sys.modules

    registration.factory()
    assert registration.probe().loaded is True
    assert len(factory_calls) == 1


def test_factory_pins_identity_and_configures_only_runtime_placement() -> None:
    calls: list[dict[str, Any]] = []

    def model_factory(**kwargs: Any) -> FakeModel:
        calls.append(kwargs)
        return FakeModel()

    config = WhisperTurboConfig(
        device="cuda",
        device_index=2,
        compute_type="int8_float16",
        model_cache=Path("/var/models/turbo"),
        local_files_only=True,
    )
    registration = build_registration(
        config,
        model_factory=model_factory,
        dependency_probe=lambda _config: (True, None),
        environ={"HF_TOKEN": "test-only-token"},
    )

    registration.factory()

    assert calls == [
        {
            "model_size_or_path": MODEL_ID,
            "device": "cuda",
            "device_index": 2,
            "compute_type": "int8_float16",
            "download_root": "/var/models/turbo",
            "local_files_only": True,
            "revision": MODEL_REVISION,
            "num_workers": 1,
            "use_auth_token": "test-only-token",
        }
    ]


def test_adapter_emits_contract_result_with_words_timings_and_provenance(
    tmp_path: Path,
) -> None:
    model = FakeModel()
    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=lambda **_kwargs: model,
        dependency_probe=lambda _config: (True, None),
        clock=FakeClock(10.0, 10.25),
        environ={},
    )
    adapter = registration.factory()
    audio_path = tmp_path / "fixture.wav"
    audio_path.write_bytes(b"mock audio; never decoded")

    options = TranscribeOptions(language="en", vad=True, context="Frisket")
    result = adapter.transcribe(audio_path, options)

    assert model.calls == [
        (
            str(audio_path),
            {
                "language": "en",
                "vad_filter": True,
                "initial_prompt": "Frisket",
                "word_timestamps": True,
            },
        )
    ]
    assert result.engine == ENGINE
    assert result.text == "Hello world"
    assert result.language == "en"
    assert result.duration == 1.25
    assert result.model_ids == [MODEL_ID]
    assert result.revision == MODEL_REVISION
    assert result.device == "cuda:0"
    assert result.dtype == "float16"
    assert result.timings == {"adapter.inference_seconds": 0.25}
    assert result.accepted_options == {
        "context": "Frisket",
        "language": "en",
        "vad": True,
    }
    assert [
        word.word for segment in result.segments for word in segment.words or []
    ] == [
        " Hello",
        " world",
    ]
    assert all(segment.speaker is None for segment in result.segments)
    registration.descriptor.validate_result(result, options)


def test_omitted_vad_uses_declared_true_default(tmp_path: Path) -> None:
    model = FakeModel()
    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=lambda **_kwargs: model,
        dependency_probe=lambda _config: (True, None),
        clock=FakeClock(1.0, 1.1),
        environ={},
    )
    audio_path = tmp_path / "fixture.wav"
    audio_path.write_bytes(b"mock")

    result = registration.factory().transcribe(audio_path, TranscribeOptions())

    assert model.calls[0][1]["vad_filter"] is True
    assert result.accepted_options == {}


def test_adapter_rejects_undeclared_diarization_option() -> None:
    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=lambda **_kwargs: FakeModel(),
        dependency_probe=lambda _config: (True, None),
        environ={},
    )

    with pytest.raises(ValueError, match="unsupported transcription option.*diarize"):
        registration.factory().transcribe(
            Path("/unused.wav"), TranscribeOptions(diarize=True)
        )


def test_config_rejects_model_identity_drift() -> None:
    with pytest.raises(ValueError, match="MODEL_ID is fixed"):
        WhisperTurboConfig.from_environ(
            {"FRISKET_WHISPER_TURBO_MODEL_ID": "other/model"}
        )

    with pytest.raises(ValueError, match="MODEL_REVISION is fixed"):
        WhisperTurboConfig.from_environ(
            {"FRISKET_WHISPER_TURBO_MODEL_REVISION": "main"}
        )


def test_cpu_config_defaults_to_float32_unless_compute_is_explicit() -> None:
    device_env = {"FRISKET_WHISPER_TURBO_DEVICE": "cpu"}
    assert WhisperTurboConfig.from_environ(device_env).compute_type == "float32"
    assert (
        WhisperTurboConfig.from_environ(
            {
                **device_env,
                "FRISKET_WHISPER_TURBO_COMPUTE_TYPE": "int8",
            }
        ).compute_type
        == "int8"
    )


def test_config_defaults_to_offline_cache_only() -> None:
    assert WhisperTurboConfig().local_files_only is True
    assert WhisperTurboConfig.from_environ({}).local_files_only is True


def test_runtime_probe_validates_version_cuda_device_and_compute() -> None:
    runtime = FakeRuntime(cuda_devices=2, supported={"float16"})
    config = WhisperTurboConfig(
        device_index=1,
        compute_type="float16",
        local_files_only=False,
    )

    assert _configured_runtime_probe(
        config,
        version_lookup=lambda _name: FASTER_WHISPER_VERSION,
        runtime_loader=lambda: runtime,
    ) == (True, None)
    assert runtime.compute_calls == [("cuda", 1)]

    unavailable, error = _configured_runtime_probe(
        WhisperTurboConfig(device_index=2, local_files_only=False),
        version_lookup=lambda _name: FASTER_WHISPER_VERSION,
        runtime_loader=lambda: runtime,
    )
    assert unavailable is False
    assert error == "configured CUDA device is unavailable"

    unavailable, error = _configured_runtime_probe(
        WhisperTurboConfig(compute_type="int8", local_files_only=False),
        version_lookup=lambda _name: FASTER_WHISPER_VERSION,
        runtime_loader=lambda: runtime,
    )
    assert unavailable is False
    assert error == "configured compute type is unsupported on the selected device"


def test_runtime_probe_rejects_wrong_package_version() -> None:
    runtime = FakeRuntime()
    wrong_version, error = _configured_runtime_probe(
        WhisperTurboConfig(local_files_only=False),
        version_lookup=lambda _name: "1.2.0",
        runtime_loader=lambda: runtime,
    )
    assert wrong_version is False
    assert error == "faster-whisper package version does not match the worker pin"
    assert runtime.compute_calls == []


def test_offline_probe_accepts_complete_provisioned_pin_and_rejects_any_missing_file(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    config = WhisperTurboConfig(
        model_cache=tmp_path,
        local_files_only=True,
    )
    missing, error = _configured_runtime_probe(
        config,
        version_lookup=lambda _name: FASTER_WHISPER_VERSION,
        runtime_loader=lambda: runtime,
    )
    assert missing is False
    assert error == "pinned model snapshot is absent from the local cache"

    snapshot = _pinned_snapshot_path(config)
    snapshot.mkdir(parents=True)
    required_files = (
        "config.json",
        "model.bin",
        "preprocessor_config.json",
        "tokenizer.json",
        "vocabulary.json",
    )
    for name in required_files:
        (snapshot / name).write_bytes(b"mock")
    assert _configured_runtime_probe(
        config,
        version_lookup=lambda _name: FASTER_WHISPER_VERSION,
        runtime_loader=lambda: runtime,
    ) == (True, None)
    for name in required_files:
        (snapshot / name).unlink()
        assert _configured_runtime_probe(
            config,
            version_lookup=lambda _name: FASTER_WHISPER_VERSION,
            runtime_loader=lambda: runtime,
        ) == (False, "pinned model snapshot is absent from the local cache")
        (snapshot / name).write_bytes(b"mock")


def test_factory_failure_is_logged_but_public_probe_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-model-loader-detail"

    def fail_factory(**_kwargs: Any) -> FakeModel:
        raise RuntimeError(secret)

    registration = build_registration(
        WhisperTurboConfig(),
        model_factory=fail_factory,
        dependency_probe=lambda _config: (True, None),
        environ={},
    )

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as captured:
        registration.factory()

    assert secret not in str(captured.value)
    probe = registration.probe()
    assert probe.available is False
    assert probe.error == "Whisper Turbo model failed to load"
    assert secret not in probe.error
    assert "Whisper Turbo model factory failed" in caplog.text
