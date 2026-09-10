"""Fixed Whisper Turbo adapter for transcription contract v1.

The leaf fixes one model at one immutable revision. Importing it never
imports ``faster_whisper`` or constructs weights; those operations happen only
inside the adapter factory after the shared worker admits a request.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket_models.transcription import (
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscribeWord,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_DESCRIPTOR as DESCRIPTOR,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_ENGINE as ENGINE,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_MODEL_ID as MODEL_ID,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_MODEL_REVISION as MODEL_REVISION,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_PACKAGE_VERSION as FASTER_WHISPER_VERSION,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_VAD_DEFAULT,
)

_ENV_PREFIX = "FRISKET_WHISPER_TURBO_"
_FIXED_MODEL_ID_ENV = f"{_ENV_PREFIX}MODEL_ID"
_FIXED_MODEL_REVISION_ENV = f"{_ENV_PREFIX}MODEL_REVISION"
_DEVICE_ENV = f"{_ENV_PREFIX}DEVICE"
_DEVICE_INDEX_ENV = f"{_ENV_PREFIX}DEVICE_INDEX"
_COMPUTE_TYPE_ENV = f"{_ENV_PREFIX}COMPUTE_TYPE"
_MODEL_CACHE_ENV = f"{_ENV_PREFIX}MODEL_CACHE"
_LOCAL_FILES_ONLY_ENV = f"{_ENV_PREFIX}LOCAL_FILES_ONLY"

_ALLOWED_DEVICES = frozenset({"cpu", "cuda"})
_ALLOWED_COMPUTE_TYPES = frozenset(
    {
        "auto",
        "bfloat16",
        "default",
        "float16",
        "float32",
        "int8",
        "int8_bfloat16",
        "int8_float16",
        "int8_float32",
        "int16",
    }
)
_PINNED_SNAPSHOT_FILES = (
    "config.json",
    "model.bin",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
)

ModelFactory = Callable[..., Any]
Clock = Callable[[], float]
ConfiguredDependencyProbe = Callable[["WhisperTurboConfig"], tuple[bool, str | None]]
VersionLookup = Callable[[str], str]
RuntimeLoader = Callable[[], Any]

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
    if value < 0 or str(value) != raw.strip():
        raise ValueError(f"{name} must be a canonical non-negative integer")
    return value


def _fixed_identity(environ: Mapping[str, str], name: str, expected: str) -> None:
    """Reject an override that would make descriptor provenance untruthful."""

    configured = environ.get(name)
    if configured is not None and configured != expected:
        raise ValueError(f"{name} is fixed at {expected!r} for this worker image")


@dataclass(frozen=True, slots=True)
class WhisperTurboConfig:
    """Runtime placement for the fixed hosted model."""

    device: str = "cuda"
    device_index: int = 0
    compute_type: str = "float16"
    model_cache: Path = Path("/models/hf")
    local_files_only: bool = True

    def __post_init__(self) -> None:
        if self.device not in _ALLOWED_DEVICES:
            choices = ", ".join(sorted(_ALLOWED_DEVICES))
            raise ValueError(f"device must be one of: {choices}")
        if self.device_index < 0:
            raise ValueError("device_index must be non-negative")
        if self.compute_type not in _ALLOWED_COMPUTE_TYPES:
            choices = ", ".join(sorted(_ALLOWED_COMPUTE_TYPES))
            raise ValueError(f"compute_type must be one of: {choices}")
        if not self.model_cache.is_absolute():
            raise ValueError("model_cache must be an absolute path")

    @property
    def device_label(self) -> str:
        return f"cuda:{self.device_index}" if self.device == "cuda" else self.device

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str] | None = None
    ) -> WhisperTurboConfig:
        source = os.environ if environ is None else environ
        _fixed_identity(source, _FIXED_MODEL_ID_ENV, MODEL_ID)
        _fixed_identity(source, _FIXED_MODEL_REVISION_ENV, MODEL_REVISION)

        model_cache_raw = source.get(_MODEL_CACHE_ENV, "/models/hf").strip()
        if not model_cache_raw:
            raise ValueError(f"{_MODEL_CACHE_ENV} must not be empty")
        device = source.get(_DEVICE_ENV, "cuda").strip().lower()
        default_compute_type = "float32" if device == "cpu" else "float16"
        return cls(
            device=device,
            device_index=_nonnegative_int(
                _DEVICE_INDEX_ENV, source.get(_DEVICE_INDEX_ENV, "0")
            ),
            compute_type=source.get(_COMPUTE_TYPE_ENV, default_compute_type)
            .strip()
            .lower(),
            model_cache=Path(model_cache_raw),
            local_files_only=_strict_bool(
                _LOCAL_FILES_ONLY_ENV,
                source.get(_LOCAL_FILES_ONLY_ENV, "true"),
            ),
        )


def _pinned_snapshot_path(config: WhisperTurboConfig) -> Path:
    repository = f"models--{MODEL_ID.replace('/', '--')}"
    return config.model_cache / repository / "snapshots" / MODEL_REVISION


def _configured_runtime_probe(
    config: WhisperTurboConfig,
    *,
    version_lookup: VersionLookup = importlib.metadata.version,
    runtime_loader: RuntimeLoader = lambda: importlib.import_module("ctranslate2"),
) -> tuple[bool, str | None]:
    """Validate the fixed package, cache policy, device, and compute support."""

    try:
        installed_version = version_lookup("faster-whisper")
    except importlib.metadata.PackageNotFoundError:
        return False, f"faster-whisper=={FASTER_WHISPER_VERSION} is not installed"
    except Exception:
        return False, "faster-whisper package metadata could not be read"
    if installed_version != FASTER_WHISPER_VERSION:
        return False, "faster-whisper package version does not match the worker pin"

    if config.local_files_only:
        snapshot = _pinned_snapshot_path(config)
        if not snapshot.is_dir() or not all(
            (snapshot / name).is_file() for name in _PINNED_SNAPSHOT_FILES
        ):
            return False, "pinned model snapshot is absent from the local cache"

    try:
        runtime = runtime_loader()
        if config.device == "cuda":
            device_count = int(runtime.get_cuda_device_count())
            if config.device_index >= device_count:
                return False, "configured CUDA device is unavailable"
        supported = set(
            runtime.get_supported_compute_types(
                config.device,
                device_index=config.device_index,
            )
        )
    except Exception:
        return False, "CTranslate2 runtime capability probe failed"
    if (
        config.compute_type not in {"auto", "default"}
        and config.compute_type not in supported
    ):
        return False, "configured compute type is unsupported on the selected device"
    return True, None


def _default_model_factory(**kwargs: Any) -> Any:
    from faster_whisper import WhisperModel  # lazy: never on capabilities path

    return WhisperModel(**kwargs)


def _finite_nonnegative(value: Any, *, field: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"faster-whisper returned a non-numeric {field}") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"faster-whisper returned an invalid {field}")
    return normalized


def _word_from_native(word: Any) -> TranscribeWord:
    token = str(getattr(word, "word", ""))
    if not token:
        raise ValueError("faster-whisper returned an empty word token")
    return TranscribeWord(
        word=token,
        start=_finite_nonnegative(getattr(word, "start", None), field="word start"),
        end=_finite_nonnegative(getattr(word, "end", None), field="word end"),
    )


def _segment_from_native(segment: Any) -> TranscribeSegment:
    native_words = getattr(segment, "words", None)
    words = (
        [_word_from_native(word) for word in native_words]
        if native_words is not None
        else None
    )
    return TranscribeSegment(
        start=_finite_nonnegative(
            getattr(segment, "start", None), field="segment start"
        ),
        end=_finite_nonnegative(getattr(segment, "end", None), field="segment end"),
        text=str(getattr(segment, "text", "")),
        words=words,
    )


class WhisperTurboAdapter:
    """Boundary-C adapter around one already-constructed WhisperModel."""

    def __init__(
        self,
        config: WhisperTurboConfig,
        *,
        model_factory: ModelFactory = _default_model_factory,
        clock: Clock = time.perf_counter,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        kwargs: dict[str, Any] = {
            "model_size_or_path": MODEL_ID,
            "device": config.device,
            "device_index": config.device_index,
            "compute_type": config.compute_type,
            "download_root": str(config.model_cache),
            "local_files_only": config.local_files_only,
            "revision": MODEL_REVISION,
            # Boundary B admits one request. Do not create hidden model workers.
            "num_workers": 1,
        }
        source = os.environ if environ is None else environ
        token = source.get("HF_TOKEN")
        if token:
            kwargs["use_auth_token"] = token
        self._model = model_factory(**kwargs)

    def transcribe(
        self, audio_path: Path, options: TranscribeOptions
    ) -> TranscribeResult:
        DESCRIPTOR.validate_options(options)
        started = self._clock()
        native_segments, info = self._model.transcribe(
            str(audio_path),
            language=options.language,
            vad_filter=(
                WHISPER_TURBO_VAD_DEFAULT if options.vad is None else options.vad
            ),
            initial_prompt=options.context,
            word_timestamps=True,
        )
        # Inference is generator-backed, so iteration belongs in this timing.
        segments = [_segment_from_native(segment) for segment in native_segments]
        elapsed = _finite_nonnegative(
            self._clock() - started, field="adapter inference timing"
        )

        text = "".join(segment.text for segment in segments).strip()
        detected_language = str(getattr(info, "language", "") or "").strip()
        language = detected_language or options.language
        native_duration = getattr(info, "duration", None)
        if native_duration is None:
            duration = max((segment.end for segment in segments), default=0.0)
        else:
            duration = _finite_nonnegative(native_duration, field="duration")

        return TranscribeResult(
            engine=ENGINE,
            text=text,
            segments=segments,
            language=language,
            duration=duration,
            model_ids=[MODEL_ID],
            revision=MODEL_REVISION,
            device=self._config.device_label,
            dtype=self._config.compute_type,
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
            self._load_error = "Whisper Turbo model failed to load"

    def snapshot(self) -> tuple[bool, str | None]:
        with self._lock:
            return self._loaded, self._load_error


def build_registration(
    config: WhisperTurboConfig | None = None,
    *,
    model_factory: ModelFactory = _default_model_factory,
    dependency_probe: ConfiguredDependencyProbe = _configured_runtime_probe,
    clock: Clock = time.perf_counter,
    environ: Mapping[str, str] | None = None,
) -> AdapterRegistration:
    """Build a lazy registration; injectable for isolated CPU-mock tests."""

    runtime_config = config or WhisperTurboConfig.from_environ(environ)
    state = _RegistrationState()

    def factory() -> WhisperTurboAdapter:
        try:
            adapter = WhisperTurboAdapter(
                runtime_config,
                model_factory=model_factory,
                clock=clock,
                environ=environ,
            )
        except Exception:
            _LOG.exception("Whisper Turbo model factory failed")
            state.failed()
            raise RuntimeError("Whisper Turbo model failed to load") from None
        state.loaded()
        return adapter

    def probe() -> EngineProbe:
        loaded, load_error = state.snapshot()
        if load_error is not None:
            return EngineProbe(available=False, loaded=False, error=load_error)
        available, dependency_error = dependency_probe(runtime_config)
        return EngineProbe(
            available=available,
            loaded=loaded,
            error=dependency_error,
        )

    return AdapterRegistration(
        descriptor=DESCRIPTOR,
        factory=factory,
        probe=probe,
    )


REGISTRATION = build_registration()


__all__ = [
    "DESCRIPTOR",
    "ENGINE",
    "FASTER_WHISPER_VERSION",
    "MODEL_ID",
    "MODEL_REVISION",
    "REGISTRATION",
    "WhisperTurboAdapter",
    "WhisperTurboConfig",
    "build_registration",
]
