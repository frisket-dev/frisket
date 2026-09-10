from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import media_splice
from frisket.engine.store.media_splice import (
    AUDIO_OUTPUT_MIME,
    RENDERER_PROFILE_VERSION,
    MediaSpliceError,
    ffmpeg_splice_argv,
    ffprobe_splice_argv,
    parse_splice_probe,
    probe_staged_media,
    stage_media_splice,
)


def _wav_bytes(seconds: float, *, sample_rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


def _probe_body(
    *,
    media_kind: str,
    duration: str = "1.000",
    start: str = "0.000000",
    format_name: str | None = None,
    codec_name: str | None = None,
    extra_streams: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    is_video = media_kind == "video"
    return {
        "format": {
            "duration": duration,
            "format_name": format_name or ("mov,mp4" if is_video else "flac"),
        },
        "streams": [
            {
                "index": 0,
                "codec_type": media_kind,
                "codec_name": codec_name or ("h264" if is_video else "flac"),
                "start_time": start,
                "duration": duration,
            },
            *(extra_streams or []),
        ],
    }


def _write_av_video(path: Path, *, with_audio: bool = True) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x90:r=10:d=2",
    ]
    if with_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        )
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if with_audio:
        command.extend(["-c:a", "aac", "-shortest"])
    else:
        command.append("-an")
    subprocess.run(
        [*command, str(path)],
        check=True,
        capture_output=True,
        timeout=120,
    )


def test_video_command_is_one_pinned_decode_reencode(tmp_path: Path) -> None:
    source = tmp_path / "source.mov"
    output = tmp_path / "clip.mp4"
    argv = ffmpeg_splice_argv(
        source, output, media_kind="video", start_ms=1250, end_ms=3750
    )

    assert argv[:6] == [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
    ]
    assert str(source) not in argv
    assert argv[argv.index("-protocol_whitelist") + 1] == "fd"
    assert argv[argv.index("-i") + 1] == "fd:"
    assert "-format_whitelist" not in argv
    assert argv[argv.index("-ss") + 1] == "1.250"
    assert argv[argv.index("-t") + 1] == "2.500"
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert "copy" not in argv
    assert argv[-1] == str(output)


def test_long_range_uses_bounded_preroll_and_audio_is_flac(tmp_path: Path) -> None:
    video = ffmpeg_splice_argv(
        tmp_path / "source.mp4",
        tmp_path / "clip.mp4",
        media_kind="video",
        start_ms=12_500,
        end_ms=13_500,
    )
    seeks = [index for index, value in enumerate(video) if value == "-ss"]
    assert [video[index + 1] for index in seeks] == ["2.500", "10.000"]
    assert seeks[0] < video.index("-i") < seeks[1]

    audio = ffmpeg_splice_argv(
        tmp_path / "source.mp3",
        tmp_path / "clip.flac",
        media_kind="audio",
        start_ms=0,
        end_ms=1000,
    )
    assert audio[audio.index("-c:a") + 1] == "flac"
    assert "-vn" in audio
    assert "copy" not in audio


def test_probe_command_has_only_the_open_descriptor_capability(tmp_path: Path) -> None:
    source = tmp_path / "attacker-controlled.ffconcat"
    argv = ffprobe_splice_argv(source)
    assert str(source) not in argv
    assert argv[argv.index("-protocol_whitelist") + 1] == "fd"
    assert "-format_whitelist" not in argv
    assert argv[-1] == "fd:"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
async def test_nested_playlist_url_cannot_escape_fd_protocol(tmp_path: Path) -> None:
    playlist = tmp_path / "source.ffconcat"
    playlist.write_text(
        "ffconcat version 1.0\nfile http://127.0.0.1:9/not-requested.wav\n"
    )

    with pytest.raises(MediaSpliceError) as exc_info:
        await probe_staged_media(playlist, scratch_dir=tmp_path)

    assert exc_info.value.code == "probe_failed"
    stderr = str(exc_info.value.details.get("stderr", "")).lower()
    assert "not-requested" in stderr
    assert "not permitted" in stderr or "whitelist" in stderr


