"""Native, fixed VibeVoice-ASR adapter for transcription contract v1.

The heavy Transformers imports and model construction are deliberately lazy:
the authenticated worker can report readiness without allocating model weights.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from frisket_models.transcription import (
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscriptionInputError,
)
from frisket_models.transcription.vibevoice_asr import (
    VIBEVOICE_ASR_DESCRIPTOR as DESCRIPTOR,
)
from frisket_models.transcription.vibevoice_asr import (
    VIBEVOICE_ASR_ENGINE as ENGINE,
)
from frisket_models.transcription.vibevoice_asr import (
    VIBEVOICE_ASR_MODEL_ID as MODEL_ID,
)
from frisket_models.transcription.vibevoice_asr import (
    VIBEVOICE_ASR_MODEL_REVISION as MODEL_REVISION,
)
from frisket_models.transcription.vibevoice_asr import (
    VIBEVOICE_ASR_PACKAGE_VERSION as TRANSFORMERS_VERSION,
)

_ENV_PREFIX = "FRISKET_VIBEVOICE_ASR_"
_FIXED_MODEL_ID_ENV = f"{_ENV_PREFIX}MODEL_ID"
_FIXED_MODEL_REVISION_ENV = f"{_ENV_PREFIX}MODEL_REVISION"
_DEVICE_ENV = f"{_ENV_PREFIX}DEVICE"
_DEVICE_INDEX_ENV = f"{_ENV_PREFIX}DEVICE_INDEX"
_MODEL_CACHE_ENV = f"{_ENV_PREFIX}MODEL_CACHE"
_LOCAL_FILES_ONLY_ENV = f"{_ENV_PREFIX}LOCAL_FILES_ONLY"
_CHUNK_SIZE_ENV = f"{_ENV_PREFIX}ACOUSTIC_TOKENIZER_CHUNK_SIZE"

_SAMPLE_RATE = 24_000
_MAX_DURATION_SECONDS = 60 * 60
_MAX_SAMPLES = _SAMPLE_RATE * _MAX_DURATION_SECONDS
_FFMPEG_GUARD_SECONDS = _MAX_DURATION_SECONDS + (1 / _SAMPLE_RATE)
_MAX_NEW_TOKENS = 32_768
_DEFAULT_CHUNK_SIZE = 1_440_000
_CHUNK_SIZE_MULTIPLE = 3_200
_PINNED_SNAPSHOT_FILES = frozenset(
    {
        "config.json",
        "generation_config.json",
        "processor_config.json",
        "chat_template.jinja",
        "tokenizer.json",
        "tokenizer_config.json",
        "model.safetensors.index.json",
    }
)
_PINNED_SHARD_FILENAMES = frozenset(
    f"model-{index:05d}-of-00008.safetensors" for index in range(1, 9)
)
_TIMESTAMP_TOLERANCE_SECONDS = 1.0

Clock = Callable[[], float]
RuntimeFactory = Callable[["VibeVoiceAsrConfig"], "NativeRuntime"]
AudioDecoder = Callable[[Path], Sequence[float] | np.ndarray]
ConfiguredDependencyProbe = Callable[["VibeVoiceAsrConfig"], tuple[bool, str | None]]
VersionLookup = Callable[[str], str]
SnapshotProbe = Callable[["VibeVoiceAsrConfig"], bool]
CudaProbe = Callable[["VibeVoiceAsrConfig"], tuple[bool, str | None]]

_LOG = logging.getLogger(__name__)


def _strict_bool(name: str, raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _nonnegative_int(name: str, raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0 or raw.strip() != str(value):
        raise ValueError(f"{name} must be a canonical non-negative integer")
    return value


def _positive_multiple(name: str, raw: str, multiple: int) -> int:
    value = _nonnegative_int(name, raw)
    if value == 0:
        raise ValueError(f"{name} must be positive")
    if value % multiple:
        raise ValueError(f"{name} must be a multiple of {multiple}")
    return value


def _fixed_identity(environ: Mapping[str, str], name: str, expected: str) -> None:
    configured = environ.get(name)
    if configured is not None and configured != expected:
        raise ValueError(f"{name} is fixed at {expected!r} for this worker image")


@dataclass(frozen=True, slots=True)
class VibeVoiceAsrConfig:
    """Runtime placement and bounded tokenizer tuning for one pinned model."""

    device_index: int = 0
    model_cache: Path = Path("/models/hf")
    local_files_only: bool = True
    acoustic_tokenizer_chunk_size: int = _DEFAULT_CHUNK_SIZE

    def __post_init__(self) -> None:
        if self.device_index < 0:
            raise ValueError("device_index must be non-negative")
        if not self.model_cache.is_absolute():
            raise ValueError("model_cache must be an absolute path")
        if not self.local_files_only:
            raise ValueError("local_files_only must be true for this worker")
        if self.acoustic_tokenizer_chunk_size <= 0:
            raise ValueError("acoustic_tokenizer_chunk_size must be positive")
        if self.acoustic_tokenizer_chunk_size % _CHUNK_SIZE_MULTIPLE:
            raise ValueError(
                "acoustic_tokenizer_chunk_size must be a multiple of "
                f"{_CHUNK_SIZE_MULTIPLE}"
            )

    @property
    def device_label(self) -> str:
        return f"cuda:{self.device_index}"

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str] | None = None
    ) -> VibeVoiceAsrConfig:
        source = os.environ if environ is None else environ
        _fixed_identity(source, _FIXED_MODEL_ID_ENV, MODEL_ID)
        _fixed_identity(source, _FIXED_MODEL_REVISION_ENV, MODEL_REVISION)
        device = source.get(_DEVICE_ENV, "cuda").strip().lower()
        if device != "cuda":
            raise ValueError(f"{_DEVICE_ENV} must be cuda; VibeVoice-ASR is GPU-only")
        cache = source.get(_MODEL_CACHE_ENV, "/models/hf").strip()
        if not cache:
            raise ValueError(f"{_MODEL_CACHE_ENV} must not be empty")
        return cls(
            device_index=_nonnegative_int(
                _DEVICE_INDEX_ENV, source.get(_DEVICE_INDEX_ENV, "0")
            ),
            model_cache=Path(cache),
            local_files_only=_strict_bool(
                _LOCAL_FILES_ONLY_ENV, source.get(_LOCAL_FILES_ONLY_ENV, "true")
            ),
            acoustic_tokenizer_chunk_size=_positive_multiple(
                _CHUNK_SIZE_ENV,
                source.get(_CHUNK_SIZE_ENV, str(_DEFAULT_CHUNK_SIZE)),
                _CHUNK_SIZE_MULTIPLE,
            ),
        )


@dataclass(frozen=True, slots=True)
class NativeRuntime:
    processor: Any
    model: Any
    eos_token_id: int | None


def _pinned_snapshot_path(config: VibeVoiceAsrConfig) -> Path:
    repository = f"models--{MODEL_ID.replace('/', '--')}"
    return config.model_cache / repository / "snapshots" / MODEL_REVISION


def _snapshot_is_present(config: VibeVoiceAsrConfig) -> bool:
    snapshot = _pinned_snapshot_path(config)
    if not snapshot.is_dir() or not all(
        (snapshot / name).is_file() for name in _PINNED_SNAPSHOT_FILES
    ):
        return False
    try:
        index = json.loads((snapshot / "model.safetensors.index.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(index, dict) or not isinstance(index.get("weight_map"), dict):
        return False
    try:
        indexed_shards = frozenset(index["weight_map"].values())
    except TypeError:
        return False
    return indexed_shards == _PINNED_SHARD_FILENAMES and all(
        (snapshot / filename).is_file() for filename in indexed_shards
    )


def _configured_cuda_probe(config: VibeVoiceAsrConfig) -> tuple[bool, str | None]:
    try:
        import torch

        if not torch.cuda.is_available():
            return False, "CUDA is unavailable"
        if config.device_index >= torch.cuda.device_count():
            return False, "configured CUDA device is unavailable"
        if torch.cuda.get_device_capability(config.device_index) < (8, 0):
            return False, "configured CUDA device does not support bfloat16"
    except Exception:
        return False, "CUDA capability probe failed"
    return True, None


def _configured_runtime_probe(
    config: VibeVoiceAsrConfig,
    *,
    version_lookup: VersionLookup = importlib.metadata.version,
    snapshot_probe: SnapshotProbe = _snapshot_is_present,
    cuda_probe: CudaProbe = _configured_cuda_probe,
) -> tuple[bool, str | None]:
    try:
        installed_version = version_lookup("transformers")
    except importlib.metadata.PackageNotFoundError:
        return False, f"transformers=={TRANSFORMERS_VERSION} is not installed"
    except Exception:
        return False, "transformers package metadata could not be read"
    if installed_version != TRANSFORMERS_VERSION:
        return False, "transformers package version does not match the worker pin"
    if not snapshot_probe(config):
        return False, "pinned model snapshot is absent from the local cache"
    return cuda_probe(config)


def _default_runtime_factory(config: VibeVoiceAsrConfig) -> NativeRuntime:
    """Load exactly the local pinned snapshot after request admission."""

    import torch
    from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration

    common = {
        "revision": MODEL_REVISION,
        "cache_dir": str(config.model_cache),
        "local_files_only": True,
    }
    processor = AutoProcessor.from_pretrained(MODEL_ID, **common)
    model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
        MODEL_ID,
        dtype=torch.bfloat16,
        **common,
    )
    model.to(config.device_label)
    model.eval()
    eos_token_id = getattr(getattr(processor, "tokenizer", None), "eos_token_id", None)
    return NativeRuntime(processor=processor, model=model, eos_token_id=eos_token_id)


def _probe_duration(path: Path) -> float:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "--",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscriptionInputError("audio duration could not be read") from exc
    if completed.returncode != 0:
        raise TranscriptionInputError("audio duration could not be read")
    try:
        duration = float(completed.stdout.strip())
    except ValueError as exc:
        raise TranscriptionInputError("audio duration could not be read") from exc
    if not math.isfinite(duration) or duration < 0:
        raise TranscriptionInputError("audio duration is invalid")
    return duration


def _decode_audio(path: Path) -> np.ndarray:
    duration = _probe_duration(path)
    if duration > _MAX_DURATION_SECONDS:
        raise TranscriptionInputError(
            "audio exceeds the VibeVoice-ASR limit of 60 minutes", too_large=True
        )
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(_SAMPLE_RATE),
                "-t",
                f"{_FFMPEG_GUARD_SECONDS:.9f}",
                "-f",
                "f32le",
                "pipe:1",
            ],
            check=False,
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscriptionInputError("audio could not be decoded") from exc
    if completed.returncode != 0:
        raise TranscriptionInputError("audio could not be decoded")
    waveform = np.frombuffer(completed.stdout, dtype=np.float32)
    if waveform.size > _MAX_SAMPLES:
        raise TranscriptionInputError(
            "audio exceeds the VibeVoice-ASR limit of 60 minutes", too_large=True
        )
    return waveform


def _finite_timestamp(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"VibeVoice-ASR returned an invalid {field}")
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VibeVoice-ASR returned a non-numeric {field}") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"VibeVoice-ASR returned an invalid {field}")
    return timestamp


def _normalise_segments(
    parsed: object, *, decoded_duration: float
) -> list[TranscribeSegment]:
    if not isinstance(parsed, list):
        raise ValueError("VibeVoice-ASR returned malformed structured output")
    speakers: dict[str, str] = {}
    segments: list[TranscribeSegment] = []
    previous_start = 0.0
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError("VibeVoice-ASR returned malformed structured output")
        missing = {"Start", "End", "Speaker", "Content"} - set(item)
        if missing:
            raise ValueError("VibeVoice-ASR returned partial structured output")
        start = _finite_timestamp(item["Start"], field="segment start")
        end = _finite_timestamp(item["End"], field="segment end")
        if end < start or (index and start < previous_start):
            raise ValueError("VibeVoice-ASR returned invalid segment ordering")
        if end > decoded_duration + _TIMESTAMP_TOLERANCE_SECONDS:
            raise ValueError("VibeVoice-ASR returned a timestamp past decoded audio")
        content = item["Content"]
        if not isinstance(content, str):
            raise ValueError("VibeVoice-ASR returned non-text segment content")
        raw_speaker = item["Speaker"]
        if isinstance(raw_speaker, bool) or not isinstance(raw_speaker, (str, int)):
            raise ValueError("VibeVoice-ASR returned an invalid speaker")
        native_speaker = str(raw_speaker).strip()
        if not native_speaker:
            raise ValueError("VibeVoice-ASR returned an empty speaker")
        speaker = speakers.setdefault(native_speaker, f"S{len(speakers) + 1}")
        segments.append(
            TranscribeSegment(start=start, end=end, text=content, speaker=speaker)
        )
        previous_start = start
    return segments


def _move_inputs(inputs: Any, runtime: NativeRuntime) -> Any:
    return inputs.to(runtime.model.device, runtime.model.dtype)


def _completion_contains_eos(
    output_ids: Any, input_length: int, eos_token_id: int | None
) -> bool:
    if eos_token_id is None:
        return False
    generated = output_ids[:, input_length:]
    values = generated.tolist()
    return any(int(token) == eos_token_id for row in values for token in row)


def _validated_waveform(waveform: Sequence[float] | np.ndarray) -> np.ndarray:
    try:
        normalized = np.asarray(waveform, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise TranscriptionInputError("audio waveform is invalid") from exc
    if normalized.ndim != 1 or normalized.size == 0:
        raise TranscriptionInputError("audio waveform is empty or invalid")
    if not np.isfinite(normalized).all():
        raise TranscriptionInputError("audio waveform contains non-finite samples")
    if normalized.size > _MAX_SAMPLES:
        raise TranscriptionInputError(
            "audio exceeds the VibeVoice-ASR limit of 60 minutes", too_large=True
        )
    return normalized


class VibeVoiceAsrAdapter:
    def __init__(
        self,
        config: VibeVoiceAsrConfig,
        *,
        runtime_factory: RuntimeFactory = _default_runtime_factory,
        audio_decoder: AudioDecoder = _decode_audio,
        clock: Clock = time.perf_counter,
    ) -> None:
        self._config = config
        self._runtime = runtime_factory(config)
        self._audio_decoder = audio_decoder
        self._clock = clock

    def transcribe(
        self, audio_path: Path, options: TranscribeOptions
    ) -> TranscribeResult:
        DESCRIPTOR.validate_options(options)
        waveform = _validated_waveform(self._audio_decoder(audio_path))
        decoded_duration = waveform.size / _SAMPLE_RATE
        started = self._clock()
        inputs = self._runtime.processor.apply_transcription_request(
            audio=waveform, prompt=options.context
        )
        inputs = _move_inputs(inputs, self._runtime)
        input_ids = inputs["input_ids"]
        output_ids = self._runtime.model.generate(
            **inputs,
            do_sample=False,
            max_new_tokens=_MAX_NEW_TOKENS,
            use_cache=True,
            acoustic_tokenizer_chunk_size=self._config.acoustic_tokenizer_chunk_size,
        )
        if not _completion_contains_eos(
            output_ids, input_ids.shape[1], self._runtime.eos_token_id
        ):
            raise ValueError("VibeVoice-ASR generation ended without EOS")
        try:
            parsed = self._runtime.processor.decode(
                output_ids[:, input_ids.shape[1] :], return_format="parsed"
            )[0]
        except Exception as exc:
            raise ValueError(
                "VibeVoice-ASR returned malformed structured output"
            ) from exc
        segments = _normalise_segments(parsed, decoded_duration=decoded_duration)
        elapsed = self._clock() - started
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("VibeVoice-ASR returned an invalid inference timing")
        return TranscribeResult(
            engine=ENGINE,
            text=" ".join(segment.text.strip() for segment in segments).strip(),
            segments=segments,
            language=None,
            duration=decoded_duration,
            model_ids=[MODEL_ID],
            revision=MODEL_REVISION,
            device=self._config.device_label,
            dtype="bfloat16",
            timings={"adapter.inference_seconds": elapsed},
            warnings=[],
            accepted_options=options.supplied_options(),
        )


class _RegistrationState:
    def __init__(self) -> None:
        self._loaded = False
        self._load_error: str | None = None
        self._lock = threading.Lock()

    def loaded(self) -> None:
        with self._lock:
            self._loaded = True
            self._load_error = None

    def failed(self) -> None:
        with self._lock:
            self._load_error = "VibeVoice-ASR model failed to load"

    def snapshot(self) -> tuple[bool, str | None]:
        with self._lock:
            return self._loaded, self._load_error


def build_registration(
    config: VibeVoiceAsrConfig | None = None,
    *,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    audio_decoder: AudioDecoder = _decode_audio,
    dependency_probe: ConfiguredDependencyProbe = _configured_runtime_probe,
    clock: Clock = time.perf_counter,
    environ: Mapping[str, str] | None = None,
) -> AdapterRegistration:
    runtime_config = config or VibeVoiceAsrConfig.from_environ(environ)
    state = _RegistrationState()

    def factory() -> VibeVoiceAsrAdapter:
        try:
            adapter = VibeVoiceAsrAdapter(
                runtime_config,
                runtime_factory=runtime_factory,
                audio_decoder=audio_decoder,
                clock=clock,
            )
        except Exception:
            _LOG.exception("VibeVoice-ASR model factory failed")
            state.failed()
            raise RuntimeError("VibeVoice-ASR model failed to load") from None
        state.loaded()
        return adapter

    def probe() -> EngineProbe:
        loaded, load_error = state.snapshot()
        if load_error is not None:
            return EngineProbe(available=False, loaded=False, error=load_error)
        available, error = dependency_probe(runtime_config)
        return EngineProbe(available=available, loaded=loaded, error=error)

    return AdapterRegistration(descriptor=DESCRIPTOR, factory=factory, probe=probe)


REGISTRATION = build_registration()

__all__ = [
    "DESCRIPTOR",
    "ENGINE",
    "MODEL_ID",
    "MODEL_REVISION",
    "REGISTRATION",
    "TRANSFORMERS_VERSION",
    "VibeVoiceAsrAdapter",
    "VibeVoiceAsrConfig",
    "build_registration",
]
