from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from tests.engine.test_row_media_reader import jpeg_bytes


def _patch_ffmpeg_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sandboxed(cmd: list[str], *args: Any, **kwargs: Any):
        if Path(cmd[0]).name == "ffprobe":
            return SandboxResult(0, json.dumps({"format": {"duration": "12.0"}}), "")
        if Path(cmd[0]).name == "ffmpeg":
            Path(cmd[-1]).write_bytes(jpeg_bytes())
            return SandboxResult(0, "", "")
        raise AssertionError(f"unexpected sandbox command: {cmd}")

    monkeypatch.setattr(
        "frisket.engine.executor.row_media_read.run_sandboxed", fake_sandboxed
    )


def _video_frames_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    output_name: str = "frames",
    frame_count: int | None = 2,
    interval_seconds: float | None = None,
    idempotency_key: str = "media_video_frames@sha256:first",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    assert input_columns is None or len(input_columns) == 1
    assert capabilities is None
    params = {"source": input_columns[0] if input_columns else "video"}
    if interval_seconds is not None:
        assert frame_count is None
        params["sampling"] = {"kind": "interval", "seconds": interval_seconds}
    elif frame_count is not None:
        params["sampling"] = {"kind": "count", "count": frame_count}
    scope = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.video_frames",
        "scope": scope,
        "params": params,
        "output_names": {"frames": output_name},
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Videos")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "video": project.add_column(sheet_id, "video", type="video"),
    }
    blobs = [
        project.add_blob(
            f"fake video bytes {label}".encode("utf-8"),
            filename=f"{label}.mp4",
            mime="video/mp4",
            source_url=f"https://media.example/{label}.mp4",
        )
        for label in ("clip1", "clip2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": f"Clip {index + 1}",
                "video": media_cell(
                    blob,
                    mime="video/mp4",
                    filename=f"clip{index + 1}.mp4",
                ),
            }
            for index, blob in enumerate(blobs)
        ],
        columns,
    )
    return {"sheet_id": sheet_id, "row_ids": row_ids, "blobs": blobs}


def test_video_frame_scale_args_are_downscale_only_and_aspect_preserving() -> None:
    from frisket.engine.executor.row_media_read import _video_frame_scale_args

    assert _video_frame_scale_args(None) == []
    assert _video_frame_scale_args(None) == []

    args = _video_frame_scale_args(720)
    assert args[0] == "-vf"
    scale = args[1]
    assert "min(iw,720)" in scale
    assert "min(ih,720)" in scale
    # `-2` rounds to NEAREST even, which could bump an odd axis up past the
    # cap (101x101 @ cap 101 -> 102x101). decrease+force_divisible_by rounds
    # down: never upscales, stays even.
    assert "force_original_aspect_ratio=decrease" in scale
    assert "force_divisible_by=2" in scale
    assert "-2" not in scale

    # Actual capability arguments are validated too, not silently clamped.
    with pytest.raises(ValueError, match="max_dimension"):
        _video_frame_scale_args(5000)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real ffmpeg/ffprobe not installed",
)
@pytest.mark.parametrize(
    ("src_w", "src_h", "cap", "want_w", "want_h"),
    [
        # odd landscape above cap: longest side capped, both even, no upscale
        (405, 203, 200, 200, 100),
        # portrait above cap
        (203, 405, 200, 100, 200),
        # odd square exactly AT the cap — the old `-2` expression produced
        # 102x101 here (rounded UP past the cap); decrease+divisible rounds
        # down to 100x100
        (101, 101, 101, 100, 100),
        # already below the cap: never upscaled (even dims pass through)
        (120, 80, 720, 120, 80),
    ],
)
def test_scale_filter_real_ffmpeg_dimensions(
    tmp_path, src_w, src_h, cap, want_w, want_h
) -> None:
    """Executes the actual filter string against a real ffmpeg: downscale-only,
    aspect-preserving (within even-rounding), longest side never exceeds the
    cap. Guards the filter semantics the unit test can only assert textually."""
    from frisket.engine.executor.row_media_read import _video_frame_scale_args

    # mjpeg/avi tolerates ODD source dimensions (yuv420p mp4 would reject
    # 405x203 at encode time — the odd sizes are the whole point here)
    src = tmp_path / "src.avi"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={src_w}x{src_h}:d=0.2",
            "-c:v",
            "mjpeg",
            "-q:v",
            "4",
            "-y",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = tmp_path / "frame.jpg"
    scale_args = _video_frame_scale_args(cap)
    subprocess.run(
        [
            "ffmpeg",
            "-ss",
            "0",
            "-i",
            str(src),
            "-frames:v",
            "1",
            *scale_args,
            "-q:v",
            "4",
            "-y",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (want_w, want_h)
    assert max(stream["width"], stream["height"]) <= cap
