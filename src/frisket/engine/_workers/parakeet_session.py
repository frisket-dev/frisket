"""Parent-side lifecycle for one run-scoped local Parakeet process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from typing import Any, Callable

from frisket.engine.sandbox import shim as sandbox_shim
from frisket.engine.sandbox.shim import SandboxPolicy

from .parakeet_artifacts import (
    PARAKEET_MODEL,
    PARAKEET_MODEL_REVISION,
    PARAKEET_VAD_REVISION,
    ParakeetArtifactCancelled,
    ParakeetArtifactUnavailable,
    ParakeetArtifacts,
    resolve_parakeet_artifacts,
)
from .session_base import duration_ms as _duration_ms
from .session_base import encode_frame as _encode_frame
from .session_base import finite_number as _finite_number
from .session_base import never_cancel as _never_cancel
from .session_base import (  # noqa: F401  (re-exported for the session tests)
    parse_proc_status_memory as _parse_proc_status_memory,
)
from .session_base import read_linux_peak_memory as _read_linux_peak_memory
from .session_base import reject_constant as _reject_constant

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "frisket.run_scoped_worker.v1"
PARAKEET_SESSION_BOOTSTRAP = (
    "from frisket.engine._workers.parakeet_worker import framed_main\nframed_main()"
)

STARTUP_WALL_SECONDS = 300.0
REQUEST_WALL_SECONDS = 7_200.0
CLOSE_WALL_SECONDS = 5.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_CPU_SECONDS = 2_147_483_647
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ORT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_ONNX_THREAD_MODES = {"shared_global", "per_session_fallback"}
_ONNX_GLOBAL_INTRA_OP_CAP = 4
_ONNX_INTER_OP_THREADS = 1


class ParakeetSessionFailure(RuntimeError):
    pass


class ParakeetSessionCancelled(RuntimeError):
    pass


class ParakeetRowError(RuntimeError):
    pass


# RSS proof for the sandboxed Parakeet child; also the sandbox memory limit.
PARAKEET_MEMORY_MB = 4096


def cumulative_cpu_seconds(expected_rows: int) -> int:
    rows = max(1, int(expected_rows))
    return min(3_600 * rows, MAX_CPU_SECONDS)


def parakeet_inference_policy(expected_rows: int) -> SandboxPolicy:
    return SandboxPolicy(
        cpu_seconds=cumulative_cpu_seconds(expected_rows),
        wall_seconds=int(REQUEST_WALL_SECONDS),
        memory_mb=PARAKEET_MEMORY_MB,
        env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
        allowed_extra_env=["HF_HUB_CACHE", "HF_HUB_OFFLINE"],
        trusted_python_netwall=True,
    )


def parakeet_inference_env(artifacts: ParakeetArtifacts) -> dict[str, str]:
    return {
        "HF_HUB_CACHE": str(artifacts.cache_dir),
        "HF_HUB_OFFLINE": "1",
    }


def parakeet_init_frame(artifacts: ParakeetArtifacts, *, vad: bool) -> dict[str, Any]:
    artifact_frame: dict[str, Any] = {
        "model_path": str(artifacts.model_path),
        "model_revision": PARAKEET_MODEL_REVISION,
    }
    if vad:
        if artifacts.vad_path is None:
            raise ParakeetSessionFailure("the pinned VAD artifact is unavailable")
        artifact_frame.update(
            {
                "vad_path": str(artifacts.vad_path),
                "vad_revision": PARAKEET_VAD_REVISION,
            }
        )
    # The frame carries only what the child consumes: schema/revision pins,
    # artifact paths, and the VAD flag. The ONNX session shape (int8, CPU-only,
    # bounded shared pool or safe fallback) is the worker's own configuration,
    # not parent-negotiated wire data.
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "init",
        "artifacts": artifact_frame,
        "vad": vad,
    }


def _decode_frame(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ParakeetSessionFailure(
            "the Parakeet worker returned invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise ParakeetSessionFailure("the Parakeet worker returned a non-object frame")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ParakeetSessionFailure("the Parakeet worker returned the wrong schema")
    return value


def _require_exact_fields(
    value: dict[str, Any], allowed: set[str], *, frame: str
) -> None:
    if set(value) != allowed:
        raise ParakeetSessionFailure(
            f"the Parakeet worker returned an invalid {frame} frame"
        )


def _validate_protocol_error(value: Any) -> None:
    if not isinstance(value, dict):
        raise ParakeetSessionFailure("the Parakeet worker returned an invalid error")
    _require_exact_fields(value, {"code", "message"}, frame="error")
    code = value.get("code")
    message = value.get("message")
    if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
        raise ParakeetSessionFailure("the Parakeet worker returned an invalid error")
    if not isinstance(message, str) or len(message) > 500:
        raise ParakeetSessionFailure("the Parakeet worker returned an invalid error")


def _validate_ready(frame: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(
        frame,
        {"schema_version", "type", "engine", "model", "onnx_threads"},
        frame="ready",
    )
    if (
        frame.get("type") != "ready"
        or frame.get("engine") != "parakeet"
        or frame.get("model") != PARAKEET_MODEL
    ):
        raise ParakeetSessionFailure("the Parakeet worker did not become ready")
    threads = frame.get("onnx_threads")
    if not isinstance(threads, dict):
        raise ParakeetSessionFailure(
            "the Parakeet worker returned invalid ONNX thread configuration"
        )
    _require_exact_fields(
        threads,
        {"mode", "intra", "inter", "cpu_count", "ort_version"},
        frame="ONNX thread configuration",
    )
    mode = threads.get("mode")
    intra = threads.get("intra")
    inter = threads.get("inter")
    cpu_count = threads.get("cpu_count")
    ort_version = threads.get("ort_version")
    valid_integers = all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (intra, inter, cpu_count)
    )
    if (
        not isinstance(mode, str)
        or mode not in _ONNX_THREAD_MODES
        or not valid_integers
        or not 1 <= cpu_count <= 1_000_000
        or inter != _ONNX_INTER_OP_THREADS
        or not isinstance(ort_version, str)
        or not _ORT_VERSION_RE.fullmatch(ort_version)
    ):
        raise ParakeetSessionFailure(
            "the Parakeet worker returned invalid ONNX thread configuration"
        )
    expected_intra = (
        min(max(1, cpu_count - 1), _ONNX_GLOBAL_INTRA_OP_CAP)
        if mode == "shared_global"
        else 1
    )
    if intra != expected_intra:
        raise ParakeetSessionFailure(
            "the Parakeet worker returned invalid ONNX thread configuration"
        )
    return {
        "mode": mode,
        "intra": intra,
        "inter": inter,
        "cpu_count": cpu_count,
        "ort_version": ort_version,
    }


def _raise_validated_fatal(frame: dict[str, Any]) -> None:
    _require_exact_fields(frame, {"schema_version", "type", "error"}, frame="fatal")
    if frame.get("type") != "fatal":
        raise ParakeetSessionFailure(
            "the Parakeet worker returned an invalid fatal frame"
        )
    _validate_protocol_error(frame.get("error"))
    if frame["error"]["code"] == "local_artifact_unavailable":
        raise ParakeetArtifactUnavailable(
            "the pinned Parakeet artifacts became unavailable before startup"
        )
    raise ParakeetSessionFailure("the Parakeet worker reported a fatal error")


def _validated_result(frame: dict[str, Any], request_id: str) -> dict[str, Any]:
    if frame.get("type") == "fatal":
        _raise_validated_fatal(frame)
    if frame.get("type") != "result" or frame.get("request_id") != request_id:
        raise ParakeetSessionFailure("the Parakeet worker returned a mismatched result")
    if frame.get("ok") is False:
        _require_exact_fields(
            frame,
            {"schema_version", "type", "request_id", "ok", "error"},
            frame="row error",
        )
        _validate_protocol_error(frame.get("error"))
        raise ParakeetRowError("audio could not be decoded or transcribed")
    _require_exact_fields(
        frame,
        {"schema_version", "type", "request_id", "ok", "data"},
        frame="result",
    )
    if frame.get("ok") is not True or not isinstance(frame.get("data"), dict):
        raise ParakeetSessionFailure("the Parakeet worker returned an invalid result")
    return validate_parakeet_result_data(frame["data"])


def validate_parakeet_result_data(data: Any) -> dict[str, Any]:
    """Validate the shared framed/one-shot engine result shape."""

    if not isinstance(data, dict):
        raise ParakeetSessionFailure("the Parakeet worker returned an invalid result")
    _require_exact_fields(data, {"text", "segments", "language"}, frame="result data")
    if not isinstance(data.get("text"), str) or not isinstance(
        data.get("segments"), list
    ):
        raise ParakeetSessionFailure("the Parakeet worker returned malformed data")
    language = data.get("language")
    if language is not None and not isinstance(language, str):
        raise ParakeetSessionFailure(
            "the Parakeet worker returned invalid language data"
        )
    for segment in data["segments"]:
        if not isinstance(segment, dict) or set(segment) != {"start", "end", "text"}:
            raise ParakeetSessionFailure(
                "the Parakeet worker returned malformed segments"
            )
        if (
            not _finite_number(segment.get("start"))
            or not _finite_number(segment.get("end"))
            or not isinstance(segment.get("text"), str)
        ):
            raise ParakeetSessionFailure(
                "the Parakeet worker returned malformed segments"
            )
    return data


class ParakeetProcessSession:
    """Lazy, serial, one-process session owned by one recipe invocation."""

    def __init__(
        self,
        *,
        expected_rows: int,
        vad: bool,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        self.expected_rows = max(1, int(expected_rows))
        self.vad = bool(vad)
        self._should_cancel = should_cancel
        self._handle: Any | None = None
        self._artifacts: ParakeetArtifacts | None = None
        self._closed = False
        self._request_count = 0
        self._in_flight = False
        self._onnx_threads: dict[str, Any] | None = None
        self._peak_memory: dict[str, int] = {}
        self.teardown_failed = False

    def mark_closed(self) -> None:
        self._closed = True

    def _sample_peak_memory(self, handle: Any) -> dict[str, int]:
        try:
            pid = handle.pid
        except (AttributeError, OSError, RuntimeError):
            pid = None
        for key, value in _read_linux_peak_memory(pid).items():
            self._peak_memory[key] = max(value, self._peak_memory.get(key, 0))
        return dict(self._peak_memory)

    def _worker_log_fields(self, handle: Any) -> dict[str, Any]:
        fields: dict[str, Any] = self._sample_peak_memory(handle)
        if self._onnx_threads is not None:
            fields.update(
                {
                    "onnx_thread_mode": self._onnx_threads["mode"],
                    "onnx_intra_threads": self._onnx_threads["intra"],
                    "onnx_inter_threads": self._onnx_threads["inter"],
                    "onnx_cpu_count": self._onnx_threads["cpu_count"],
                    "onnxruntime_version": self._onnx_threads["ort_version"],
                }
            )
        return fields

    async def _abort_flagging(self, handle: Any) -> None:
        """Abort the child; flag an unprovable teardown before it propagates."""

        try:
            await handle.abort()
        except sandbox_shim.SandboxTeardownError:
            self.teardown_failed = True
            raise

    async def _abort(self) -> None:
        self._closed = True
        handle, self._handle = self._handle, None
        if handle is None:
            return
        await self._abort_flagging(handle)

    async def _ensure_started(self) -> None:
        if self._closed:
            raise ParakeetSessionFailure("the Parakeet session is already closed")
        if self._handle is not None:
            return
        try:
            if self._should_cancel is not None and self._should_cancel():
                self._closed = True
                raise ParakeetSessionCancelled("transcription was cancelled")
        except (ParakeetSessionCancelled, asyncio.CancelledError):
            raise
        except Exception as error:
            self._closed = True
            raise ParakeetSessionFailure(
                "the Parakeet cancellation check failed"
            ) from error
        artifact_started = time.perf_counter()
        try:
            artifacts = await resolve_parakeet_artifacts(
                vad=self.vad, should_cancel=self._should_cancel
            )
        except ParakeetArtifactCancelled as error:
            self._closed = True
            raise ParakeetSessionCancelled("transcription was cancelled") from error
        except sandbox_shim.SandboxTeardownError:
            self._closed = True
            self.teardown_failed = True
            raise
        except asyncio.CancelledError:
            self._closed = True
            raise
        except Exception:
            self._closed = True
            raise
        logger.info(
            "Parakeet artifact resolution completed",
            extra={
                "event": "parakeet_artifact_resolution_completed",
                "duration_ms": _duration_ms(artifact_started),
                "vad": self.vad,
            },
        )
        self._artifacts = artifacts
        startup_started = time.perf_counter()
        try:
            handle = await sandbox_shim.open_sandboxed_process(
                [sys.executable, "-c", PARAKEET_SESSION_BOOTSTRAP],
                policy=parakeet_inference_policy(self.expected_rows),
                extra_env=parakeet_inference_env(artifacts),
                # SandboxedProcess uses the cancellation hook as its bounded
                # I/O poll cadence. Supplying a false hook also avoids a
                # platform/runtime lost-wakeup path for direct calls that do
                # not otherwise need cancellation.
                should_cancel=self._should_cancel or _never_cancel,
            )
        except sandbox_shim.SandboxProcessCancelledError as error:
            self._closed = True
            raise ParakeetSessionCancelled("transcription was cancelled") from error
        except sandbox_shim.SandboxTeardownError:
            self._closed = True
            self.teardown_failed = True
            raise
        except asyncio.CancelledError:
            self._closed = True
            raise
        except Exception as error:
            self._closed = True
            raise ParakeetSessionFailure(
                "the Parakeet worker process could not be started"
            ) from error
        self._handle = handle
        try:
            response = await handle.exchange_frame(
                _encode_frame(parakeet_init_frame(artifacts, vad=self.vad)),
                wall_seconds=STARTUP_WALL_SECONDS,
                response_limit=MAX_RESPONSE_BYTES,
            )
            frame = _decode_frame(response)
            if frame.get("type") == "fatal":
                _raise_validated_fatal(frame)
            self._onnx_threads = _validate_ready(frame)
        except sandbox_shim.SandboxProcessCancelledError as error:
            await self._abort()
            raise ParakeetSessionCancelled("transcription was cancelled") from error
        except asyncio.CancelledError:
            await self._abort()
            raise
        except sandbox_shim.SandboxTeardownError:
            self._closed = True
            self.teardown_failed = True
            raise
        except Exception as error:
            await self._abort()
            if isinstance(error, (ParakeetArtifactUnavailable, ParakeetSessionFailure)):
                raise
            raise ParakeetSessionFailure(
                "the Parakeet worker failed during startup"
            ) from error
        logger.info(
            "Parakeet model worker ready",
            extra={
                "event": "parakeet_model_ready",
                "duration_ms": _duration_ms(startup_started),
                "expected_rows": self.expected_rows,
                "vad": self.vad,
                **self._worker_log_fields(handle),
            },
        )

    async def transcribe(self, path: str) -> dict[str, Any]:
        if self._closed:
            raise ParakeetSessionFailure("the Parakeet session is already closed")
        if self._in_flight:
            raise ParakeetSessionFailure("the Parakeet session already has a request")
        self._in_flight = True
        try:
            await self._ensure_started()
            if self._closed:
                raise ParakeetSessionFailure("the Parakeet session is already closed")
            assert self._handle is not None
            handle = self._handle
            self._request_count += 1
            request_id = f"r{self._request_count}"
            request = {
                "schema_version": SCHEMA_VERSION,
                "type": "request",
                "request_id": request_id,
                "operation": "transcribe",
                "input": {"path": os.path.abspath(path)},
            }
            request_started = time.perf_counter()
            try:
                response = await handle.exchange_frame(
                    _encode_frame(request),
                    wall_seconds=REQUEST_WALL_SECONDS,
                    response_limit=MAX_RESPONSE_BYTES,
                )
                result = _validated_result(_decode_frame(response), request_id)
            except ParakeetRowError:
                # A valid row-error frame does not poison the child.
                logger.info(
                    "Parakeet request completed",
                    extra={
                        "event": "parakeet_request_completed",
                        "duration_ms": _duration_ms(request_started),
                        "request_index": self._request_count,
                        "status": "row_error",
                        **self._worker_log_fields(handle),
                    },
                )
                raise
            except sandbox_shim.SandboxProcessCancelledError as error:
                await self._abort()
                raise ParakeetSessionCancelled("transcription was cancelled") from error
            except asyncio.CancelledError:
                await self._abort()
                raise
            except sandbox_shim.SandboxTeardownError:
                self._closed = True
                self.teardown_failed = True
                raise
            except Exception as error:
                await self._abort()
                if isinstance(error, ParakeetSessionFailure):
                    raise
                raise ParakeetSessionFailure(
                    "the Parakeet worker failed during transcription"
                ) from error
            logger.info(
                "Parakeet request completed",
                extra={
                    "event": "parakeet_request_completed",
                    "duration_ms": _duration_ms(request_started),
                    "request_index": self._request_count,
                    "status": "ok",
                    **self._worker_log_fields(handle),
                },
            )
            return result
        finally:
            self._in_flight = False

    async def close(self) -> None:
        """Close/reap the child; a post-work nonzero exit is only a warning."""

        self._closed = True
        if self.teardown_failed:
            return
        handle, self._handle = self._handle, None
        if handle is None:
            return
        close_started = time.perf_counter()
        try:
            cancelled = self._should_cancel is not None and self._should_cancel()
        except Exception as error:
            await self._abort_flagging(handle)
            logger.warning(
                "Parakeet cancellation check failed during verified close",
                extra={
                    "category": type(error).__name__,
                    "requests": self._request_count,
                },
            )
            raise
        except BaseException:
            # asyncio.CancelledError included: abort, then let it propagate.
            await self._abort_flagging(handle)
            raise
        try:
            if cancelled:
                await handle.abort()
                logger.info(
                    "Parakeet session closed",
                    extra={
                        "event": "parakeet_session_closed",
                        "duration_ms": _duration_ms(close_started),
                        "requests": self._request_count,
                        "status": "cancelled",
                        **self._worker_log_fields(handle),
                    },
                )
                return
            returncode = await handle.close(
                _encode_frame({"schema_version": SCHEMA_VERSION, "type": "close"}),
                wall_seconds=CLOSE_WALL_SECONDS,
            )
            if returncode != 0:
                logger.warning(
                    "Parakeet worker exited nonzero during verified close",
                    extra={"returncode": returncode, "requests": self._request_count},
                )
            logger.info(
                "Parakeet session closed",
                extra={
                    "event": "parakeet_session_closed",
                    "duration_ms": _duration_ms(close_started),
                    "requests": self._request_count,
                    "status": "ok" if returncode == 0 else "nonzero",
                    "returncode": returncode,
                    **self._worker_log_fields(handle),
                },
            )
        except sandbox_shim.SandboxTeardownError:
            self.teardown_failed = True
            raise
        except asyncio.CancelledError:
            await self._abort_flagging(handle)
            raise
        except Exception as error:
            # Close anomalies after all row responses do not erase committed
            # work, but cleanup must still be verified.
            await self._abort_flagging(handle)
            logger.warning(
                "Parakeet worker needed teardown escalation during close",
                extra={
                    "category": type(error).__name__,
                    "requests": self._request_count,
                },
            )
