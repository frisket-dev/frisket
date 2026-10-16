"""Bounded media part producers with source-timeline locations.

The caller supplies an already-admitted :class:`BoundLocalFile` or a private
materialization lease owned by this module. Borrowed streams are copied and
verified into scratch; verified blob materializations are decoded only while
their owning context is active. Encoder payloads contain only representation
bytes. Exact presentation timestamps and intervals stay on located wrappers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, TypeAlias

from PIL import Image

from frisket.engine.executor.action_inventory import BoundLocalFile
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.retrieval.encoding import (
    EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE,
    EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
    AudioEncodingInput,
    ImageEncodingInput,
    VideoClipEncodingInput,
)


CancelCheck: TypeAlias = Callable[[], bool]
_PCM_BYTES_PER_SAMPLE = 2
_MAX_AUDIO_SECONDS = 30.0
_COPY_CHUNK_BYTES = 1024 * 1024
_SHOWINFO_FRAME = re.compile(r"\bn:\s*\d+\s+pts:\s*(\S+)\s+pts_time:\s*(\S+)")
_SHOWINFO_TIME_BASE = re.compile(r"config in time_base:\s*(\d+)\s*/\s*(\d+)")
_FORMAT_DURATION_IS_END = frozenset({"matroska", "webm", "nut"})


class MediaProductionError(RuntimeError):
    """Actionable refusal or decoder failure at the media producer boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _MaterializedMedia:
    _path: Path
    sha256: str
    _active: bool = True

    def borrow_path(self) -> Path:
        if not self._active:
            raise MediaProductionError(
                "invalid_media_source", "media materialization lease has ended"
            )
        if not self._path.is_file():
            raise MediaProductionError(
                "invalid_media_source", "media materialization is unavailable"
            )
        return self._path


@contextlib.contextmanager
def _borrow_materialized_media(project, digest: str):
    """Own one verified blob materialization for the yielded media borrower."""

    with project.materialize_blob(digest) as path:
        borrowed = _MaterializedMedia(Path(path), f"sha256:{digest}")
        try:
            yield borrowed
        finally:
            borrowed._active = False


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    checked = float(value)
    if not math.isfinite(checked) or checked <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return checked


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    checked = float(value)
    if not math.isfinite(checked) or checked < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return checked


def _positive_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _dimension(value: object) -> int:
    checked = _positive_count(value, "max_dimension")
    if not 16 <= checked <= 4096:
        raise ValueError("max_dimension must be between 16 and 4096")
    return checked


@dataclass(frozen=True, slots=True)
class PeriodicFrameRecipe:
    interval_seconds: float = 1.0
    adjacent_dedup_threshold: float | None = 0.01
    max_frames: int = 600
    max_dimension: int = 448
    strategy: Literal["periodic_frames"] = "periodic_frames"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "interval_seconds",
            _positive_number(self.interval_seconds, "interval_seconds"),
        )
        object.__setattr__(
            self, "max_frames", _positive_count(self.max_frames, "max_frames")
        )
        object.__setattr__(self, "max_dimension", _dimension(self.max_dimension))
        threshold = self.adjacent_dedup_threshold
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                raise TypeError("adjacent_dedup_threshold must be a number or None")
            threshold = float(threshold)
            if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise ValueError("adjacent_dedup_threshold must be between 0 and 1")
            object.__setattr__(self, "adjacent_dedup_threshold", threshold)

    @property
    def recipe_id(self) -> str:
        return (
            f"{self.strategy}:interval={self.interval_seconds}:"
            f"dedup={self.adjacent_dedup_threshold}:max-dimension={self.max_dimension}"
        )


@dataclass(frozen=True, slots=True)
class FixedClipRecipe:
    duration_seconds: float = 8.0
    stride_seconds: float = 4.0
    sample_fps: float = 1.0
    max_clips: int = 450
    max_dimension: int = 448
    strategy: Literal["fixed_clips"] = "fixed_clips"

    def __post_init__(self) -> None:
        duration = _positive_number(self.duration_seconds, "duration_seconds")
        stride = _positive_number(self.stride_seconds, "stride_seconds")
        fps = _positive_number(self.sample_fps, "sample_fps")
        if stride > duration:
            raise ValueError("stride_seconds must not exceed duration_seconds")
        if math.ceil(duration * fps) > EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES:
            raise ValueError("a clip recipe must produce at most 32 sampled frames")
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "stride_seconds", stride)
        object.__setattr__(self, "sample_fps", fps)
        object.__setattr__(
            self, "max_clips", _positive_count(self.max_clips, "max_clips")
        )
        object.__setattr__(self, "max_dimension", _dimension(self.max_dimension))

    @property
    def recipe_id(self) -> str:
        return (
            f"{self.strategy}:duration={self.duration_seconds}:"
            f"stride={self.stride_seconds}:fps={self.sample_fps}:"
            f"max-dimension={self.max_dimension}"
        )