def test_probe_parser_keeps_the_facts_used_by_receipts(tmp_path: Path) -> None:
    probe = parse_splice_probe(
        {
            "format": {
                "duration": "1.234",
                "format_name": "mov,mp4",
                "size": "321",
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "start_time": "0.000000",
                    "duration": "1.234",
                    "avg_frame_rate": "30000/1001",
                }
            ],
        },
        path=tmp_path / "missing.mp4",
    )
    assert probe.duration_ms == 1234
    assert probe.size_bytes == 321
    assert probe.streams[0].codec_name == "h264"
    assert probe.streams[0].avg_frame_rate == "30000/1001"


@pytest.mark.asyncio
async def test_probe_uses_open_source_and_caller_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source-store"
    source_dir.mkdir()
    source = source_dir / "source.mp4"
    source.write_bytes(b"canonical-source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    async def fake_run(argv, **kwargs):
        assert kwargs["stdin_file"].read() == b"canonical-source"
        assert kwargs["scratch_dir"] == scratch
        assert str(source) not in argv
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(_probe_body(media_kind="video")),
            stderr="",
        )

    monkeypatch.setattr(media_splice, "run_sandboxed", fake_run)
    probe = await probe_staged_media(source, scratch_dir=scratch)
    assert probe.duration_ms == 1000
    assert list(source_dir.iterdir()) == [source]


