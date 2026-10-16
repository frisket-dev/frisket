"""Bounded media part producers with source-timeline locations.

The caller supplies an already-admitted :class:`BoundLocalFile`.  This module
copies and verifies that borrowed stream into owned scratch before invoking
the existing confined ffmpeg boundary; it never accepts a URL or an arbitrary
filesystem path.  Encoder payloads contain only representation bytes.  Exact
presentation timestamps and intervals stay on the located wrappers here.
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
_COPY_CHUNK_BYTES = 1024 * 1024
_SHOWINFO_POINT = re.compile(r"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:\s*([-+\d.eE]+)")
_SHOWINFO_TIME_BASE = re.compile(r"config in time_base:\s*(\d+)\s*/\s*(\d+)")


class MediaProductionError(RuntimeError):
    """Actionable refusal or decoder failure at the media producer boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    checked = float(value)
    if not math.isfinite(checked) or checked <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
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


@dataclass(frozen=True, slots=True)
class ShotClipRecipe:
    max_duration_seconds: float = 8.0
    sample_fps: float = 1.0
    max_clips: int = 450
    detector_threshold: float = 27.0
    min_scene_len_frames: int = 15
    max_dimension: int = 448
    strategy: Literal["shot_clips"] = "shot_clips"

    def __post_init__(self) -> None:
        duration = _positive_number(self.max_duration_seconds, "max_duration_seconds")
        fps = _positive_number(self.sample_fps, "sample_fps")
        threshold = _positive_number(self.detector_threshold, "detector_threshold")
        if math.ceil(duration * fps) > EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES:
            raise ValueError("a shot recipe must produce at most 32 sampled frames")
        object.__setattr__(self, "max_duration_seconds", duration)
        object.__setattr__(self, "sample_fps", fps)
        object.__setattr__(
            self, "max_clips", _positive_count(self.max_clips, "max_clips")
        )
        object.__setattr__(self, "detector_threshold", threshold)
        object.__setattr__(
            self,
            "min_scene_len_frames",
            _positive_count(self.min_scene_len_frames, "min_scene_len_frames"),
        )
        object.__setattr__(self, "max_dimension", _dimension(self.max_dimension))


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
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "stride_seconds", stride)
        object.__setattr__(
            self, "max_windows", _positive_count(self.max_windows, "max_windows")
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
        Literal["max_frames", "max_clips", "max_windows", "missing_samples"] | None
    ) = None


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


def _expected_digest(source: BoundLocalFile) -> str:
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
            with contextlib.suppress(OSError, ValueError):
                stream.seek(original_position)
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise MediaProductionError(
            "stale_media_source", "admitted media bytes no longer match their SHA-256"
        )


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
            "format=start_time,duration:stream=index,codec_type,start_time,duration,time_base",
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
    stream_duration = _decimal(stream.get("duration"))
    format_duration = _decimal(format_facts.get("duration"))
    if stream_duration is not None:
        end = start + stream_duration
    elif format_duration is not None:
        # ffprobe's container duration is measured on the container timeline.
        # In particular, Matroska with a delayed first track can report
        # start=0.375,duration=1.375 for content ending at 1.375.  Adding the
        # offset again would manufacture a nonexistent tail.
        end = format_duration
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
    )


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
) -> tuple[LocatedFrame, ...]:
    ffmpeg = _binary("ffmpeg")
    output.mkdir()
    filter_chain = ",".join(
        (
            _select_filter(interval_seconds, cuts),
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
    points = [int(match.group(1)) for match in _SHOWINFO_POINT.finditer(result.stderr)]
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
    for frame_path, pts in zip(paths, points[: len(paths)]):
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
    cancelled: CancelCheck | None,
) -> tuple[float, ...]:
    """Run PySceneDetect while retaining bounded cooperative cancellation."""

    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(
        ContentDetector(
            threshold=threshold,
            min_scene_len=min_scene_len_frames,
        )
    )
    detection = asyncio.create_task(
        asyncio.to_thread(manager.detect_scenes, video=video, show_progress=False)
    )
    try:
        while not detection.done():
            _check_cancelled(cancelled)
            await asyncio.wait({detection}, timeout=0.1)
        await detection
    except BaseException:
        manager.stop()
        # PySceneDetect owns a decoder thread.  Settle it before scratch can be
        # removed, even when the surrounding asyncio task was cancelled.
        settling_cancel = False
        while not detection.done():
            try:
                await asyncio.shield(detection)
            except asyncio.CancelledError:
                settling_cancel = True
        with contextlib.suppress(asyncio.CancelledError):
            detection.result()
        if settling_cancel:
            raise asyncio.CancelledError
        raise
    return tuple(
        float(start.seconds)
        for start, _end in manager.get_scene_list(start_in_scene=True)[1:]
    )


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
    reason: Literal["max_frames", "max_clips", "max_windows", "missing_samples"] | None,
) -> MediaCoverage:
    return MediaCoverage(
        source_start_seconds=probe.start_seconds,
        source_end_seconds=probe.end_seconds,
        indexed_spans=tuple(spans),
        complete=complete,
        truncation_reason=reason,
    )