@dataclass(frozen=True, slots=True)
class ShotClipRecipe:
    """Clip on ffmpeg's versioned 0..1 scene score, then split long shots."""

    max_duration_seconds: float = 10.0
    quantum_seconds: float = 120.0
    sample_fps: float = 1.0
    max_clips: int = 450
    scene_score_threshold: float = 0.1
    min_scene_len_frames: int = 15
    max_dimension: int = 448
    strategy: Literal["shot_clips_ffmpeg_scene_v1"] = "shot_clips_ffmpeg_scene_v1"

    def __post_init__(self) -> None:
        duration = _positive_number(self.max_duration_seconds, "max_duration_seconds")
        quantum = _positive_number(self.quantum_seconds, "quantum_seconds")
        fps = _positive_number(self.sample_fps, "sample_fps")
        threshold = _positive_number(
            self.scene_score_threshold, "scene_score_threshold"
        )
        if threshold > 1:
            raise ValueError("scene_score_threshold must not exceed one")
        if math.ceil(duration * fps) > EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES:
            raise ValueError("a shot recipe must produce at most 32 sampled frames")
        object.__setattr__(self, "max_duration_seconds", duration)
        object.__setattr__(self, "quantum_seconds", quantum)
        object.__setattr__(self, "sample_fps", fps)
        object.__setattr__(
            self, "max_clips", _positive_count(self.max_clips, "max_clips")
        )
        object.__setattr__(self, "scene_score_threshold", threshold)
        object.__setattr__(
            self,
            "min_scene_len_frames",
            _positive_count(self.min_scene_len_frames, "min_scene_len_frames"),
        )
        object.__setattr__(self, "max_dimension", _dimension(self.max_dimension))

    @property
    def recipe_id(self) -> str:
        return (
            f"{self.strategy}:max-duration={self.max_duration_seconds}:"
            f"quantum={self.quantum_seconds}:"
            f"fps={self.sample_fps}:scene-score={self.scene_score_threshold}:"
            f"min-scene-frames={self.min_scene_len_frames}:"
            f"max-dimension={self.max_dimension}"
        )


@dataclass(frozen=True, slots=True)
class AudioWindowRecipe:
    duration_seconds: float = 8.0
    stride_seconds: float = 4.0
    max_windows: int = 450
    strategy: Literal["audio_windows"] = "audio_windows"

    def __post_init__(self) -> None:
        duration = _positive_number(self.duration_seconds, "duration_seconds")
        stride = _positive_number(self.stride_seconds, "stride_seconds")
        if stride > duration:
            raise ValueError("stride_seconds must not exceed duration_seconds")
        if duration > _MAX_AUDIO_SECONDS:
            raise ValueError("duration_seconds must not exceed 30 seconds")
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "stride_seconds", stride)
        object.__setattr__(
            self, "max_windows", _positive_count(self.max_windows, "max_windows")
        )

    @property
    def recipe_id(self) -> str:
        return (
            f"{self.strategy}:duration={self.duration_seconds}:"
            f"stride={self.stride_seconds}"
        )


MediaRecipe: TypeAlias = (
    PeriodicFrameRecipe | FixedClipRecipe | ShotClipRecipe | AudioWindowRecipe
)


@dataclass(frozen=True, slots=True)
class PresentationPoint:
    """One decoder-observed presentation timestamp and its stream time base."""

    pts: int
    time_base_num: int
    time_base_den: int

    @property
    def seconds(self) -> float:
        return self.pts * self.time_base_num / self.time_base_den


@dataclass(frozen=True, slots=True)
class LocatedFrame:
    point: PresentationPoint
    input: ImageEncodingInput


@dataclass(frozen=True, slots=True)
class LocatedVideoClip:
    start_seconds: float
    end_seconds: float
    frames: tuple[LocatedFrame, ...]
    input: VideoClipEncodingInput


@dataclass(frozen=True, slots=True)
class AudioWindow:
    start_seconds: float
    end_seconds: float
    input: AudioEncodingInput


LocatedMediaInput: TypeAlias = LocatedFrame | LocatedVideoClip | AudioWindow


@dataclass(frozen=True, slots=True)
class MediaCoverage:
    source_start_seconds: float
    source_end_seconds: float
    indexed_spans: tuple[tuple[float, float], ...]
    complete: bool
    truncation_reason: (
        Literal[
            "max_frames",
            "max_clips",
            "max_windows",
            "missing_samples",
            "timeline_quantum",
        ]
        | None
    ) = None
    timeline_estimated: bool = False


@dataclass(frozen=True, slots=True)
class ProducedMedia:
    items: tuple[LocatedMediaInput, ...]
    coverage: MediaCoverage


@dataclass(frozen=True, slots=True)
class _StreamProbe:
    start_seconds: float
    end_seconds: float
    time_base_num: int
    time_base_den: int
    estimated: bool