@pytest.mark.asyncio
async def test_stager_renders_once_then_probes_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    output = tmp_path / "clip.flac"
    source.write_bytes(_wav_bytes(2))
    calls: list[str] = []

    async def fake_run(argv, **kwargs):
        calls.append(argv[0])
        assert kwargs["policy"].allow_network is False
        assert kwargs["should_cancel"] is None
        if argv[0] == "ffmpeg":
            assert kwargs["stdin_file"].read() == source.read_bytes()
            output.write_bytes(b"staged-flac")
            return SandboxResult(returncode=0, stdout="", stderr="")
        assert kwargs["stdin_file"].read() == b"staged-flac"
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(_probe_body(media_kind="audio")),
            stderr="",
        )

    monkeypatch.setattr(media_splice, "run_sandboxed", fake_run)
    staged = await stage_media_splice(
        source,
        output,
        media_kind="audio",
        start_ms=250,
        end_ms=1250,
        source_mime="audio/wav",
        source_filename="source.wav",
    )

    assert calls == ["ffmpeg", "ffprobe"]
    assert staged.path == output
    assert staged.mime == AUDIO_OUTPUT_MIME
    assert staged.duration_ms == 1000
    assert staged.resolved_source_start_ms == 250
    assert staged.resolved_source_end_ms == 1250
    assert staged.precision == "sample_accurate"
    assert staged.renderer_profile == RENDERER_PROFILE_VERSION
    assert "path" not in staged.receipt_metadata()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "clip.flac",
        "source.wav",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (SandboxResult(returncode=1, stdout="", stderr="bad input"), "ffmpeg_failed"),
        (
            SandboxResult(
                returncode=124,
                stdout="",
                stderr="wall timeout",
                timed_out=True,
            ),
            "ffmpeg_failed",
        ),
        (
            SandboxResult(
                returncode=125, stdout="", stderr="cancelled", cancelled=True
            ),
            "cancelled",
        ),
    ],
)
async def test_render_failure_is_an_ordinary_error_and_removes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: SandboxResult,
    expected_code: str,
) -> None:
    source = tmp_path / "source.wav"
    output = tmp_path / "clip.flac"
    source.write_bytes(_wav_bytes(1))

    async def fake_run(argv, **kwargs):
        del argv, kwargs
        output.write_bytes(b"partial")
        return result

    monkeypatch.setattr(media_splice, "run_sandboxed", fake_run)
    with pytest.raises(MediaSpliceError) as exc_info:
        await stage_media_splice(
            source, output, media_kind="audio", start_ms=0, end_ms=1000
        )
    assert exc_info.value.code == expected_code
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_kind", "body", "expected_code"),
    [
        (
            "audio",
            _probe_body(media_kind="audio", format_name="ogg"),
            "probe_failed",
        ),
        (
            "audio",
            _probe_body(media_kind="audio", codec_name="vorbis"),
            "probe_failed",
        ),
        (
            "video",
            _probe_body(media_kind="video", format_name="matroska"),
            "probe_failed",
        ),
        (
            "video",
            _probe_body(media_kind="video", codec_name="hevc"),
            "probe_failed",
        ),
        (
            "video",
            _probe_body(
                media_kind="video",
                extra_streams=[
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "mp3",
                        "start_time": "0",
                        "duration": "1",
                    }
                ],
            ),
            "probe_failed",
        ),
        (
            "video",
            _probe_body(media_kind="video", duration="0.500"),
            "cut_alignment_failed",
        ),
        (
            "video",
            {
                "format": {"duration": "1.000", "format_name": "mov,mp4"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "start_time": "0.500",
                        "duration": "0.500",
                    }
                ],
            },
            "cut_alignment_failed",
        ),
    ],
)
async def test_probe_rejects_wrong_profile_duration_or_primary_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_kind: str,
    body: dict[str, object],
    expected_code: str,
) -> None:
    suffix = ".mp4" if media_kind == "video" else ".flac"
    source = tmp_path / "source"
    output = tmp_path / f"clip{suffix}"
    source.write_bytes(b"source")

    async def fake_run(argv, **kwargs):
        del kwargs
        if argv[0] == "ffmpeg":
            output.write_bytes(b"rendered")
            return SandboxResult(returncode=0, stdout="", stderr="")
        return SandboxResult(returncode=0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(media_splice, "run_sandboxed", fake_run)
    with pytest.raises(MediaSpliceError) as exc_info:
        await stage_media_splice(
            source,
            output,
            media_kind=media_kind,  # type: ignore[arg-type]
            start_ms=0,
            end_ms=1000,
        )
    assert exc_info.value.code == expected_code
    assert not output.exists()


@pytest.mark.asyncio
async def test_video_allows_optional_aac_with_its_own_start_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    body = _probe_body(
        media_kind="video",
        extra_streams=[
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "start_time": "0.128",
                "duration": "0.872",
            }
        ],
    )

    async def fake_run(argv, **kwargs):
        del kwargs
        if argv[0] == "ffmpeg":
            output.write_bytes(b"rendered")
            return SandboxResult(returncode=0, stdout="", stderr="")
        return SandboxResult(returncode=0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(media_splice, "run_sandboxed", fake_run)
    staged = await stage_media_splice(
        source, output, media_kind="video", start_ms=0, end_ms=1000
    )
    assert staged.probe.streams[1].start_time == "0.128"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
@pytest.mark.parametrize("media_kind", ["audio", "video"])
def test_real_splice_is_positive_and_has_expected_profile(
    tmp_path: Path, media_kind: str
) -> None:
    if media_kind == "audio":
        source = tmp_path / "source.wav"
        output = tmp_path / "clip.flac"
        source.write_bytes(_wav_bytes(2))
    else:
        source = tmp_path / "source.mp4"
        output = tmp_path / "clip.mp4"
        _write_av_video(source)

    staged = asyncio.run(
        stage_media_splice(
            source,
            output,
            media_kind=media_kind,  # type: ignore[arg-type]
            start_ms=250,
            end_ms=1250,
        )
    )

    assert output.stat().st_size > 0
    assert abs(staged.probe.duration_ms - 1000) <= staged.alignment_tolerance_ms
    expected = [("audio", "flac")]
    if media_kind == "video":
        expected = [("video", "h264"), ("audio", "aac")]
    assert [
        (stream.codec_type, stream.codec_name) for stream in staged.probe.streams
    ] == expected


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_real_video_without_audio_is_supported(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "clip.mp4"
    _write_av_video(source, with_audio=False)

    staged = asyncio.run(
        stage_media_splice(source, output, media_kind="video", start_ms=0, end_ms=1000)
    )
    assert [
        (stream.codec_type, stream.codec_name) for stream in staged.probe.streams
    ] == [("video", "h264")]
