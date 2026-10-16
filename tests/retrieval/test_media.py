from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from frisket.engine.executor.action_inventory import BoundLocalFile
from frisket.retrieval.encoding import (
    AudioEncodingInput,
    ImageEncodingInput,
    VideoClipEncodingInput,
)
from frisket.retrieval.media import (
    AudioWindow,
    AudioWindowRecipe,
    FixedClipRecipe,
    LocatedFrame,
    LocatedVideoClip,
    PeriodicFrameRecipe,
    ShotClipRecipe,
    produce_media,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real ffmpeg/ffprobe not installed",
)


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
    )


def _cfr_fixture(path: Path) -> None:
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=48x32:r=4:d=1",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=48x32:r=4:d=1",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "ffv1",
        "-y",
        str(path),
    )


def _vfr_fixture(path: Path) -> None:
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=48x32:r=10:d=0.4",
        "-vf",
        "setpts='N*N*0.1/TB'",
        "-frames:v",
        "4",
        "-fps_mode",
        "vfr",
        "-c:v",
        "ffv1",
        "-y",
        str(path),
    )


def _offset_audio_fixture(path: Path) -> None:
    _run_ffmpeg(
        "-itsoffset",
        "0.375",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000:duration=1",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(path),
    )


def _bound(path: Path) -> tuple[BoundLocalFile, object]:
    stream = path.open("rb")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return BoundLocalFile(stream=stream, sha256=f"sha256:{digest}"), stream


def _produce(path: Path, recipe, *, cancelled=None):
    source, stream = _bound(path)
    try:
        return asyncio.run(produce_media(source, recipe, cancelled=cancelled))
    finally:
        stream.close()


def test_periodic_frames_use_actual_vfr_pts_and_disclose_point_locations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "vfr.mkv"
    _vfr_fixture(source)

    produced = _produce(
        source,
        PeriodicFrameRecipe(
            interval_seconds=0.05,
            adjacent_dedup_threshold=None,
            max_frames=20,
        ),
    )

    assert produced.coverage.complete
    frames = produced.items
    assert len(frames) == 4
    assert all(isinstance(item, LocatedFrame) for item in frames)
    assert all(isinstance(item.input, ImageEncodingInput) for item in frames)
    points = [item.point.seconds for item in frames]
    assert points == pytest.approx([0.0, 0.1, 0.4, 0.9], abs=0.03)
    assert all(item.point.pts is not None for item in frames)
    assert all(item.point.time_base_den > 0 for item in frames)


def test_periodic_adjacent_dedup_suppresses_repeated_frames(tmp_path: Path) -> None:
    source = tmp_path / "repeat.mkv"
    _cfr_fixture(source)

    without_dedup = _produce(
        source,
        PeriodicFrameRecipe(
            interval_seconds=0.25,
            adjacent_dedup_threshold=None,
            max_frames=20,
        ),
    )
    with_dedup = _produce(
        source,
        PeriodicFrameRecipe(
            interval_seconds=0.25,
            adjacent_dedup_threshold=0.01,
            max_frames=20,
        ),
    )

    assert len(without_dedup.items) == 8
    assert len(with_dedup.items) == 2
    assert with_dedup.coverage.complete


def test_fixed_clips_overlap_cover_boundary_and_anchor_tail(tmp_path: Path) -> None:
    source = tmp_path / "fixed.mkv"
    _cfr_fixture(source)

    produced = _produce(
        source,
        FixedClipRecipe(
            duration_seconds=0.75,
            stride_seconds=0.5,
            sample_fps=4,
            max_clips=20,
        ),
    )

    assert produced.coverage.complete
    clips = produced.items
    assert all(isinstance(item, LocatedVideoClip) for item in clips)
    assert all(isinstance(item.input, VideoClipEncodingInput) for item in clips)
    assert clips[-1].end_seconds == pytest.approx(produced.coverage.source_end_seconds)
    assert clips[-1].start_seconds == pytest.approx(1.25)
    assert any(
        left.end_seconds > right.start_seconds for left, right in zip(clips, clips[1:])
    )
    for clip in clips:
        assert 1 <= len(clip.frames) <= 32
        assert tuple(frame.input for frame in clip.frames) == clip.input.frames
        assert all(
            clip.start_seconds <= frame.point.seconds < clip.end_seconds
            for frame in clip.frames
        )


def test_fixed_clip_cap_is_reported_as_partial_coverage(tmp_path: Path) -> None:
    source = tmp_path / "capped.mkv"
    _cfr_fixture(source)

    produced = _produce(
        source,
        FixedClipRecipe(
            duration_seconds=0.5,
            stride_seconds=0.25,
            sample_fps=2,
            max_clips=2,
        ),
    )

    assert len(produced.items) == 2
    assert not produced.coverage.complete
    assert produced.coverage.truncation_reason == "max_clips"
    assert produced.coverage.indexed_spans == tuple(
        (item.start_seconds, item.end_seconds) for item in produced.items
    )


def test_shot_clips_split_long_shots_and_cover_the_tail(tmp_path: Path) -> None:
    source = tmp_path / "shots.mkv"
    _cfr_fixture(source)

    produced = _produce(
        source,
        ShotClipRecipe(
            max_duration_seconds=0.6,
            sample_fps=2,
            max_clips=20,
            detector_threshold=10.0,
            min_scene_len_frames=1,
        ),
    )

    assert produced.coverage.complete
    assert produced.items[-1].end_seconds == pytest.approx(
        produced.coverage.source_end_seconds
    )
    assert all(
        item.end_seconds - item.start_seconds <= 0.6 + 1e-6 for item in produced.items
    )


def test_audio_windows_are_pcm16_mono_16khz_and_keep_track_offset(
    tmp_path: Path,
) -> None:
    source = tmp_path / "offset.mkv"
    _offset_audio_fixture(source)

    produced = _produce(
        source,
        AudioWindowRecipe(
            duration_seconds=0.4,
            stride_seconds=0.2,
            max_windows=20,
        ),
    )

    windows = produced.items
    assert produced.coverage.complete
    assert all(isinstance(item, AudioWindow) for item in windows)
    assert all(isinstance(item.input, AudioEncodingInput) for item in windows)
    assert windows[0].start_seconds >= 0.35
    assert windows[-1].end_seconds == pytest.approx(
        produced.coverage.source_end_seconds, abs=1 / 16_000
    )
    for window in windows:
        expected_samples = round((window.end_seconds - window.start_seconds) * 16_000)
        assert len(window.input.pcm16le_bytes) == pytest.approx(
            expected_samples * 2, abs=2
        )
        assert window.input.sample_rate == 16_000


def test_preobserved_cancellation_starts_no_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cancel.mkv"
    _cfr_fixture(source)
    source_ref, stream = _bound(source)
    called = False

    async def unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("cancelled production started a subprocess")

    monkeypatch.setattr("frisket.retrieval.media.run_sandboxed", unexpected)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                produce_media(
                    source_ref,
                    PeriodicFrameRecipe(),
                    cancelled=lambda: True,
                )
            )
    finally:
        stream.close()
    assert not called


def test_recipe_rejects_a_clip_that_exceeds_native_frame_budget() -> None:
    with pytest.raises(ValueError, match="32"):
        FixedClipRecipe(duration_seconds=20, stride_seconds=10, sample_fps=2)


def test_fixture_probe_confirms_audio_offset_is_real(tmp_path: Path) -> None:
    """Keep the offset assertion above grounded in the actual fixture container."""

    source = tmp_path / "offset-proof.mkv"
    _offset_audio_fixture(source)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=start_time",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(json.loads(result.stdout)["streams"][0]["start_time"]) >= 0.35