def _check_cancelled(cancelled: CancelCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise asyncio.CancelledError


def _binary(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise MediaProductionError(
            "missing_media_tool", f"media retrieval needs {name} on PATH"
        )
    return binary


def _expected_digest(source: BoundLocalFile | _MaterializedMedia) -> str:
    prefix, separator, digest = source.sha256.partition(":")
    if (
        prefix != "sha256"
        or not separator
        or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    ):
        raise MediaProductionError(
            "invalid_media_source", "admitted media has an invalid SHA-256 identity"
        )
    return digest.lower()


@contextlib.contextmanager
def _borrow_stream(source: BoundLocalFile):
    if source.open_source is not None:
        with source.open_source() as stream:
            yield stream
        return
    if source.stream is None:
        raise MediaProductionError(
            "invalid_media_source", "admitted media is unavailable"
        )
    yield source.stream


async def _stage_source(
    source: BoundLocalFile, path: Path, cancelled: CancelCheck | None
) -> None:
    if not isinstance(source, BoundLocalFile):
        raise TypeError("source must be an admitted BoundLocalFile")
    expected = _expected_digest(source)
    digest = hashlib.sha256()
    with _borrow_stream(source) as stream:
        if not hasattr(stream, "seek") or not hasattr(stream, "read"):
            raise MediaProductionError(
                "invalid_media_source",
                "admitted media must be a seekable binary stream",
            )
        original_position: int | None = None
        try:
            original_position = stream.tell()
            stream.seek(0)
            with path.open("xb") as output:
                while True:
                    _check_cancelled(cancelled)
                    chunk = stream.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("admitted media stream must return bytes")
                    digest.update(chunk)
                    output.write(chunk)
                    await asyncio.sleep(0)
        except (OSError, ValueError) as exc:
            raise MediaProductionError(
                "invalid_media_source", "admitted media could not be staged"
            ) from exc
        finally:
            if original_position is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.seek(original_position)
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise MediaProductionError(
            "stale_media_source", "admitted media bytes no longer match their SHA-256"
        )


@contextlib.asynccontextmanager
async def _readable_media_path(
    source: BoundLocalFile | _MaterializedMedia,
    scratch: Path,
    cancelled: CancelCheck | None,
):
    """Yield one confined decoder path from the admitted source variant."""

    _check_cancelled(cancelled)
    if isinstance(source, _MaterializedMedia):
        _expected_digest(source)
        yield source.borrow_path()
        return
    if not isinstance(source, BoundLocalFile):
        raise TypeError("source must be admitted media")
    staged = scratch / "source.media"
    await _stage_source(source, staged, cancelled)
    yield staged


async def _run_tool(
    argv: list[str],
    *,
    op: str,
    read: Path,
    write: Path | None,
    cancelled: CancelCheck | None,
    wall_seconds: int = 300,
):
    _check_cancelled(cancelled)
    binary = argv[0]
    result = await run_sandboxed(
        argv,
        policy=SandboxPolicy(
            wall_seconds=wall_seconds,
            memory_mb=2048,
            allow_network=False,
            env_passthrough=["PATH"],
            confine=fence.Confinement(
                op=op,
                read=(str(read),),
                write=(str(write),) if write is not None else (),
                exec_binary=binary,
            ),
        ),
        should_cancel=cancelled,
    )
    if result.cancelled:
        raise asyncio.CancelledError
    if not result.ok:
        detail = result.stderr[-400:].strip()
        raise MediaProductionError(
            "media_decode_failed", f"{Path(binary).name} failed: {detail}"
        )
    _check_cancelled(cancelled)
    return result


def _decimal(value: object) -> Decimal | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


async def _probe(
    path: Path, kind: Literal["video", "audio"], cancelled: CancelCheck | None
) -> _StreamProbe:
    ffprobe = _binary("ffprobe")
    result = await _run_tool(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,start_time,duration:stream=index,codec_type,start_time,duration,time_base",
            "-of",
            "json",
            str(path),
        ],
        op=f"retrieval.media.{kind}_probe",
        read=path,
        write=None,
        cancelled=cancelled,
        wall_seconds=60,
    )
    try:
        payload = json.loads(result.stdout)
        stream = next(
            item
            for item in payload.get("streams", [])
            if item.get("codec_type") == kind
        )
    except (json.JSONDecodeError, StopIteration, TypeError) as exc:
        raise MediaProductionError(
            "missing_media_stream", f"media has no decodable {kind} stream"
        ) from exc
    format_facts = payload.get("format") or {}
    start = _decimal(stream.get("start_time"))
    if start is None:
        start = _decimal(format_facts.get("start_time")) or Decimal(0)
    if start < 0:
        raise MediaProductionError(
            "invalid_media_timeline",
            f"{kind} stream starts before zero; leading preroll is unsupported",
        )
    stream_duration = _decimal(stream.get("duration"))
    format_duration = _decimal(format_facts.get("duration"))
    estimated = False
    if stream_duration is not None:
        end = start + stream_duration
    elif format_duration is not None:
        # Container families disagree: Matroska/WebM and NUT report an
        # absolute timeline end, while FLV and ISO-BMFF report a length. This
        # fallback is disclosed as estimated because it may describe a longer
        # sibling stream rather than this selected stream.
        names = set(str(format_facts.get("format_name") or "").split(","))
        end = (
            format_duration
            if names & _FORMAT_DURATION_IS_END
            else start + format_duration
        )
        estimated = True
    else:
        end = None
    if end is None or end <= start:
        raise MediaProductionError(
            "invalid_media_timeline", f"{kind} stream has no positive duration"
        )
    time_base = str(stream.get("time_base") or "1/1000000").split("/", 1)
    try:
        numerator, denominator = int(time_base[0]), int(time_base[1])
    except (ValueError, IndexError) as exc:
        raise MediaProductionError(
            "invalid_media_timeline", f"{kind} stream has an invalid time base"
        ) from exc
    if numerator <= 0 or denominator <= 0:
        raise MediaProductionError(
            "invalid_media_timeline", f"{kind} stream has an invalid time base"
        )
    return _StreamProbe(
        start_seconds=float(start),
        end_seconds=float(end),
        time_base_num=numerator,
        time_base_den=denominator,
        estimated=estimated,
    )


def _showinfo_points(stderr: str) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    for match in _SHOWINFO_FRAME.finditer(stderr):
        pts_raw, seconds_raw = match.groups()
        if pts_raw == "NOPTS" or seconds_raw == "NOPTS":
            raise MediaProductionError(
                "invalid_media_timeline",
                "ffmpeg returned a decoded frame without a presentation timestamp",
            )
        try:
            pts = int(pts_raw)
            seconds = float(seconds_raw)
        except ValueError as exc:
            raise MediaProductionError(
                "invalid_media_timeline",
                "ffmpeg returned an invalid decoded-frame timestamp",
            ) from exc
        if not math.isfinite(seconds):
            raise MediaProductionError(
                "invalid_media_timeline",
                "ffmpeg returned a non-finite decoded-frame timestamp",
            )
        points.append((pts, seconds))
    return points


def _scale_filter(max_dimension: int) -> str:
    return (
        f"scale=w='min(iw,{max_dimension})':h='min(ih,{max_dimension})':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _select_filter(interval_seconds: float, cuts: Sequence[float] = ()) -> str:
    expression = (
        f"isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval_seconds:.12g})"
    )
    for cut in cuts:
        expression += f"+gte(t\\,{cut:.12g})*lt(prev_selected_t\\,{cut:.12g})"
    return f"select='{expression}'"


async def _decode_video_frames(
    path: Path,
    output: Path,
    *,
    interval_seconds: float,
    max_frames: int,
    max_dimension: int,
    fallback_time_base: tuple[int, int],
    cancelled: CancelCheck | None,
    cuts: Sequence[float] = (),
    output_start_seconds: float | None = None,
    input_start_seconds: float | None = None,
) -> tuple[LocatedFrame, ...]:
    ffmpeg = _binary("ffmpeg")
    output.mkdir()
    filters = [_select_filter(interval_seconds, cuts)]
    if output_start_seconds is not None:
        filters.append(f"select='gte(t\\,{output_start_seconds:.12g})'")
    filters.extend((_scale_filter(max_dimension), "showinfo"))
    filter_chain = ",".join(filters)
    input_args = []
    if input_start_seconds is not None and input_start_seconds > 0:
        input_args = ["-ss", f"{input_start_seconds:.12g}"]
    result = await _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-copyts",
            *input_args,
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            filter_chain,
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(max_frames),
            "-q:v",
            "4",
            "-y",
            str(output / "frame-%06d.jpg"),
        ],
        op="retrieval.media.video_decode",
        read=path,
        write=output,
        cancelled=cancelled,
    )
    time_base_match = _SHOWINFO_TIME_BASE.search(result.stderr)
    if time_base_match is None:
        numerator, denominator = fallback_time_base
    else:
        numerator, denominator = map(int, time_base_match.groups())
    points = _showinfo_points(result.stderr)
    paths = sorted(output.glob("frame-*.jpg"))
    # Encoders can request one filtered frame ahead before the output frame
    # limit closes the muxer.  showinfo observes that frame, while no file is
    # written for it.  Output order is preserved, so only a missing timestamp
    # is an invalid timeline.
    if not paths or len(points) < len(paths):
        raise MediaProductionError(
            "invalid_media_timeline",
            "ffmpeg did not return one presentation timestamp per decoded frame",
        )
    frames: list[LocatedFrame] = []
    for frame_path, (pts, _seconds) in zip(paths, points[: len(paths)]):
        _check_cancelled(cancelled)
        if frame_path.is_symlink() or frame_path.parent.resolve() != output.resolve():
            raise MediaProductionError(
                "media_decode_failed", "decoded frame escaped owned scratch"
            )
        frames.append(
            LocatedFrame(
                point=PresentationPoint(pts, numerator, denominator),
                input=ImageEncodingInput(frame_path.read_bytes()),
            )
        )
    return tuple(frames)


