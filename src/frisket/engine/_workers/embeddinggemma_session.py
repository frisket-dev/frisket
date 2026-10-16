"""Parent-side resident session for the optional EmbeddingGemma 2 worker."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import tempfile
import weakref
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from frisket.engine._workers.embeddinggemma_model import (
    MODEL_REVISION,
    REQUIRED_SNAPSHOT_FILES,
)
from frisket.engine._workers.embeddinggemma_worker import SCHEMA_VERSION
from frisket.engine._workers.local_engine_lease import (
    LocalEngineLeaseBusy,
    acquire_local_engine_lease,
)
from frisket.engine._workers.session_base import (
    encode_frame,
    never_cancel,
    reject_constant,
)
from frisket.engine.sandbox import fence, shim
from frisket.retrieval.encoding import (
    AudioEncodingInput,
    EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID,
    EmbeddingGemma2Space,
    EmbeddingVector,
    EncodingError,
    EncodingInput,
    EncodingPurpose,
    ImageEncodingInput,
    TextEncodingInput,
    VideoClipEncodingInput,
)
from frisket.runtime.launch import PythonRuntime, worker_argv


MAX_STAGED_BATCH_BYTES = 512 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _snapshot_is_complete(snapshot: Path) -> bool:
    return (
        snapshot.name == MODEL_REVISION
        and snapshot.is_dir()
        and all((snapshot / relative).is_file() for relative in REQUIRED_SNAPSHOT_FILES)
    )


def _snapshot_read_paths(snapshot: Path) -> tuple[str, ...]:
    """Grant the snapshot names and every final blob reached through symlinks."""

    paths = [snapshot, *(snapshot / relative for relative in REQUIRED_SNAPSHOT_FILES)]
    resolved = (path.resolve(strict=True) for path in paths)
    return tuple(dict.fromkeys(str(path) for path in resolved))


class EmbeddingGemma2Session:
    """One cancellation-aware worker process shared across bounded batches."""

    def __init__(
        self,
        snapshot: Path,
        runtime: PythonRuntime,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._snapshot = Path(snapshot).absolute()
        self._runtime = runtime
        self._cancelled = cancelled or never_cancel
        self._lock = asyncio.Lock()
        self._handle = None
        self._lease = None
        self._closed = False
        self._staging = Path(tempfile.mkdtemp(prefix="frisket-embeddinggemma-"))
        self._staging_cleanup = weakref.finalize(
            self, shutil.rmtree, self._staging, True
        )

    @classmethod
    def from_installed(
        cls, *, cancelled: Callable[[], bool] | None = None
    ) -> EmbeddingGemma2Session:
        """Construct a production session from host-validated installed paths."""

        from frisket.engine._workers.embeddinggemma_artifacts import (
            cached_embeddinggemma_path,
            embeddinggemma_ready,
        )
        from frisket.runtime.embeddinggemma_install import runtime_python

        try:
            ready = embeddinggemma_ready()
            snapshot = cached_embeddinggemma_path() if ready else None
        except (OSError, RuntimeError):
            snapshot = None
        if snapshot is None:
            raise EncodingError(
                "model_unavailable",
                "Prepare the pinned EmbeddingGemma 2 model and runtime before semantic retrieval.",
            )
        runtime = PythonRuntime(runtime_python(), PythonRuntime.current().app_code_root)
        return cls(snapshot, runtime, cancelled=cancelled)

    async def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = encode_frame(request)
        if len(payload) > MAX_REQUEST_BYTES:
            raise EncodingError(
                "encoding_input_too_large",
                "This embedding batch is too large. Prepare a smaller batch.",
            )
        assert self._handle is not None
        raw = await self._handle.exchange_frame(
            payload,
            wall_seconds=600,
            response_limit=MAX_RESPONSE_BYTES,
            request_limit=MAX_REQUEST_BYTES,
        )
        response = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(response, dict):
            raise ValueError("invalid EmbeddingGemma response")
        if response.get("type") == "error":
            raise EncodingError(
                str(response.get("code", "encoding_failed")),
                str(
                    response.get(
                        "message", "The local embedding model stopped unexpectedly."
                    )
                ),
            )
        return response

    async def _start(self) -> None:
        if self._handle is not None:
            return
        if not _snapshot_is_complete(self._snapshot):
            raise EncodingError(
                "model_unavailable",
                "Prepare the pinned EmbeddingGemma 2 model before semantic retrieval.",
            )
        try:
            argv = worker_argv("embeddinggemma-session", runtime=self._runtime)
        except ValueError as exc:
            raise EncodingError(
                "model_unavailable",
                "Install the EmbeddingGemma 2 runtime before semantic retrieval.",
            ) from exc
        try:
            self._lease = acquire_local_engine_lease("embeddinggemma-2")
        except LocalEngineLeaseBusy as exc:
            raise EncodingError("engine_busy", str(exc)) from exc
        environment = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
        }
        # Snapshot entries may cross more than one content-addressed symlink.
        # Landlock checks the final inode, so grant each pinned file's resolved
        # target instead of assuming that every cache keeps blobs under the
        # model repository directory.
        snapshot_reads = _snapshot_read_paths(self._snapshot)
        self._handle = await shim.open_sandboxed_process(
            argv,
            policy=shim.SandboxPolicy(
                cpu_seconds=86400,
                memory_mb=8192,
                wall_seconds=600,
                trusted_python_netwall=True,
                allowed_extra_env=list(environment),
                confine=fence.Confinement(
                    op="local multimodal embedding",
                    read=(
                        *snapshot_reads,
                        str(self._staging),
                        "/proc/cpuinfo",
                        "/sys/devices/system/cpu",
                    ),
                ),
            ),
            extra_env=environment,
            should_cancel=self._cancelled,
        )
        ready = await self._exchange(
            {
                "schema_version": SCHEMA_VERSION,
                "type": "init",
                "model_revision": MODEL_REVISION,
                "snapshot": str(self._snapshot),
                "staging_root": str(self._staging),
            }
        )
        expected = {
            "schema_version": SCHEMA_VERSION,
            "type": "ready",
            "model_revision": MODEL_REVISION,
            "preprocessing_base_id": EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID,
        }
        if ready != expected:
            raise EncodingError(
                "model_unavailable",
                "The EmbeddingGemma 2 runtime has an incompatible preprocessing contract.",
            )

    def _write(self, directory: Path, name: str, value: bytes) -> str:
        if self._cancelled():
            raise asyncio.CancelledError
        path = directory / name
        path.write_bytes(value)
        return str(path)

    @staticmethod
    def _validate_staging_size(inputs: Sequence[EncodingInput]) -> None:
        total_bytes = sum(
            len(value.image_bytes)
            if isinstance(value, ImageEncodingInput)
            else len(value.pcm16le_bytes)
            if isinstance(value, AudioEncodingInput)
            else sum(len(frame.image_bytes) for frame in value.frames)
            if isinstance(value, VideoClipEncodingInput)
            else 0
            for value in inputs
        )
        if total_bytes > MAX_STAGED_BATCH_BYTES:
            raise EncodingError(
                "encoding_input_too_large",
                "This media embedding input exceeds the 512 MiB staging limit.",
            )

    def _stage(
        self, directory: Path, inputs: Sequence[EncodingInput]
    ) -> list[dict[str, Any]]:
        if not inputs or len(inputs) > 32:
            raise EncodingError(
                "invalid_input", "Encode batches must contain between 1 and 32 inputs."
            )
        if len({type(value) for value in inputs}) != 1:
            raise EncodingError(
                "invalid_input", "Each embedding batch must use one input modality."
            )
        if not isinstance(inputs[0], TextEncodingInput) and len(inputs) != 1:
            raise EncodingError(
                "invalid_input", "Media inputs are encoded one at a time."
            )
        self._validate_staging_size(inputs)
        staged: list[dict[str, Any]] = []
        for index, value in enumerate(inputs):
            if isinstance(value, TextEncodingInput):
                staged.append({"kind": "text", "text": value.text})
            elif isinstance(value, ImageEncodingInput):
                staged.append(
                    {
                        "kind": "image",
                        "path": self._write(
                            directory, f"image-{index}.encoded", value.image_bytes
                        ),
                    }
                )
            elif isinstance(value, AudioEncodingInput):
                staged.append(
                    {
                        "kind": "audio",
                        "path": self._write(
                            directory, f"audio-{index}.pcm", value.pcm16le_bytes
                        ),
                        "sample_rate": value.sample_rate,
                    }
                )
            elif isinstance(value, VideoClipEncodingInput):
                paths = []
                for frame_index, frame in enumerate(value.frames):
                    paths.append(
                        self._write(
                            directory,
                            f"video-{index}-{frame_index}.encoded",
                            frame.image_bytes,
                        )
                    )
                staged.append({"kind": "video", "frames": paths})
            else:
                raise TypeError("unsupported retrieval encoding input")
        return staged

    @staticmethod
    def _vectors(
        response: dict[str, Any], *, count: int, dimensions: int
    ) -> list[EmbeddingVector]:
        if (
            response.get("schema_version") != SCHEMA_VERSION
            or response.get("type") != "result"
            or response.get("model_revision") != MODEL_REVISION
        ):
            raise ValueError("invalid EmbeddingGemma result envelope")
        raw_vectors = response.get("vectors")
        if not isinstance(raw_vectors, list) or len(raw_vectors) != count:
            raise ValueError("invalid EmbeddingGemma vector count")
        result: list[EmbeddingVector] = []
        for raw in raw_vectors:
            if (
                not isinstance(raw, list)
                or len(raw) != dimensions
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in raw
                )
            ):
                raise ValueError("invalid EmbeddingGemma vector")
            vector = tuple(float(value) for value in raw)
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=2e-4, abs_tol=2e-4):
                raise ValueError("EmbeddingGemma vector is not normalized")
            result.append(vector)
        return result

    async def encode(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
    ) -> list[EmbeddingVector]:
        async with self._lock:
            if self._closed:
                raise EncodingError(
                    "encoding_failed", "The local embedding session is closed."
                )
            request_dir = Path(tempfile.mkdtemp(dir=self._staging, prefix="batch-"))
            try:
                if self._cancelled():
                    raise asyncio.CancelledError
                if (
                    inputs
                    and isinstance(inputs[0], VideoClipEncodingInput)
                    and len(inputs[0].frames) > space.max_video_frames
                ):
                    raise EncodingError(
                        "invalid_input",
                        "This native video clip exceeds the selected visual-token budget.",
                    )
                staged = self._stage(request_dir, inputs)
                await self._start()
                response = await self._exchange(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "type": "encode",
                        "purpose": purpose.value,
                        "dimensions": space.dimensions,
                        "image_soft_tokens": space.image_soft_tokens,
                        "video_soft_tokens_per_frame": (
                            space.video_soft_tokens_per_frame
                        ),
                        "items": staged,
                    }
                )
                return self._vectors(
                    response, count=len(inputs), dimensions=space.dimensions
                )
            except EncodingError as exc:
                if exc.code not in {"invalid_input", "encoding_input_too_large"}:
                    await self._close(abort=True)
                raise
            except (shim.SandboxProcessCancelledError, asyncio.CancelledError):
                await self._close(abort=True)
                raise asyncio.CancelledError from None
            except shim.SandboxTeardownError:
                if self._lease is not None:
                    self._lease.poison()
                self._closed = True
                self._staging_cleanup()
                raise
            except Exception as exc:
                await self._close(abort=True)
                raise EncodingError(
                    "encoding_failed",
                    "The local embedding model stopped unexpectedly. Retry or check available memory.",
                ) from exc
            finally:
                shutil.rmtree(request_dir, ignore_errors=True)

    async def _close(self, *, abort: bool) -> None:
        self._closed = True
        try:
            if self._handle is not None:
                if abort:
                    await self._handle.abort()
                else:
                    await self._handle.close(
                        encode_frame(
                            {"schema_version": SCHEMA_VERSION, "type": "close"}
                        ),
                        wall_seconds=10,
                    )
        except shim.SandboxTeardownError:
            if self._lease is not None:
                self._lease.poison()
            raise
        finally:
            if self._lease is not None:
                self._lease.release()
            self._staging_cleanup()

    async def aclose(self) -> None:
        async with self._lock:
            if not self._closed:
                await self._close(abort=False)


__all__ = ["EmbeddingGemma2Session", "MAX_STAGED_BATCH_BYTES"]
