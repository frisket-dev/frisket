"""Admitted local media extraction; image publication belongs to the row host."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from frisket.actions.row_media_types import Face, Frame, FrameCount, FrameSampling
from frisket.actions.types import ColumnRef, RowError
from frisket.engine.executor.blob_outputs import RowBlobOutput, RowBlobPlan
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.runtime.launch import worker_argv
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.provider import enforce_media_duration_limit
from frisket.sdk.media import media_text_hash


_YUNET_MODEL_PATH = (
    Path(__file__).resolve().parents[2]
    / "data/face_detection/face_detection_yunet_2023mar.onnx"
)
_YUNET_MODEL = "face_detection_yunet_2023mar"
_YUNET_MODEL_REVISION = "opencv_zoo@47534e27c9851bb1128ccc0102f1145e27f23f98"
_YUNET_MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def _ffmpeg_binary(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(
            f"video frame extraction needs {name} on PATH — brew/apt install ffmpeg"
        )
    return resolved


def _video_frame_scale_args(max_dimension: int | None) -> list[str]:
    if max_dimension is None:
        return []
    if type(max_dimension) is not int or not 16 <= max_dimension <= 4096:
        raise ValueError("max_dimension must be an integer from 16 to 4096")
    return [
        "-vf",
        f"scale=w='min(iw,{max_dimension})':h='min(ih,{max_dimension})':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2",
    ]


class _AdmittedMediaExtractor:
    def __init__(self, project, stager, *, cancelled=None, execution_limits=None):
        self._project = project
        self._stager = stager
        self._cancelled = cancelled
        self._limits = execution_limits
        self._closed = False
        self._tasks = set()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("media extractor is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, sheet_id, row_id, sources):
        self._check_open()
        if (
            type(sheet_id) is not int
            or sheet_id <= 0
            or type(row_id) is not int
            or row_id <= 0
        ):
            raise RowError(
                "invalid_input_ref", "Media extraction requires its admitted row."
            )
        return self._binding(
            self,
            row,
            sheet_id,
            row_id,
            copy.deepcopy(dict(sources or {})),
            self._stager.bind_row(row_id),
        )

    async def _run(self, operation):
        self._check_open()
        task = asyncio.create_task(operation())
        self._tasks.add(task)
        try:
            return await task
        finally:
            # run_sandboxed settles its owned process tree before propagating
            # cancellation, so borrowed input and output scratch stay alive.
            await _settle(task)
            self._tasks.discard(task)

    async def aclose(self):
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            await _settle(task)


class _BoundMediaExtractor:
    def __init__(self, owner, row, sheet_id, row_id, sources, stager):
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources
        self._stager = stager

    def _source(self, row, source, *, accepted_types):
        self._owner._check_open()
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref", "Media extractor requires its admitted source."
            )
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in row.values
            or canonical_json_hash(source.read(row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError(
                "stale_input", "Media source differs from its admitted cell."
            )
        project = self._owner._project
        column = project.db.execute(
            "SELECT sheet_id,type,hidden FROM columns WHERE id=?",
            (captured["column_id"],),
        ).fetchone()
        if (
            column is None
            or column["sheet_id"] != self._sheet_id
            or column["hidden"]
            or column["type"] not in accepted_types
        ):
            raise RowError(
                "invalid_input_ref",
                "Media source column is unavailable or incompatible.",
            )
        value = captured["value"]
        if value is None or value == "":
            return None
        if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
            raise RowError(
                "invalid_media_cell", "Media source must be a blob-backed cell."
            )
        blob = MediaBlobStore(project).blob_row(value["blob"])
        if blob is None:
            raise RowError("missing_blob", "Media source blob is missing.")
        return {
            "sheet_id": self._sheet_id,
            "row_id": self._row_id,
            "column_id": captured["column_id"],
            "source_column": source.name,
            "blob_hash": value["blob"],
            "value_hash": canonical_json_hash(value),
            "filename": str(value.get("filename") or blob["filename"] or "source.bin"),
            "mime": str(
                value.get("mime") or blob["mime"] or "application/octet-stream"
            ),
            "size": blob["size"],
            "source_url_hash": media_text_hash(str(blob["source_url"]))
            if blob["source_url"]
            else None,
        }

    async def _sandbox(self, argv, *, policy, stdin_data=None):
        self._owner._check_open()
        result = await run_sandboxed(
            argv,
            policy=policy,
            stdin_data=stdin_data,
            should_cancel=lambda: (
                self._owner._closed
                or bool(self._owner._cancelled and self._owner._cancelled())
            ),
        )
        if result.cancelled:
            raise asyncio.CancelledError
        self._owner._check_open()
        return result

    def _stage(self, path, *, kind, source, details):
        self._owner._check_open()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return self._stager.stage_output(
            RowBlobOutput(
                primary=RowBlobPlan(
                    role=kind,
                    content_digest=digest,
                    staged_path=path,
                    filename=path.name,
                    mime="image/jpeg",
                    source_url=None,
                ),
                facts={"kind": kind, "source": source, **details},
            ),
            image=True,
        )


class _BoundFrameExtractor(_BoundMediaExtractor):
    async def extract(self, row, source, *, sampling, max_dimension=None):
        ref = self._source(row, source, accepted_types=("video", "file"))
        sampling = TypeAdapter(FrameSampling).validate_python(
            sampling.model_dump(mode="python")
            if hasattr(sampling, "model_dump")
            else sampling
        )
        scale_args = _video_frame_scale_args(max_dimension)
        if ref is None:
            return []

        async def extract():
            project = self._owner._project
            if self._owner._limits is not None:
                probe = MediaBlobStore(project).probe_metadata(ref["blob_hash"])
                enforce_media_duration_limit(
                    self._owner._limits.max_media_seconds, probe.get("duration_seconds")
                )
            with (
                project.materialize_blob(ref["blob_hash"]) as source_path,
                tempfile.TemporaryDirectory(prefix="frisket-frames-") as directory,
            ):
                path = str(source_path)
                ffprobe, ffmpeg = _ffmpeg_binary("ffprobe"), _ffmpeg_binary("ffmpeg")
                probe = await self._sandbox(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        path,
                    ],
                    policy=SandboxPolicy(
                        wall_seconds=60,
                        allow_network=False,
                        env_passthrough=["PATH"],
                        confine=fence.Confinement(
                            op="media.video_frames (ffprobe)",
                            read=(path,),
                            exec_binary=ffprobe,
                        ),
                    ),
                )
                self._stager.record_observation(
                    {
                        "kind": "media_read",
                        "service": "video_frames",
                        "source": ref,
                        "engine": "ffmpeg",
                        "sampling": sampling.model_dump(mode="json"),
                        "max_dimension": max_dimension,
                    }
                )
                if not probe.ok:
                    raise RowError(
                        "video_frames_run_failed",
                        f"ffprobe failed: {probe.stderr[:200]}",
                    )
                duration = float(json.loads(probe.stdout)["format"]["duration"])
                if not math.isfinite(duration) or duration < 0:
                    raise RowError(
                        "video_frames_run_failed", "Video duration is invalid."
                    )
                if self._owner._limits is not None:
                    enforce_media_duration_limit(
                        self._owner._limits.max_media_seconds, duration
                    )
                if isinstance(sampling, FrameCount):
                    timestamps = [
                        duration * (i + 0.5) / sampling.count
                        for i in range(sampling.count)
                    ]
                else:
                    count = max(1, min(200, int(duration // sampling.seconds) + 1))
                    timestamps = [
                        min(duration, i * sampling.seconds) for i in range(count)
                    ]
                frames = []
                for index, timestamp in enumerate(timestamps):
                    out = Path(directory) / f"frame{index}.jpg"
                    result = await self._sandbox(
                        [
                            ffmpeg,
                            "-ss",
                            f"{timestamp:.2f}",
                            "-i",
                            path,
                            "-frames:v",
                            "1",
                            *scale_args,
                            "-q:v",
                            "4",
                            "-y",
                            str(out),
                        ],
                        policy=SandboxPolicy(
                            wall_seconds=120,
                            memory_mb=2048,
                            env_passthrough=["PATH"],
                            confine=fence.Confinement(
                                op="media.video_frames (ffmpeg)",
                                read=(path,),
                                write=(directory,),
                                exec_binary=ffmpeg,
                            ),
                        ),
                    )
                    if out.is_symlink():
                        raise RowError(
                            "video_frames_run_failed",
                            "Frame output escaped its owned scratch directory.",
                        )
                    if result.ok and out.exists():
                        stamp = round(timestamp, 2)
                        image = self._stage(
                            out,
                            kind="video_frame",
                            source=ref,
                            details={
                                "timestamp": stamp,
                                "index": index,
                                "engine": "ffmpeg",
                                "sampling": sampling.model_dump(mode="json"),
                                "max_dimension": max_dimension,
                            },
                        )
                        frames.append(Frame(t=stamp, image=image))
                return frames

        try:
            return await self._owner._run(extract)
        except BlobNotFoundError:
            raise RowError("missing_blob", "Video blob bytes are missing.") from None


class _BoundFaceExtractor(_BoundMediaExtractor):
    async def extract(self, row, source):
        ref = self._source(row, source, accepted_types=("image", "file"))
        if ref is None:
            return []

        async def extract():
            with (
                self._owner._project.materialize_blob(ref["blob_hash"]) as source_path,
                tempfile.TemporaryDirectory(prefix="frisket-faces-") as directory,
            ):
                path = str(source_path)
                result = await self._sandbox(
                    worker_argv("faces"),
                    policy=SandboxPolicy(
                        wall_seconds=120,
                        memory_mb=2048,
                        env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
                        confine=fence.Confinement(
                            op="media.extract_faces",
                            read=(path, str(_YUNET_MODEL_PATH)),
                            write=(directory,),
                        ),
                    ),
                    stdin_data=json.dumps(
                        {
                            "path": path,
                            "model_path": str(_YUNET_MODEL_PATH),
                            "out_dir": directory,
                        }
                    ).encode(),
                )
                self._stager.record_observation(
                    {
                        "kind": "media_read",
                        "service": "extract_faces",
                        "source": ref,
                        "engine": "opencv_yunet",
                        "model": _YUNET_MODEL,
                        "model_revision": _YUNET_MODEL_REVISION,
                        "model_sha256": _YUNET_MODEL_SHA256,
                    }
                )
                if not result.ok:
                    raise RowError(
                        "extract_faces_run_failed",
                        f"face detection failed: {result.stderr[:200]}",
                    )
                output = json.loads(result.stdout)
                if output.get("error"):
                    raise RowError("extract_faces_run_failed", str(output["error"]))
                faces = []
                for index, item in enumerate(output["faces"]):
                    crop = Path(item["crop"])
                    if (
                        crop.is_symlink()
                        or crop.parent.resolve() != Path(directory).resolve()
                        or not crop.is_file()
                    ):
                        raise RowError(
                            "extract_faces_run_failed",
                            "Face crop escaped its owned scratch directory.",
                        )
                    bbox = {key: item[key] for key in ("x", "y", "w", "h")}
                    image = self._stage(
                        crop,
                        kind="face_crop",
                        source=ref,
                        details={
                            "bbox": bbox,
                            "index": index,
                            "engine": "opencv_yunet",
                            "model": _YUNET_MODEL,
                            "model_revision": _YUNET_MODEL_REVISION,
                            "model_sha256": _YUNET_MODEL_SHA256,
                        },
                    )
                    faces.append(Face(**bbox, face=image))
                return faces

        try:
            return await self._owner._run(extract)
        except BlobNotFoundError:
            raise RowError("missing_blob", "Image blob bytes are missing.") from None


class AdmittedFrameExtractor(_AdmittedMediaExtractor):
    _binding = _BoundFrameExtractor


class AdmittedFaceExtractor(_AdmittedMediaExtractor):
    _binding = _BoundFaceExtractor