async def _decode_video_frame_at(
    path: Path,
    output: Path,
    *,
    at_seconds: float,
    input_seek_seconds: float,
    max_dimension: int,
    fallback_time_base: tuple[int, int],
    cancelled: CancelCheck | None,
) -> LocatedFrame:
    """Decode the first real frame at or after one source-timeline instant."""

    ffmpeg = _binary("ffmpeg")
    output.mkdir()
    filter_chain = ",".join(
        (
            f"select='gte(t\\,{at_seconds:.12g})'",
            _scale_filter(max_dimension),
            "showinfo",
        )
    )
    result = await _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-copyts",
            "-ss",
            f"{input_seek_seconds:.12g}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            filter_chain,
            "-fps_mode",
            "passthrough",
            "-frames:v",
            "1",
            "-q:v",
            "4",
            "-y",
            str(output / "frame.jpg"),
        ],
        op="retrieval.media.video_anchor_decode",
        read=path,
        write=output,
        cancelled=cancelled,
    )
    frame_path = output / "frame.jpg"
    points = _showinfo_points(result.stderr)
    if not frame_path.is_file() or not points:
        raise MediaProductionError(
            "media_selection_out_of_range",
            "no decoded video frame exists at or after the selected time",
        )
    time_base_match = _SHOWINFO_TIME_BASE.search(result.stderr)
    if time_base_match is None:
        numerator, denominator = fallback_time_base
    else:
        numerator, denominator = map(int, time_base_match.groups())
    point = PresentationPoint(points[0][0], numerator, denominator)
    if point.seconds + 1e-9 < at_seconds:
        raise MediaProductionError(
            "invalid_media_timeline",
            "ffmpeg returned a video frame before the selected time",
        )
    return LocatedFrame(point, ImageEncodingInput(frame_path.read_bytes()))


