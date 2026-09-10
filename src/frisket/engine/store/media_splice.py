"""Render one audio or video range into a caller-owned staging path.

The action layer renders every requested clip before publishing any of them.
This module only runs ffmpeg, checks the resulting file with ffprobe, and
returns the facts needed by that later publication step.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed


MediaKind = Literal["audio", "video"]

RENDERER_PROFILE_VERSION = "frisket.media_splice.v1"
VIDEO_OUTPUT_MIME = "video/mp4"
AUDIO_OUTPUT_MIME = "audio/flac"

_VIDEO_SUFFIX = ".mp4"
_AUDIO_SUFFIX = ".flac"
_VIDEO_FORMAT = "mp4"
_AUDIO_FORMAT = "flac"
_VIDEO_CODEC = "h264"
_VIDEO_AUDIO_CODEC = "aac"
_AUDIO_CODEC = "flac"
_VIDEO_ALIGNMENT_TOLERANCE_MS = 100
_AUDIO_ALIGNMENT_TOLERANCE_MS = 20
_VIDEO_SEEK_PREROLL_MS = 10_000
_MIN_RENDER_WALL_SECONDS = 120
_MAX_RENDER_WALL_SECONDS = 6 * 60 * 60

# ffmpeg receives an already-open file as stdin and is allowed to open only
# that descriptor. Uploaded playlists therefore cannot fetch network URLs or
# open another host path through a nested libavformat protocol.
_MEDIA_INPUT_URL = "fd:"
_MEDIA_INPUT_OPTIONS = ["-protocol_whitelist", "fd"]


class MediaSpliceError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class MediaStreamProbe:
    index: int
    codec_type: str
    codec_name: str | None
    start_time: str | None
    duration: str | None
    time_base: str | None = None
    avg_frame_rate: str | None = None
    r_frame_rate: str | None = None
    width: int | None = None
    height: int | None = None
    sample_rate: int | None = None
    channels: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items() if value is not None}


@dataclass(frozen=True)
class MediaSpliceProbe:
    duration_ms: int
    size_bytes: int
    format_name: str | None
    streams: tuple[MediaStreamProbe, ...]
    format_start_time: str | None = None
    bit_rate: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "format_name": self.format_name,
            "streams": [stream.as_dict() for stream in self.streams],
        }


@dataclass(frozen=True)
class StagedMediaSplice:
    path: Path
    filename: str
    mime: str
    media_kind: MediaKind
    requested_start_ms: int
    requested_end_ms: int
    resolved_source_start_ms: int
    resolved_source_end_ms: int
    duration_ms: int
    alignment_error_ms: int
    alignment_tolerance_ms: int
    precision: str
    renderer_profile: str
    renderer_params: Mapping[str, Any]
    probe: MediaSpliceProbe

    def receipt_metadata(self) -> dict[str, Any]:
        return {
            "renderer_profile": self.renderer_profile,
            "renderer_params": dict(self.renderer_params),
            "media_kind": self.media_kind,
            "mime": self.mime,
            "filename": self.filename,
            "requested_source_range": {
                "start_ms": self.requested_start_ms,
                "end_ms": self.requested_end_ms,
            },
            "resolved_source_range": {
                "start_ms": self.resolved_source_start_ms,
                "end_ms": self.resolved_source_end_ms,
            },
            "duration_ms": self.duration_ms,
            "alignment_error_ms": self.alignment_error_ms,
            "alignment_tolerance_ms": self.alignment_tolerance_ms,
            "precision": self.precision,
            "probe": self.probe.as_dict(),
        }


def _strict_nonnegative_ms(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MediaSpliceError(
            "invalid_range", f"{name} must be nonnegative integer milliseconds"
        )
    return value


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def ffmpeg_splice_argv(
    source_path: str | Path,
    output_path: str | Path,
    *,
    media_kind: MediaKind,
    start_ms: int,
    end_ms: int,
) -> list[str]:
    """Build the one decode/re-encode command used by Extract and Split."""

    del source_path  # The opened source descriptor is the input capability.
    start = _strict_nonnegative_ms("start_ms", start_ms)
    end = _strict_nonnegative_ms("end_ms", end_ms)
    if end <= start:
        raise MediaSpliceError("invalid_range", "end_ms must be greater than start_ms")
    if media_kind not in {"audio", "video"}:
        raise MediaSpliceError(
            "invalid_media_kind", "media_kind must be audio or video"
        )

    coarse_seek_ms = max(0, start - _VIDEO_SEEK_PREROLL_MS)
    precise_seek_ms = start - coarse_seek_ms
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
    ]
    if coarse_seek_ms:
        command.extend(["-ss", _seconds(coarse_seek_ms)])
    command.extend([*_MEDIA_INPUT_OPTIONS, "-i", _MEDIA_INPUT_URL])
    if precise_seek_ms:
        command.extend(["-ss", _seconds(precise_seek_ms)])
    command.extend(
        [
            "-t",
            _seconds(end - start),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
        ]
    )

    output = str(output_path)
    if media_kind == "audio":
        return [
            *command,
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "flac",
            "-compression_level",
            "8",
            "-avoid_negative_ts",
            "make_zero",
            output,
        ]
    return [
        *command,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
        "-fps_mode",
        "passthrough",
        "-metadata:s:v:0",
        "rotate=0",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "disabled",
        output,
    ]


def ffprobe_splice_argv(path: str | Path) -> list[str]:
    del path  # Like ffmpeg, ffprobe reads the already-open source descriptor.
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,start_time,format_name,size,bit_rate:"
            "stream=index,codec_type,codec_name,time_base,start_time,duration,"
            "avg_frame_rate,r_frame_rate,width,height,sample_rate,channels,"
            "channel_layout"
        ),
        "-of",
        "json",
        *_MEDIA_INPUT_OPTIONS,
        _MEDIA_INPUT_URL,
    ]


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_splice_probe(body: Any, *, path: str | Path) -> MediaSpliceProbe:
    if not isinstance(body, Mapping):
        raise MediaSpliceError("probe_failed", "ffprobe returned a non-object payload")
    raw_format = body.get("format")
    fmt = raw_format if isinstance(raw_format, Mapping) else {}
    raw_streams = body.get("streams")
    stream_values = raw_streams if isinstance(raw_streams, list) else []

    duration = _finite_float(fmt.get("duration"))
    if duration is None:
        duration = max(
            (
                parsed
                for raw in stream_values
                if isinstance(raw, Mapping)
                and (parsed := _finite_float(raw.get("duration"))) is not None
            ),
            default=None,
        )
    if duration is None or duration <= 0:
        raise MediaSpliceError("probe_failed", "ffprobe returned no positive duration")

    streams: list[MediaStreamProbe] = []
    for raw in stream_values:
        if not isinstance(raw, Mapping):
            continue
        index = _optional_int(raw.get("index"))
        codec_type = raw.get("codec_type")
        if index is None or not isinstance(codec_type, str):
            continue
        streams.append(
            MediaStreamProbe(
                index=index,
                codec_type=codec_type,
                codec_name=str(raw["codec_name"]) if raw.get("codec_name") else None,
                start_time=(
                    str(raw["start_time"])
                    if raw.get("start_time") is not None
                    else None
                ),
                duration=str(raw["duration"]) if raw.get("duration") else None,
                time_base=str(raw["time_base"]) if raw.get("time_base") else None,
                avg_frame_rate=(
                    str(raw["avg_frame_rate"]) if raw.get("avg_frame_rate") else None
                ),
                r_frame_rate=(
                    str(raw["r_frame_rate"]) if raw.get("r_frame_rate") else None
                ),
                width=_optional_int(raw.get("width")),
                height=_optional_int(raw.get("height")),
                sample_rate=_optional_int(raw.get("sample_rate")),
                channels=_optional_int(raw.get("channels")),
            )
        )

    staged = Path(path).resolve()
    size = staged.stat().st_size if staged.is_file() else _optional_int(fmt.get("size"))
    if size is None or size <= 0:
        raise MediaSpliceError("probe_failed", "staged media has no positive size")
    return MediaSpliceProbe(
        duration_ms=round(duration * 1000),
        size_bytes=size,
        format_name=str(fmt["format_name"]) if fmt.get("format_name") else None,
        streams=tuple(streams),
        format_start_time=(
            str(fmt["start_time"]) if fmt.get("start_time") is not None else None
        ),
        bit_rate=_optional_int(fmt.get("bit_rate")),
    )


async def probe_staged_media(
    path: str | Path,
    *,
    scratch_dir: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> MediaSpliceProbe:
    staged = Path(path).resolve()
    scratch = Path(scratch_dir).resolve() if scratch_dir is not None else staged.parent
    with staged.open("rb") as source_stream:
        result = await run_sandboxed(
            ffprobe_splice_argv(staged),
            policy=SandboxPolicy(
                cpu_seconds=60,
                memory_mb=512,
                wall_seconds=60,
                allow_network=False,
                env_passthrough=["PATH"],
            ),
            stdin_file=source_stream,
            scratch_dir=scratch,
            should_cancel=should_cancel,
        )
    if result.cancelled:
        raise MediaSpliceError("cancelled", "media probe was cancelled")
    if not result.ok:
        raise MediaSpliceError(
            "probe_failed",
            "ffprobe could not inspect the staged media",
            details={"stderr": result.stderr[:500], "timed_out": result.timed_out},
        )
    try:
        body = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaSpliceError("probe_failed", "ffprobe returned invalid JSON") from exc
    return parse_splice_probe(body, path=staged)


def _format_tokens(format_name: str | None) -> set[str]:
    return {token.strip().lower() for token in (format_name or "").split(",")}


def _validate_probe(
    probe: MediaSpliceProbe,
    *,
    media_kind: MediaKind,
    requested_duration_ms: int,
    tolerance_ms: int,
) -> None:
    streams = probe.streams
    if media_kind == "audio":
        valid_profile = (
            _AUDIO_FORMAT in _format_tokens(probe.format_name)
            and len(streams) == 1
            and streams[0].codec_type == "audio"
            and streams[0].codec_name == _AUDIO_CODEC
        )
        primary = streams[0] if valid_profile else None
    else:
        videos = [stream for stream in streams if stream.codec_type == "video"]
        audios = [stream for stream in streams if stream.codec_type == "audio"]
        valid_profile = (
            _VIDEO_FORMAT in _format_tokens(probe.format_name)
            and len(videos) == 1
            and videos[0].codec_name == _VIDEO_CODEC
            and len(audios) <= 1
            and all(stream.codec_name == _VIDEO_AUDIO_CODEC for stream in audios)
            and len(videos) + len(audios) == len(streams)
        )
        primary = videos[0] if valid_profile else None
    if primary is None:
        raise MediaSpliceError(
            "probe_failed",
            "staged output does not match the expected media profile",
            details={
                "media_kind": media_kind,
                "format": probe.format_name,
                "streams": [stream.as_dict() for stream in streams],
            },
        )

    start_seconds = _finite_float(primary.start_time)
    duration_seconds = _finite_float(primary.duration)
    if start_seconds is None or start_seconds < 0:
        raise MediaSpliceError("probe_failed", "staged output has no valid start time")
    primary_start_ms = start_seconds * 1000
    if primary_start_ms > tolerance_ms:
        raise MediaSpliceError(
            "cut_alignment_failed",
            "staged output starts too late for the requested range",
            details={
                "start_ms": primary_start_ms,
                "alignment_tolerance_ms": tolerance_ms,
            },
        )

    endpoints = [probe.duration_ms]
    if duration_seconds is not None and duration_seconds > 0:
        endpoints.append(round((start_seconds + duration_seconds) * 1000))
    if any(
        abs(endpoint - requested_duration_ms) > tolerance_ms for endpoint in endpoints
    ):
        raise MediaSpliceError(
            "cut_alignment_failed",
            "staged output duration differs from the requested range",
            details={
                "requested_duration_ms": requested_duration_ms,
                "probed_endpoints_ms": endpoints,
                "alignment_tolerance_ms": tolerance_ms,
            },
        )


def _render_wall_seconds(duration_ms: int) -> int:
    estimate = 120 + math.ceil(duration_ms / 1000) * 4
    return max(_MIN_RENDER_WALL_SECONDS, min(_MAX_RENDER_WALL_SECONDS, estimate))


async def stage_media_splice(
    source_path: str | Path,
    output_path: str | Path,
    *,
    media_kind: MediaKind,
    start_ms: int,
    end_ms: int,
    source_mime: str | None = None,
    source_filename: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> StagedMediaSplice:
    """Render and validate one range without publishing it to a project."""

    start = _strict_nonnegative_ms("start_ms", start_ms)
    end = _strict_nonnegative_ms("end_ms", end_ms)
    if end <= start:
        raise MediaSpliceError("invalid_range", "end_ms must be greater than start_ms")
    if media_kind not in {"audio", "video"}:
        raise MediaSpliceError(
            "invalid_media_kind", "media_kind must be audio or video"
        )

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if not source.is_file():
        raise MediaSpliceError("source_missing", "source media path does not exist")
    if source == output:
        raise MediaSpliceError(
            "invalid_output_path", "output path must differ from source"
        )
    if output.exists():
        raise MediaSpliceError("output_exists", "staged output path already exists")
    if not output.parent.is_dir():
        raise MediaSpliceError(
            "invalid_output_path", "staged output directory is missing"
        )
    expected_suffix = _VIDEO_SUFFIX if media_kind == "video" else _AUDIO_SUFFIX
    if output.suffix.lower() != expected_suffix:
        raise MediaSpliceError(
            "invalid_output_path", f"{media_kind} output must use {expected_suffix}"
        )

    requested_duration_ms = end - start
    tolerance_ms = (
        _VIDEO_ALIGNMENT_TOLERANCE_MS
        if media_kind == "video"
        else _AUDIO_ALIGNMENT_TOLERANCE_MS
    )
    wall_seconds = _render_wall_seconds(requested_duration_ms)
    try:
        with source.open("rb") as source_stream:
            result = await run_sandboxed(
                ffmpeg_splice_argv(
                    source,
                    output,
                    media_kind=media_kind,
                    start_ms=start,
                    end_ms=end,
                ),
                policy=SandboxPolicy(
                    cpu_seconds=wall_seconds,
                    memory_mb=2048,
                    wall_seconds=wall_seconds,
                    allow_network=False,
                    env_passthrough=["PATH"],
                ),
                stdin_file=source_stream,
                scratch_dir=output.parent,
                should_cancel=should_cancel,
            )
        if result.cancelled:
            raise MediaSpliceError("cancelled", "media render was cancelled")
        if not result.ok or not output.is_file() or output.stat().st_size <= 0:
            raise MediaSpliceError(
                "ffmpeg_failed",
                "ffmpeg could not render the requested range",
                details={"stderr": result.stderr[:500], "timed_out": result.timed_out},
            )

        probe = await probe_staged_media(output, should_cancel=should_cancel)
        _validate_probe(
            probe,
            media_kind=media_kind,
            requested_duration_ms=requested_duration_ms,
            tolerance_ms=tolerance_ms,
        )
        alignment_error_ms = probe.duration_ms - requested_duration_ms
        return StagedMediaSplice(
            path=output,
            filename=output.name,
            mime=VIDEO_OUTPUT_MIME if media_kind == "video" else AUDIO_OUTPUT_MIME,
            media_kind=media_kind,
            requested_start_ms=start,
            requested_end_ms=end,
            resolved_source_start_ms=start,
            resolved_source_end_ms=end,
            duration_ms=requested_duration_ms,
            alignment_error_ms=alignment_error_ms,
            alignment_tolerance_ms=tolerance_ms,
            precision="frame_accurate" if media_kind == "video" else "sample_accurate",
            renderer_profile=RENDERER_PROFILE_VERSION,
            renderer_params={
                "source_mime": source_mime,
                "source_filename": source_filename,
                "video_codec": "libx264" if media_kind == "video" else None,
                "audio_codec": "aac" if media_kind == "video" else "flac",
            },
            probe=probe,
        )
    except BaseException:
        output.unlink(missing_ok=True)
        raise