async def _produce_frames(
    path: Path,
    scratch: Path,
    recipe: PeriodicFrameRecipe,
    cancelled: CancelCheck | None,
) -> ProducedMedia:
    probe = await _probe(path, "video", cancelled)
    planned = max(
        1,
        math.ceil((probe.end_seconds - probe.start_seconds) / recipe.interval_seconds),
    )
    complete = planned <= recipe.max_frames
    frames = await _decode_video_frames(
        path,
        scratch / "frames",
        interval_seconds=recipe.interval_seconds,
        max_frames=min(planned, recipe.max_frames),
        max_dimension=recipe.max_dimension,
        fallback_time_base=(probe.time_base_num, probe.time_base_den),
        cancelled=cancelled,
    )
    frames = _deduplicate(frames, recipe.adjacent_dedup_threshold)
    spans = tuple((frame.point.seconds, frame.point.seconds) for frame in frames)
    return ProducedMedia(
        items=frames,
        coverage=_coverage(
            probe,
            spans,
            complete=complete,
            reason=None if complete else "max_frames",
        ),
    )


async def _produce_fixed_clips(
    path: Path,
    scratch: Path,
    recipe: FixedClipRecipe,
    cancelled: CancelCheck | None,
) -> ProducedMedia:
    probe = await _probe(path, "video", cancelled)
    planned = _fixed_spans(
        probe.start_seconds,
        probe.end_seconds,
        recipe.duration_seconds,
        recipe.stride_seconds,
    )
    complete = len(planned) <= recipe.max_clips
    spans = planned[: recipe.max_clips]
    coverage_end = spans[-1][1]
    decoded_limit = min(
        recipe.max_clips * EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
        max(1, math.ceil((coverage_end - probe.start_seconds) * recipe.sample_fps) + 1),
    )
    frames = await _decode_video_frames(
        path,
        scratch / "frames",
        interval_seconds=1 / recipe.sample_fps,
        max_frames=decoded_limit,
        max_dimension=recipe.max_dimension,
        fallback_time_base=(probe.time_base_num, probe.time_base_den),
        cancelled=cancelled,
    )
    clips = _clip_items(spans, frames)
    reason = None if complete else "max_clips"
    if len(clips) != len(spans) and reason is None:
        reason = "missing_samples"
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
) -> ProducedMedia:
    probe = await _probe(path, "video", cancelled)
    _check_cancelled(cancelled)
    relative_cuts = await _detect_shot_cuts(
        path,
        threshold=recipe.detector_threshold,
        min_scene_len_frames=recipe.min_scene_len_frames,
        cancelled=cancelled,
    )
    _check_cancelled(cancelled)
    cut_seconds = tuple(probe.start_seconds + cut for cut in relative_cuts)
    planned = _shot_spans(
        probe.start_seconds,
        probe.end_seconds,
        cut_seconds,
        recipe.max_duration_seconds,
    )
    complete = len(planned) <= recipe.max_clips
    spans = planned[: recipe.max_clips]
    coverage_end = spans[-1][1]
    decoded_limit = min(
        recipe.max_clips * EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
        max(
            1,
            math.ceil((coverage_end - probe.start_seconds) * recipe.sample_fps)
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
    )
    clips = _clip_items(spans, frames)
    reason = None if complete else "max_clips"
    if len(clips) != len(spans) and reason is None:
        reason = "missing_samples"
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
) -> ProducedMedia:
    probe = await _probe(path, "audio", cancelled)
    planned = _fixed_spans(
        probe.start_seconds,
        probe.end_seconds,
        recipe.duration_seconds,
        recipe.stride_seconds,
    )
    complete = len(planned) <= recipe.max_windows
    spans = planned[: recipe.max_windows]
    coverage_end = spans[-1][1]
    output = scratch / "audio.pcm"
    ffmpeg = _binary("ffmpeg")
    await _run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-t",
            f"{coverage_end - probe.start_seconds:.12g}",
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
    windows: list[AudioWindow] = []
    with output.open("rb") as pcm:
        for start, end in spans:
            _check_cancelled(cancelled)
            first_sample = round(
                (start - probe.start_seconds) * EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
            )
            sample_count = round((end - start) * EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE)
            pcm.seek(first_sample * _PCM_BYTES_PER_SAMPLE)
            data = pcm.read(sample_count * _PCM_BYTES_PER_SAMPLE)
            if not data:
                continue
            if len(data) % _PCM_BYTES_PER_SAMPLE:
                data = data[:-1]
            actual_end = start + (
                len(data) / _PCM_BYTES_PER_SAMPLE / EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE
            )
            windows.append(
                AudioWindow(
                    start_seconds=start,
                    end_seconds=actual_end,
                    input=AudioEncodingInput(data),
                )
            )
    reason = None if complete else "max_windows"
    if len(windows) != len(spans) and reason is None:
        reason = "missing_samples"
    return ProducedMedia(
        items=tuple(windows),
        coverage=_coverage(
            probe,
            [(window.start_seconds, window.end_seconds) for window in windows],
            complete=complete and len(windows) == len(spans),
            reason=reason,
        ),
    )


async def produce_media(
    source: BoundLocalFile,
    recipe: MediaRecipe,
    *,
    cancelled: CancelCheck | None = None,
) -> ProducedMedia:
    """Produce bounded, located native-encoder inputs from admitted media."""

    _check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix="frisket-retrieval-media-") as directory:
        scratch = Path(directory)
        staged = scratch / "source.media"
        await _stage_source(source, staged, cancelled)
        if isinstance(recipe, PeriodicFrameRecipe):
            return await _produce_frames(staged, scratch, recipe, cancelled)
        if isinstance(recipe, FixedClipRecipe):
            return await _produce_fixed_clips(staged, scratch, recipe, cancelled)
        if isinstance(recipe, ShotClipRecipe):
            return await _produce_shot_clips(staged, scratch, recipe, cancelled)
        if isinstance(recipe, AudioWindowRecipe):
            return await _produce_audio(staged, scratch, recipe, cancelled)
        raise TypeError("recipe must be a supported media producer recipe")


__all__ = [
    "AudioWindow",
    "AudioWindowRecipe",
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