def _perceptual_sample(jpeg: bytes) -> tuple[int, ...]:
    from io import BytesIO

    with Image.open(BytesIO(jpeg)) as image:
        return tuple(image.convert("L").resize((8, 8)).get_flattened_data())


def _deduplicate(
    frames: Sequence[LocatedFrame], threshold: float | None
) -> tuple[LocatedFrame, ...]:
    if threshold is None:
        return tuple(frames)
    kept: list[LocatedFrame] = []
    previous: tuple[int, ...] | None = None
    for frame in frames:
        sample = _perceptual_sample(frame.input.image_bytes)
        difference = (
            1.0
            if previous is None
            else sum(abs(left - right) for left, right in zip(previous, sample))
            / (len(sample) * 255)
        )
        if previous is None or difference > threshold:
            kept.append(frame)
        previous = sample
    return tuple(kept)


def _fixed_spans(
    start: float, end: float, duration: float, stride: float
) -> list[tuple[float, float]]:
    if end - start <= duration:
        return [(start, end)]
    spans: list[tuple[float, float]] = []
    cursor = start
    tolerance = 1e-9
    while cursor + duration <= end + tolerance:
        spans.append((cursor, min(cursor + duration, end)))
        cursor += stride
    tail_start = end - duration
    if not spans or not math.isclose(spans[-1][0], tail_start, abs_tol=tolerance):
        spans.append((tail_start, end))
    return spans


def _fixed_span_batch(
    start: float,
    end: float,
    duration: float,
    stride: float,
    *,
    start_index: int,
    limit: int,
) -> tuple[list[tuple[float, float]], int]:
    """Calculate one fixed-window page without materializing the full timeline."""

    if start_index < 0:
        raise ValueError("media start_index must be nonnegative")
    length = end - start
    if length <= duration:
        count = 1
        regular_count = 1
        tail_extra = False
    else:
        tolerance = 1e-9
        regular_count = math.floor((length - duration + tolerance) / stride) + 1
        last_regular = start + (regular_count - 1) * stride
        tail_start = end - duration
        tail_extra = not math.isclose(last_regular, tail_start, abs_tol=tolerance)
        count = regular_count + int(tail_extra)
    if start_index >= count:
        raise ValueError("media continuation is beyond the source timeline")
    spans: list[tuple[float, float]] = []
    for index in range(start_index, min(start_index + limit, count)):
        if tail_extra and index == regular_count:
            span_start = end - duration
        elif count == 1:
            span_start = start
        else:
            span_start = start + index * stride
        spans.append((span_start, min(span_start + duration, end)))
    return spans, count


def _shot_spans(
    start: float, end: float, cuts: Sequence[float], max_duration: float
) -> list[tuple[float, float]]:
    boundaries = [start]
    boundaries.extend(cut for cut in cuts if start < cut < end)
    boundaries.append(end)
    spans: list[tuple[float, float]] = []
    for scene_start, scene_end in zip(boundaries, boundaries[1:]):
        cursor = scene_start
        while scene_end - cursor > max_duration:
            spans.append((cursor, cursor + max_duration))
            cursor += max_duration
        if scene_end > cursor:
            spans.append((cursor, scene_end))
    return spans


async def _detect_shot_cuts(
    path: Path,
    *,
    threshold: float,
    min_scene_len_frames: int,
    start_seconds: float,
    end_seconds: float,
    input_seek_seconds: float,
    cancelled: CancelCheck | None,
) -> tuple[float, ...]:
    """Detect cuts inside the same confined, cancellable ffmpeg boundary."""

    ffmpeg = _binary("ffmpeg")
    select = (
        f"select='gt(scene\\,{threshold:.12g})*"
        f"(isnan(prev_selected_n)+gte(n-prev_selected_n\\,{min_scene_len_frames}))',"
        "showinfo"
    )
    result = await _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-copyts",
            "-ss",
            f"{input_seek_seconds:.12g}",
            "-i",
            str(path),
            "-t",
            f"{end_seconds - start_seconds:.12g}",
            "-map",
            "0:v:0",
            "-vf",
            select,
            "-fps_mode",
            "passthrough",
            "-an",
            "-f",
            "null",
            "-",
        ],
        op="retrieval.media.shot_detect",
        read=path,
        write=None,
        cancelled=cancelled,
    )
    return tuple(seconds for _pts, seconds in _showinfo_points(result.stderr))


def _clip_items(
    spans: Sequence[tuple[float, float]], frames: Sequence[LocatedFrame]
) -> tuple[LocatedVideoClip, ...]:
    clips: list[LocatedVideoClip] = []
    for start, end in spans:
        located = tuple(frame for frame in frames if start <= frame.point.seconds < end)
        if not located:
            continue
        inputs = tuple(frame.input for frame in located)
        clips.append(
            LocatedVideoClip(
                start_seconds=start,
                end_seconds=end,
                frames=located,
                input=VideoClipEncodingInput(inputs),
            )
        )
    return tuple(clips)


