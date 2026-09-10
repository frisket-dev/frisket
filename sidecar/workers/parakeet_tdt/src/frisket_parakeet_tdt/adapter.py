"""Fixed Parakeet TDT adapter for transcription contract v1.

The image owns one immutable ASR/VAD/diarizer assembly. Heavy runtimes and
weights stay lazy so capability probes never start inference or touch CUDA.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket_models.transcription import (
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
)
from frisket_models.transcription.parakeet_tdt import (
    DIARIZER_FILENAME,
)
from frisket_models.transcription.parakeet_tdt import (
    PARAKEET_DESCRIPTOR as DESCRIPTOR,
)
from frisket_models.transcription.parakeet_tdt import (
    PARAKEET_ENGINE as ENGINE,
)
from frisket_models.transcription.parakeet_tdt import (
    PARAKEET_REVISION as REVISION,
)

ASR_FILES = (
    "config.json",
    "vocab.txt",
    "encoder-model.int8.onnx",
    "decoder_joint-model.int8.onnx",
)
VAD_FILES = ("config.json", "silero_vad.onnx")

_MODEL_ROOT_ENV = "FRISKET_PARAKEET_TDT_MODEL_ROOT"
_ONNX_MODEL_NAME = "nemo-parakeet-tdt-0.6b-v2"
_VAD_MODEL_NAME = "silero"
_SAMPLE_RATE = 16_000
_MAX_SPEAKERS = 4
_UNKNOWN_SPEAKER = "UNKNOWN"
_UNKNOWN_SPEAKER_WARNING = (
    "Diarization found no matching speaker turn for one or more transcript "
    "segments; those segments are labelled UNKNOWN."
)
_APPROXIMATE_WARNING = (
    "Speaker labels are approximate because VAD transcription has no word "
    "timestamps; each utterance uses its majority-overlap speaker."
)
_MAX_SPEAKERS_WARNING = (
    "The diarizer reached its four-speaker limit; additional voices may be "
    "folded into those labels."
)

Clock = Callable[[], float]
RuntimeFactory = Callable[["ParakeetConfig"], "ParakeetRuntime"]
DiarizerFactory = Callable[["ParakeetConfig"], Any]
ConfiguredDependencyProbe = Callable[["ParakeetConfig"], tuple[bool, str | None]]

_LOG = logging.getLogger(__name__)


def _finite_nonnegative(value: Any, *, field: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parakeet returned a non-numeric {field}") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"Parakeet returned an invalid {field}")
    return normalized


@dataclass(frozen=True, slots=True)
class ParakeetConfig:
    model_root: Path = Path("/models/parakeet-tdt")

    def __post_init__(self) -> None:
        if not self.model_root.is_absolute():
            raise ValueError("model_root must be an absolute path")

    @property
    def asr_path(self) -> Path:
        return self.model_root / "asr"

    @property
    def vad_path(self) -> Path:
        return self.model_root / "vad"

    @property
    def diarizer_path(self) -> Path:
        return self.model_root / "diarizer" / DIARIZER_FILENAME

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> ParakeetConfig:
        source = os.environ if environ is None else environ
        raw = source.get(_MODEL_ROOT_ENV, "/models/parakeet-tdt").strip()
        if not raw:
            raise ValueError(f"{_MODEL_ROOT_ENV} must not be empty")
        return cls(model_root=Path(raw))


@dataclass(frozen=True, slots=True)
class ParakeetRuntime:
    model: Any
    vad: Any


def _files_present(directory: Path, filenames: Sequence[str]) -> bool:
    return directory.is_dir() and all(
        (directory / name).is_file() for name in filenames
    )


def _configured_runtime_probe(config: ParakeetConfig) -> tuple[bool, str | None]:
    packages = ("av", "nemo", "numpy", "onnx_asr", "onnxruntime")
    if any(importlib.util.find_spec(package) is None for package in packages):
        return False, "the fixed Parakeet worker dependencies are not installed"
    if not _files_present(config.asr_path, ASR_FILES):
        return False, "the pinned Parakeet ASR snapshot is absent"
    if not _files_present(config.vad_path, VAD_FILES):
        return False, "the pinned VAD snapshot is absent"
    if not config.diarizer_path.is_file():
        return False, "the pinned Sortformer checkpoint is absent"
    return True, None


def _default_runtime_factory(config: ParakeetConfig) -> ParakeetRuntime:
    import onnx_asr

    providers = ["CPUExecutionProvider"]
    model = onnx_asr.load_model(
        _ONNX_MODEL_NAME,
        path=config.asr_path,
        quantization="int8",
        providers=providers,
    )
    vad = onnx_asr.load_vad(
        _VAD_MODEL_NAME,
        path=config.vad_path,
        providers=providers,
    )
    return ParakeetRuntime(model=model, vad=vad)


def _default_diarizer_factory(config: ParakeetConfig) -> Any:
    from nemo.collections.asr.models import SortformerEncLabelModel

    model = SortformerEncLabelModel.restore_from(str(config.diarizer_path))
    model = model.to("cuda")
    model.eval()
    model.sortformer_modules.chunk_len = 340
    model.sortformer_modules.chunk_right_context = 40
    model.sortformer_modules.fifo_len = 40
    model.sortformer_modules.spkcache_update_period = 300
    return model


def _load_audio(path: Path) -> Any:
    with path.open("rb") as stream:
        if stream.read(4) == b"RIFF":
            return str(path)

    import av
    import numpy as np

    chunks = []
    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=_SAMPLE_RATE
    )
    with av.open(path) as container:
        stream = next(item for item in container.streams if item.type == "audio")
        for frame in container.decode(stream):
            chunks.extend(item.to_ndarray() for item in resampler.resample(frame))
        chunks.extend(item.to_ndarray() for item in resampler.resample(None))
    if not chunks:
        raise ValueError("audio contains no decodable stream")
    pcm = np.concatenate([chunk.reshape(-1) for chunk in chunks])
    return pcm.astype(np.float32) / 32768.0


def _word_segments(tokens: Any, timestamps: Any) -> list[dict[str, Any]]:
    words: list[str] = []
    starts: list[float] = []
    for token, timestamp in zip(tokens, timestamps, strict=False):
        token = str(token)
        if token[:1] in ("▁", " ") or not words:
            words.append(token.lstrip("▁ "))
            starts.append(_finite_nonnegative(timestamp, field="word timestamp"))
        else:
            words[-1] += token

    last = (
        _finite_nonnegative(timestamps[-1], field="word timestamp")
        if timestamps
        else 0.0
    )
    gaps = [starts[index + 1] - starts[index] for index in range(len(starts) - 1)]
    positive_gaps = [gap for gap in gaps if gap > 0]
    tail = sum(positive_gaps) / len(positive_gaps) if positive_gaps else 0.5
    segments: list[dict[str, Any]] = []
    for index, (word, start) in enumerate(zip(words, starts, strict=False)):
        if not word:
            continue
        end = starts[index + 1] if index + 1 < len(starts) else max(last, start + tail)
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": word})
    return segments


def _sortformer_turns(predictions: Any) -> list[dict[str, Any]]:
    if not predictions:
        return []
    per_audio = predictions[0]
    labels: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    for item in per_audio or []:
        if isinstance(item, str):
            parts = item.split()
            if len(parts) < 3:
                continue
            start, end, raw = parts[0], parts[1], parts[2]
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            start, end, raw = item[0], item[1], str(item[2])
        else:
            continue
        try:
            normalized_start = _finite_nonnegative(start, field="speaker turn start")
            normalized_end = _finite_nonnegative(end, field="speaker turn end")
        except ValueError:
            continue
        if normalized_end <= normalized_start:
            continue
        if raw not in labels:
            labels[raw] = f"S{len(labels) + 1}"
        turns.append(
            {
                "speaker": labels[raw],
                "start": normalized_start,
                "end": normalized_end,
            }
        )
    return turns


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _best_turn_index(
    start: float, end: float, turns: list[dict[str, Any]]
) -> int | None:
    best_index: int | None = None
    best_overlap = 0.0
    for index, turn in enumerate(turns):
        overlap = _overlap(start, end, turn["start"], turn["end"])
        if overlap > best_overlap:
            best_index = index
            best_overlap = overlap
    return best_index


def _speaker_for_span(
    start: float, end: float, turns: list[dict[str, Any]]
) -> str | None:
    intervals: dict[str, list[tuple[float, float]]] = {}
    order: list[str] = []
    for turn in turns:
        overlap_start = max(start, turn["start"])
        overlap_end = min(end, turn["end"])
        if overlap_end <= overlap_start:
            continue
        speaker = turn["speaker"]
        if speaker not in intervals:
            intervals[speaker] = []
            order.append(speaker)
        intervals[speaker].append((overlap_start, overlap_end))

    best: str | None = None
    best_coverage = 0.0
    for speaker in order:
        coverage = 0.0
        current_start: float | None = None
        current_end: float | None = None
        for interval_start, interval_end in sorted(intervals[speaker]):
            if current_end is None or interval_start > current_end:
                if current_end is not None and current_start is not None:
                    coverage += current_end - current_start
                current_start, current_end = interval_start, interval_end
            elif interval_end > current_end:
                current_end = interval_end
        if current_end is not None and current_start is not None:
            coverage += current_end - current_start
        if coverage > best_coverage:
            best, best_coverage = speaker, coverage
    return best


def _merge_speaker_turns(
    segments: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    *,
    has_word_timestamps: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    if has_word_timestamps:
        grouped: list[dict[str, Any]] = []
        for segment in segments:
            turn_index = _best_turn_index(segment["start"], segment["end"], turns)
            speaker = turns[turn_index]["speaker"] if turn_index is not None else None
            if grouped and grouped[-1]["_turn"] == turn_index:
                grouped[-1]["end"] = segment["end"]
                grouped[-1]["text"] = (
                    grouped[-1]["text"] + " " + segment["text"]
                ).strip()
            else:
                grouped.append({**segment, "_turn": turn_index, "speaker": speaker})
        merged = []
        for segment in grouped:
            normalized = {
                key: value for key, value in segment.items() if key != "_turn"
            }
            merged.append(normalized)
        return merged, []

    approximate = False
    merged = []
    for segment in segments:
        speaker = _speaker_for_span(segment["start"], segment["end"], turns)
        normalized = dict(segment)
        if speaker is not None:
            normalized.update(speaker=speaker, speaker_confidence="approximate")
            approximate = True
        merged.append(normalized)
    return merged, [_APPROXIMATE_WARNING] if approximate else []


def _ensure_speakers(
    segments: list[dict[str, Any]], warnings: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    missing = False
    normalized = []
    for segment in segments:
        item = dict(segment)
        if not item.get("speaker"):
            item["speaker"] = _UNKNOWN_SPEAKER
            item["speaker_confidence"] = "unknown"
            missing = True
        normalized.append(item)
    if missing and _UNKNOWN_SPEAKER_WARNING not in warnings:
        warnings.append(_UNKNOWN_SPEAKER_WARNING)
    return normalized, warnings


class ParakeetTdtAdapter:
    def __init__(
        self,
        config: ParakeetConfig,
        *,
        runtime_factory: RuntimeFactory = _default_runtime_factory,
        diarizer_factory: DiarizerFactory = _default_diarizer_factory,
        clock: Clock = time.perf_counter,
    ) -> None:
        self._config = config
        self._runtime = runtime_factory(config)
        self._diarizer_factory = diarizer_factory
        self._clock = clock
        self._diarizer: Any | None = None
        self._diarizer_lock = threading.Lock()

    def _load_diarizer(self) -> Any:
        if self._diarizer is None:
            with self._diarizer_lock:
                if self._diarizer is None:
                    self._diarizer = self._diarizer_factory(self._config)
        return self._diarizer

    def transcribe(
        self, audio_path: Path, options: TranscribeOptions
    ) -> TranscribeResult:
        DESCRIPTOR.validate_options(options)
        waveform = _load_audio(audio_path)
        vad = True if options.vad is None else options.vad

        asr_started = self._clock()
        if vad:
            native = self._runtime.model.with_vad(
                self._runtime.vad, batch_size=1
            ).recognize(waveform, sample_rate=_SAMPLE_RATE)
            segments = [
                {
                    "start": _finite_nonnegative(item.start, field="segment start"),
                    "end": _finite_nonnegative(item.end, field="segment end"),
                    "text": str(item.text).strip(),
                }
                for item in native
                if str(item.text).strip()
            ]
            has_word_timestamps = False
        else:
            native = self._runtime.model.with_timestamps().recognize(
                waveform, sample_rate=_SAMPLE_RATE
            )
            segments = _word_segments(native.tokens, native.timestamps)
            has_word_timestamps = True
        asr_seconds = _finite_nonnegative(
            self._clock() - asr_started, field="ASR timing"
        )

        warnings: list[str] = []
        diarization_seconds = 0.0
        if options.diarize is True:
            diarization_started = self._clock()
            predictions = self._load_diarizer().diarize(
                audio=[str(audio_path)], batch_size=1
            )
            turns = _sortformer_turns(predictions)
            if len({turn["speaker"] for turn in turns}) >= _MAX_SPEAKERS:
                warnings.append(_MAX_SPEAKERS_WARNING)
            segments, merge_warnings = _merge_speaker_turns(
                segments,
                turns,
                has_word_timestamps=has_word_timestamps,
            )
            warnings.extend(merge_warnings)
            segments, warnings = _ensure_speakers(segments, warnings)
            diarization_seconds = _finite_nonnegative(
                self._clock() - diarization_started,
                field="diarization timing",
            )

        result_segments = [TranscribeSegment.model_validate(item) for item in segments]
        text = " ".join(segment.text.strip() for segment in result_segments).strip()
        duration = max((segment.end for segment in result_segments), default=0.0)
        timings = {"adapter.asr_seconds": asr_seconds}
        if options.diarize is True:
            timings["adapter.diarization_seconds"] = diarization_seconds
        result = TranscribeResult(
            engine=ENGINE,
            text=text,
            segments=result_segments,
            language="en" if text else None,
            duration=duration,
            model_ids=list(DESCRIPTOR.model_ids),
            revision=REVISION,
            device="cpu+cuda:0" if options.diarize is True else "cpu",
            dtype="int8+float32" if options.diarize is True else "int8",
            timings=timings,
            warnings=warnings,
            accepted_options=options.supplied_options(),
        )
        return DESCRIPTOR.validate_result(result, options)


class _RegistrationState:
    def __init__(self) -> None:
        self.loaded = False
        self.load_error: str | None = None
        self.lock = threading.Lock()


def build_registration(
    config: ParakeetConfig | None = None,
    *,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    diarizer_factory: DiarizerFactory = _default_diarizer_factory,
    dependency_probe: ConfiguredDependencyProbe = _configured_runtime_probe,
    clock: Clock = time.perf_counter,
    environ: Mapping[str, str] | None = None,
) -> AdapterRegistration:
    runtime_config = config or ParakeetConfig.from_environ(environ)
    state = _RegistrationState()

    def factory() -> ParakeetTdtAdapter:
        try:
            adapter = ParakeetTdtAdapter(
                runtime_config,
                runtime_factory=runtime_factory,
                diarizer_factory=diarizer_factory,
                clock=clock,
            )
        except Exception:
            _LOG.error("Parakeet model factory failed")
            with state.lock:
                state.load_error = "Parakeet model failed to load"
            raise RuntimeError("Parakeet model failed to load") from None
        with state.lock:
            state.loaded = True
            state.load_error = None
        return adapter

    def probe() -> EngineProbe:
        with state.lock:
            loaded, load_error = state.loaded, state.load_error
        if load_error is not None:
            return EngineProbe(available=False, loaded=False, error=load_error)
        available, error = dependency_probe(runtime_config)
        return EngineProbe(available=available, loaded=loaded, error=error)

    return AdapterRegistration(descriptor=DESCRIPTOR, factory=factory, probe=probe)


REGISTRATION = build_registration()

__all__ = [
    "ASR_FILES",
    "DESCRIPTOR",
    "ENGINE",
    "REGISTRATION",
    "REVISION",
    "VAD_FILES",
    "ParakeetConfig",
    "ParakeetRuntime",
    "ParakeetTdtAdapter",
    "_merge_speaker_turns",
    "_sortformer_turns",
    "_word_segments",
    "build_registration",
]
