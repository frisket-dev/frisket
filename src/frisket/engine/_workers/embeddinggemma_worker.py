"""Pinned offline EmbeddingGemma 2 worker and reusable runtime adapter.

This module is safe to import in the main application.  Torch, Transformers,
NumPy and Pillow are imported only when ``EmbeddingGemma2Runtime`` is created
inside the optional worker environment.
"""

from __future__ import annotations

import contextlib
import math
import sys
import warnings
from pathlib import Path
from typing import Any

from frisket.engine._workers.embeddinggemma_model import (
    MODEL_REVISION,
    REQUIRED_SNAPSHOT_FILES,
    TRANSFORMERS_VERSION,
)
from frisket.engine._workers.framing import FrameCodec
from frisket.retrieval.encoding import (
    EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE,
    EMBEDDINGGEMMA2_CONTEXT_TOKENS,
    EMBEDDINGGEMMA2_DOCUMENT_PREFIX,
    EMBEDDINGGEMMA2_IMAGE_SOFT_TOKENS,
    EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
    EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID,
    EMBEDDINGGEMMA2_QUERY_PREFIX,
    EMBEDDINGGEMMA2_SUPPORTED_DIMENSIONS,
    EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS,
    EMBEDDINGGEMMA2_VIDEO_SOFT_TOKENS_PER_FRAME,
)


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DECODED_IMAGE_PIXELS = 100_000_000
CODEC = FrameCodec(MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ValueError)


class WorkerProtocolError(RuntimeError):
    pass


def _exact_fields(frame: dict[str, Any], fields: set[str], *, kind: str) -> None:
    if set(frame) != fields:
        raise WorkerProtocolError(f"invalid {kind} fields")


def _snapshot_path(raw: Any) -> Path:
    if not isinstance(raw, str):
        raise WorkerProtocolError("snapshot path must be a string")
    path = Path(raw).absolute()
    if path.name != MODEL_REVISION or not path.is_dir():
        raise WorkerProtocolError("the pinned model snapshot is unavailable")
    for relative in REQUIRED_SNAPSHOT_FILES:
        if not (path / relative).is_file():
            raise WorkerProtocolError("the pinned model snapshot is incomplete")
    return path


def _staging_root(raw: Any) -> Path:
    if not isinstance(raw, str):
        raise WorkerProtocolError("staging root must be a string")
    path = Path(raw).resolve(strict=True)
    if not path.is_dir():
        raise WorkerProtocolError("staging root is unavailable")
    return path


def _staged_file(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str):
        raise WorkerProtocolError("staged input path must be a string")
    try:
        path = Path(raw).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkerProtocolError("staged input is outside the request root") from exc
    if not path.is_file():
        raise WorkerProtocolError("staged input is unavailable")
    return path