def _coverage(
    probe: _StreamProbe,
    spans: Sequence[tuple[float, float]],
    *,
    complete: bool,
    reason: Literal[
        "max_frames",
        "max_clips",
        "max_windows",
        "missing_samples",
        "timeline_quantum",
    ]
    | None,
) -> MediaCoverage:
    return MediaCoverage(
        source_start_seconds=probe.start_seconds,
        source_end_seconds=probe.end_seconds,
        indexed_spans=tuple(spans),
        complete=complete,
        truncation_reason=reason,
        timeline_estimated=probe.estimated,
    )


async def _produce_frames(
    path: Path,
    scratch: Path,
    recipe: PeriodicFrameRecipe,
    cancelled: CancelCheck | None,
    start_index: int = 0,
) -> ProducedMedia:
    probe = await _probe(path, "video", cancelled)
    planned = max(
        1,
        math.ceil((probe.end_seconds - probe.start_seconds) / recipe.interval_seconds),
    )
    if start_index >= planned:
        raise ValueError("media continuation is beyond the source timeline")
    requested = min(planned - start_index, recipe.max_frames)
    complete = start_index + requested >= planned
    output_start = probe.start_seconds + start_index * recipe.interval_seconds
    include_predecessor = (
        start_index > 0 and recipe.adjacent_dedup_threshold is not None
    )
    decode_start = (
        output_start - recipe.interval_seconds if include_predecessor else output_start
    )
    decoded = await _decode_video_frames(
        path,
        scratch / "frames",
        interval_seconds=recipe.interval_seconds,
        max_frames=requested + int(include_predecessor),
        max_dimension=recipe.max_dimension,
        fallback_time_base=(probe.time_base_num, probe.time_base_den),
        cancelled=cancelled,
        output_start_seconds=decode_start,
        input_start_seconds=decode_start - probe.start_seconds,
    )
    current = tuple(
        frame for frame in decoded if frame.point.seconds + 1e-9 >= output_start
    )
    missing_samples = len(current) < requested
    frames = tuple(
        frame
        for frame in _deduplicate(decoded, recipe.adjacent_dedup_threshold)
        if frame.point.seconds + 1e-9 >= output_start
    )
    spans = tuple((frame.point.seconds, frame.point.seconds) for frame in frames)
    return ProducedMedia(
        items=frames,
        coverage=_coverage(
            probe,
            spans,
            complete=complete and not missing_samples,
            reason=(
                "max_frames"
                if not complete
                else "missing_samples"
                if missing_samples
                else None
            ),
        ),
    )


async def _produce_fixed_clips(
    path: Path,
    scratch: Path,
    recipe: FixedClipRecipe,
    cancelled: CancelCheck | None,
    start_index: int = 0,
) -> ProducedMedia:
    probe = await _probe(path, "video", cancelled)
    spans, total = _fixed_span_batch(
        probe.start_seconds,
        probe.end_seconds,
        recipe.duration_seconds,
        recipe.stride_seconds,
        start_index=start_index,
        limit=recipe.max_clips,
    )
    complete = start_index + len(spans) >= total
    coverage_end = spans[-1][1]
    decoded_limit = min(
        recipe.max_clips * EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
        max(1, math.ceil((coverage_end - spans[0][0]) * recipe.sample_fps) + 1),
    )
    frames = await _decode_video_frames(
        path,
        scratch / "frames",
        interval_seconds=1 / recipe.sample_fps,
        max_frames=decoded_limit,
        max_dimension=recipe.max_dimension,
        fallback_time_base=(probe.time_base_num, probe.time_base_den),
        cancelled=cancelled,
        output_start_seconds=spans[0][0],
        input_start_seconds=spans[0][0] - probe.start_seconds,
    )
    clips = _clip_items(spans, frames)
    reason = (
        "missing_samples"
        if len(clips) != len(spans)
        else None
        if complete
        else "max_clips"
    )
    return ProducedMedia(
        items=clips,
        coverage=_coverage(
            probe,
            [(clip.start_seconds, clip.end_seconds) for clip in clips],
            complete=complete and len(clips) == len(spans),
            reason=reason,
        ),
    )


async def _produce_shot_clips(
    path: Path,
    scratch: Path,
    recipe: ShotClipRecipe,
    cancelled: CancelCheck | None,
    start_seconds: float | None = None,
) -> ProducedMedia:
    probe = await _probe(path, "video", cancelled)
    quantum_start = probe.start_seconds if start_seconds is None else start_seconds
    if not math.isfinite(quantum_start) or not (
        probe.start_seconds <= quantum_start < probe.end_seconds
    ):
        raise ValueError("shot continuation is beyond the source timeline")
    quantum_end = min(probe.end_seconds, quantum_start + recipe.quantum_seconds)
    _check_cancelled(cancelled)
    cut_seconds = await _detect_shot_cuts(
        path,
        threshold=recipe.scene_score_threshold,
        min_scene_len_frames=recipe.min_scene_len_frames,
        start_seconds=quantum_start,
        end_seconds=quantum_end,
        input_seek_seconds=quantum_start - probe.start_seconds,
        cancelled=cancelled,
    )
    _check_cancelled(cancelled)
    planned = _shot_spans(
        quantum_start,
        quantum_end,
        cut_seconds,
        recipe.max_duration_seconds,
    )
    complete = quantum_end >= probe.end_seconds and len(planned) <= recipe.max_clips
    spans = planned[: recipe.max_clips]
    coverage_end = spans[-1][1]
    decoded_limit = min(
        recipe.max_clips * EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
        max(
            1,
            math.ceil((coverage_end - quantum_start) * recipe.sample_fps)
            + len(spans)
            + 1,
        ),
    )
    frames = await _decode_video_frames(
        path,
        scratch / "frames",
        interval_seconds=1 / recipe.sample_fps,
        max_frames=decoded_limit,
        max_dimension=recipe.max_dimension,
        fallback_time_base=(probe.time_base_num, probe.time_base_den),
        cancelled=cancelled,
        cuts=tuple(start for start, _end in spans[1:]),
        output_start_seconds=quantum_start,
        input_start_seconds=quantum_start - probe.start_seconds,
    )
    clips = _clip_items(spans, frames)
    reason = (
        "missing_samples"
        if len(clips) != len(spans)
        else None
        if complete
        else "max_clips"
        if len(planned) > recipe.max_clips
        else "timeline_quantum"
    )
    return ProducedMedia(
        items=clips,
        coverage=_coverage(
            probe,
            [(clip.start_seconds, clip.end_seconds) for clip in clips],
            complete=complete and len(clips) == len(spans),
            reason=reason,
        ),
    )


async def _produce_audio(
    path: Path,
    scratch: Path,
    recipe: AudioWindowRecipe,
    cancelled: CancelCheck | None,
    start_index: int = 0,
) -> ProducedMedia:
    probe = await _probe(path, "audio", cancelled)
    spans, total = _fixed_span_batch(
        probe.start_seconds,
        probe.end_seconds,
        recipe.duration_seconds,
        recipe.stride_seconds,
        start_index=start_index,
        limit=recipe.max_windows,
    )
    complete = start_index + len(spans) >= total
    coverage_start = spans[0][0]
    coverage_end = spans[-1][1]
    output = scratch / "audio.pcm"
    ffmpeg = _binary("ffmpeg")
    await _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-ss",
            f"{coverage_start - probe.start_seconds:.12g}",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            "asetpts=PTS-STARTPTS,aresample=16000:async=1:first_pts=0",
            "-t",
            f"{coverage_end - coverage_start:.12g}",
            "-ac",
            "1",
            "-ar",
            str(EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-y",
            str(output),
        ],
        op="retrieval.media.audio_decode",
        read=path,
        write=scratch,
        cancelled=cancelled,
    )
    decoded_bytes = output.stat().st_size
    if decoded_bytes % _PCM_BYTES_PER_SAMPLE:
        decoded_bytes -= 1
    decoded_duration = (
        decoded_bytes / _PCM_BYTES_PER_SAMPLE / EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
    )
    sample_tolerance = 0.5 / EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
    if decoded_duration <= sample_tolerance:
        raise MediaProductionError(
            "media_decode_failed", "ffmpeg returned no decodable audio samples"
        )
    decoded_end = coverage_start + decoded_duration
    if decoded_end + sample_tolerance < coverage_end:
        probe = _StreamProbe(
            start_seconds=probe.start_seconds,
            end_seconds=decoded_end,
            time_base_num=probe.time_base_num,
            time_base_den=probe.time_base_den,
            estimated=False,
        )
        spans = [
            (start, min(end, decoded_end))
            for start, end in spans
            if start < decoded_end
        ]
        complete = True
    windows: list[AudioWindow] = []
    missing_samples = False
    with output.open("rb") as pcm:
        for start, end in spans:
            _check_cancelled(cancelled)
            first_sample = round(
                (start - coverage_start) * EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
            )
            sample_count = round((end - start) * EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE)
            pcm.seek(first_sample * _PCM_BYTES_PER_SAMPLE)
            data = pcm.read(sample_count * _PCM_BYTES_PER_SAMPLE)
            if not data:
                missing_samples = True
                continue
            if len(data) % _PCM_BYTES_PER_SAMPLE:
                data = data[:-1]
            if not data:
                missing_samples = True
                continue
            actual_end = start + (
                len(data) / _PCM_BYTES_PER_SAMPLE / EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
            )
            if actual_end + sample_tolerance < end:
                missing_samples = True
            windows.append(
                AudioWindow(
                    start_seconds=start,
                    end_seconds=actual_end,
                    input=AudioEncodingInput(data),
                )
            )
    reason = (
        "missing_samples"
        if missing_samples or len(windows) != len(spans)
        else None
        if complete
        else "max_windows"
    )
    return ProducedMedia(
        items=tuple(windows),
        coverage=_coverage(
            probe,
            [(window.start_seconds, window.end_seconds) for window in windows],
            complete=complete and not missing_samples and len(windows) == len(spans),
            reason=reason,
        ),
    )