def _positive_choice(value: Any, choices: frozenset[int], *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in choices:
        raise WorkerProtocolError(f"invalid {name}")
    return value


class EmbeddingGemma2Runtime:
    """The production AutoProcessor/AutoModel implementation.

    Experiments use this class too, so the quality gate and worker cannot drift
    into separate pooling, prompt, video, or normalization algorithms.
    """

    def __init__(self, snapshot: Path) -> None:
        import numpy as np
        import torch
        import transformers
        from PIL import Image
        from transformers import AutoModel, AutoProcessor

        if transformers.__version__ != TRANSFORMERS_VERSION:
            raise RuntimeError(
                f"EmbeddingGemma 2 requires Transformers {TRANSFORMERS_VERSION}"
            )
        torch.set_num_threads(2)
        Image.MAX_IMAGE_PIXELS = MAX_DECODED_IMAGE_PIXELS
        self._np = np
        self._torch = torch
        self._image = Image
        self._processor = AutoProcessor.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
        )
        self._model = AutoModel.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
            dtype=torch.float32,
        ).eval()

    def _read_image(self, path: Path) -> Any:
        Image = self._image
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                source.load()
                return source.convert("RGB")

    def _features(
        self,
        items: list[dict[str, Any]],
        *,
        purpose: str,
        staging_root: Path,
        image_soft_tokens: int,
        video_soft_tokens_per_frame: int,
    ) -> Any:
        kind = items[0]["kind"]
        if any(item.get("kind") != kind for item in items):
            raise WorkerProtocolError("an encode batch must use one modality")
        if kind == "text":
            prefix = (
                EMBEDDINGGEMMA2_QUERY_PREFIX
                if purpose == "query"
                else EMBEDDINGGEMMA2_DOCUMENT_PREFIX
            )
            values: list[str] = []
            for item in items:
                _exact_fields(item, {"kind", "text"}, kind="text item")
                text = item["text"]
                if not isinstance(text, str) or not text.strip():
                    raise WorkerProtocolError("text input must not be empty")
                values.append(prefix + text)
            return self._processor(
                text=values,
                text_kwargs={
                    "padding": True,
                    "truncation": True,
                    "max_length": EMBEDDINGGEMMA2_CONTEXT_TOKENS,
                },
                return_tensors="pt",
            )
        if kind == "image":
            images = []
            for item in items:
                _exact_fields(item, {"kind", "path"}, kind="image item")
                images.append(
                    self._read_image(_staged_file(staging_root, item["path"]))
                )
            return self._processor(
                images=images,
                images_kwargs={"max_soft_tokens": image_soft_tokens},
                return_tensors="pt",
            )
        if kind == "audio":
            values = []
            for item in items:
                _exact_fields(
                    item,
                    {"kind", "path", "sample_rate"},
                    kind="audio item",
                )
                if item["sample_rate"] != EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE:
                    raise WorkerProtocolError("invalid audio sampling rate")
                samples = self._np.fromfile(
                    _staged_file(staging_root, item["path"]), dtype="<i2"
                )
                if samples.size == 0:
                    raise WorkerProtocolError("audio input must not be empty")
                values.append(samples.astype(self._np.float32) / 32768.0)
            return self._processor(
                audio=values,
                audio_kwargs={"sampling_rate": EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE},
                return_tensors="pt",
            )
        if kind == "video":
            videos = []
            for item in items:
                _exact_fields(item, {"kind", "frames"}, kind="video item")
                frames = item["frames"]
                if (
                    not isinstance(frames, list)
                    or not 1 <= len(frames) <= EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES
                ):
                    raise WorkerProtocolError("invalid native video frame count")
                decoded = [
                    self._np.asarray(
                        self._read_image(_staged_file(staging_root, frame))
                    )
                    for frame in frames
                ]
                videos.append(self._np.stack(decoded, axis=0))
            return self._processor(
                videos=videos,
                do_sample_frames=False,
                max_frames=EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES,
                overflow_strategy="truncate",
                add_timestamps=False,
                max_soft_tokens=video_soft_tokens_per_frame,
                return_tensors="pt",
            )
        raise WorkerProtocolError("unsupported encoding modality")

    def encode(
        self,
        items: list[dict[str, Any]],
        *,
        purpose: str,
        dimensions: int,
        staging_root: Path,
        image_soft_tokens: int = EMBEDDINGGEMMA2_IMAGE_SOFT_TOKENS,
        video_soft_tokens_per_frame: int = (
            EMBEDDINGGEMMA2_VIDEO_SOFT_TOKENS_PER_FRAME
        ),
    ) -> list[list[float]]:
        if not items or len(items) > 32:
            raise WorkerProtocolError("encode batch must contain 1 to 32 inputs")
        if purpose not in {"query", "document"}:
            raise WorkerProtocolError("invalid encoding purpose")
        dimensions = _positive_choice(
            dimensions, EMBEDDINGGEMMA2_SUPPORTED_DIMENSIONS, name="dimensions"
        )
        image_soft_tokens = _positive_choice(
            image_soft_tokens,
            EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS,
            name="image soft-token budget",
        )
        video_soft_tokens_per_frame = _positive_choice(
            video_soft_tokens_per_frame,
            EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS,
            name="video soft-token budget",
        )
        features = self._features(
            items,
            purpose=purpose,
            staging_root=staging_root,
            image_soft_tokens=image_soft_tokens,
            video_soft_tokens_per_frame=video_soft_tokens_per_frame,
        )
        torch = self._torch
        with torch.inference_mode():
            hidden = self._model(**features).last_hidden_state
            mask = features["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
            vectors = torch.nn.functional.normalize(
                pooled[:, :dimensions].float(), p=2, dim=-1
            )
        if tuple(vectors.shape) != (len(items), dimensions) or not bool(
            torch.isfinite(vectors).all()
        ):
            raise RuntimeError("model returned an invalid vector matrix")
        result = vectors.detach().cpu().tolist()
        if any(
            not math.isclose(
                math.sqrt(sum(float(value) ** 2 for value in vector)),
                1.0,
                rel_tol=2e-4,
                abs_tol=2e-4,
            )
            for vector in result
        ):
            raise RuntimeError("model returned unnormalized vectors")
        return result


def _init(frame: dict[str, Any]) -> tuple[Path, Path]:
    _exact_fields(
        frame,
        {"schema_version", "type", "model_revision", "snapshot", "staging_root"},
        kind="init",
    )
    if frame["schema_version"] != SCHEMA_VERSION or frame["type"] != "init":
        raise WorkerProtocolError("invalid init frame")
    if frame["model_revision"] != MODEL_REVISION:
        raise WorkerProtocolError("model revision does not match the worker pin")
    return _snapshot_path(frame["snapshot"]), _staging_root(frame["staging_root"])


def _encode_request(frame: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(
        frame,
        {
            "schema_version",
            "type",
            "purpose",
            "dimensions",
            "image_soft_tokens",
            "video_soft_tokens_per_frame",
            "items",
        },
        kind="encode",
    )
    if frame["schema_version"] != SCHEMA_VERSION or frame["type"] != "encode":
        raise WorkerProtocolError("invalid encode frame")
    if not isinstance(frame["items"], list):
        raise WorkerProtocolError("encode items must be a list")
    return frame


def framed_main() -> int:
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        try:
            first = CODEC.read_frame(source)
            if first is None:
                return 1
            snapshot, staging_root = _init(first)
            runtime = EmbeddingGemma2Runtime(snapshot)
        except Exception:
            CODEC.write_frame(
                sink,
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "error",
                    "code": "model_unavailable",
                    "message": (
                        "EmbeddingGemma 2 could not load. Prepare the pinned local "
                        "model and runtime, then retry."
                    ),
                },
            )
            return 1
        CODEC.write_frame(
            sink,
            {
                "schema_version": SCHEMA_VERSION,
                "type": "ready",
                "model_revision": MODEL_REVISION,
                "preprocessing_base_id": EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID,
            },
        )
        while (frame := CODEC.read_frame(source)) is not None:
            if frame == {"schema_version": SCHEMA_VERSION, "type": "close"}:
                return 0
            try:
                request = _encode_request(frame)
                vectors = runtime.encode(
                    request["items"],
                    purpose=request["purpose"],
                    dimensions=request["dimensions"],
                    staging_root=staging_root,
                    image_soft_tokens=request["image_soft_tokens"],
                    video_soft_tokens_per_frame=request["video_soft_tokens_per_frame"],
                )
            except WorkerProtocolError:
                CODEC.write_frame(
                    sink,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "type": "error",
                        "code": "invalid_input",
                        "message": "The local embedding input is invalid.",
                    },
                )
            except Exception:
                CODEC.write_frame(
                    sink,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "type": "error",
                        "code": "encoding_failed",
                        "message": (
                            "EmbeddingGemma 2 could not encode this batch. Retry or "
                            "check available memory."
                        ),
                    },
                )
                return 1
            else:
                CODEC.write_frame(
                    sink,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "type": "result",
                        "model_revision": MODEL_REVISION,
                        "vectors": vectors,
                    },
                )
    return 0


__all__ = [
    "EmbeddingGemma2Runtime",
    "MAX_DECODED_IMAGE_PIXELS",
    "SCHEMA_VERSION",
    "WorkerProtocolError",
    "framed_main",
]