async def decode_video_frame(
    source: BoundLocalFile | _MaterializedMedia,
    at_seconds: float,
    *,
    max_dimension: int = 448,
    cancelled: CancelCheck | None = None,
) -> LocatedFrame:
    """Decode one selected frame and report its actual source-timeline PTS."""

    selected = _nonnegative_number(at_seconds, "at_seconds")
    dimension = _dimension(max_dimension)
    _check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(
        prefix="frisket-retrieval-video-anchor-"
    ) as directory:
        scratch = Path(directory)
        async with _readable_media_path(source, scratch, cancelled) as staged:
            probe = await _probe(staged, "video", cancelled)
            if not probe.start_seconds <= selected < probe.end_seconds:
                raise MediaProductionError(
                    "media_selection_out_of_range",
                    "selected video time is outside the source timeline",
                )
            return await _decode_video_frame_at(
                staged,
                scratch / "frame",
                at_seconds=selected,
                input_seek_seconds=selected - probe.start_seconds,
                max_dimension=dimension,
                fallback_time_base=(probe.time_base_num, probe.time_base_den),
                cancelled=cancelled,
            )


async def decode_audio_interval(
    source: BoundLocalFile | _MaterializedMedia,
    start_seconds: float,
    end_seconds: float,
    *,
    cancelled: CancelCheck | None = None,
) -> AudioWindow:
    """Decode one explicit interval to signed mono PCM16LE at 16 kHz."""

    start = _nonnegative_number(start_seconds, "start_seconds")
    end = _nonnegative_number(end_seconds, "end_seconds")
    if end <= start:
        raise ValueError("selected audio interval must have positive duration")
    if end - start > _MAX_AUDIO_SECONDS:
        raise ValueError("selected audio interval must not exceed 30 seconds")
    _check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(
        prefix="frisket-retrieval-audio-anchor-"
    ) as directory:
        scratch = Path(directory)
        async with _readable_media_path(source, scratch, cancelled) as staged:
            probe = await _probe(staged, "audio", cancelled)
            if start < probe.start_seconds or start >= probe.end_seconds:
                raise MediaProductionError(
                    "media_selection_out_of_range",
                    "selected audio interval starts outside the source timeline",
                )
            selected_end = min(end, probe.end_seconds)
            output = scratch / "audio.pcm"
            await _run_tool(
                [
                    _binary("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                    "-ss",
                    f"{start - probe.start_seconds:.12g}",
                    "-i",
                    str(staged),
                    "-t",
                    f"{selected_end - start:.12g}",
                    "-map",
                    "0:a:0",
                    "-af",
                    "asetpts=PTS-STARTPTS,aresample=16000:async=1:first_pts=0",
                    "-ac",
                    "1",
                    "-ar",
                    str(EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "s16le",
                    "-y",
                    str(output),
                ],
                op="retrieval.media.audio_anchor_decode",
                read=staged,
                write=scratch,
                cancelled=cancelled,
            )
            data = output.read_bytes()
            if len(data) % _PCM_BYTES_PER_SAMPLE:
                data = data[:-1]
            max_samples = round(
                (selected_end - start) * EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
            )
            data = data[: max_samples * _PCM_BYTES_PER_SAMPLE]
            if not data:
                raise MediaProductionError(
                    "media_selection_out_of_range",
                    "selected audio interval contains no decoded samples",
                )
            actual_end = start + (
                len(data) / _PCM_BYTES_PER_SAMPLE / EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
            )
            return AudioWindow(start, actual_end, AudioEncodingInput(data))


async def produce_media(
    source: BoundLocalFile | _MaterializedMedia,
    recipe: MediaRecipe,
    *,
    cancelled: CancelCheck | None = None,
    start_index: int = 0,
    start_seconds: float | None = None,
) -> ProducedMedia:
    """Produce bounded, located native-encoder inputs from admitted media."""

    _check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix="frisket-retrieval-media-") as directory:
        scratch = Path(directory)
        async with _readable_media_path(source, scratch, cancelled) as staged:
            if isinstance(start_index, bool) or not isinstance(start_index, int):
                raise TypeError("start_index must be an integer")
            if start_index < 0:
                raise ValueError("start_index must be nonnegative")
            if isinstance(recipe, PeriodicFrameRecipe):
                if start_seconds is not None:
                    raise ValueError("periodic frame continuation uses start_index")
                return await _produce_frames(
                    staged, scratch, recipe, cancelled, start_index
                )
            if isinstance(recipe, FixedClipRecipe):
                if start_seconds is not None:
                    raise ValueError("fixed clip continuation uses start_index")
                return await _produce_fixed_clips(
                    staged, scratch, recipe, cancelled, start_index
                )
            if isinstance(recipe, ShotClipRecipe):
                if start_index:
                    raise ValueError("shot clip continuation uses start_seconds")
                return await _produce_shot_clips(
                    staged, scratch, recipe, cancelled, start_seconds
                )
            if isinstance(recipe, AudioWindowRecipe):
                if start_seconds is not None:
                    raise ValueError("audio continuation uses start_index")
                return await _produce_audio(
                    staged, scratch, recipe, cancelled, start_index
                )
            raise TypeError("recipe must be a supported media producer recipe")


__all__ = [
    "AudioWindow",
    "AudioWindowRecipe",
    "decode_audio_interval",
    "decode_video_frame",
    "FixedClipRecipe",
    "LocatedFrame",
    "LocatedMediaInput",
    "LocatedVideoClip",
    "MediaCoverage",
    "MediaProductionError",
    "MediaRecipe",
    "PeriodicFrameRecipe",
    "PresentationPoint",
    "ProducedMedia",
    "ShotClipRecipe",
    "produce_media",
]
