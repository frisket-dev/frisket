"""Shared parent-side machinery for run-scoped sandboxed framed-IPC sessions.

``FramedSession`` owns the lifecycle that the resident spaCy, Opus-MT,
classifier and EmbeddingGemma sessions share: a lazily launched sandboxed
child with an init/ready handshake, the bounded frame exchange, and close.
``_serialized`` runs one request at a time under the lock and maps
cancellation to an abort; EmbeddingGemma keeps its own request wrapper because
its per-request staging directory is created outside that failure mapping.

The module-level helpers are the parts that are *genuinely identical* between
the local engine session managers (RapidOCR, Parakeet, and the GPU
transcription session that is expected next): monotonic duration measurement,
the bounded ``should_cancel`` poll hook, and Linux peak-memory
(``VmHWM``/``VmPeak``) sampling.

Engine-specific wire validation (frame decoders, exact-field/schema checks,
``ready``/``ack``/result validators) and the Parakeet and RapidOCR abort/close
state machines deliberately stay in their own modules: those differ in error
type, diagnostic wording, schema version, and cleanup steps (e.g. RapidOCR's
staging root), and unifying them would change behavior rather than remove
duplication.

The module must stay dependency-light: importing it must not pull in ONNX
Runtime, NumPy, OpenCV, Pillow, or any model library.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from frisket.engine._workers.framing import encode_frame, reject_constant
from frisket.engine.sandbox import fence, shim


def duration_ms(started: float) -> float:
    """Milliseconds elapsed since a ``time.perf_counter()`` reading, clamped."""

    return round(max(0.0, time.perf_counter() - started) * 1000, 3)


def never_cancel() -> bool:
    """A false ``should_cancel`` hook for callers that never cancel.

    SandboxedProcess uses the cancellation hook as its bounded I/O poll cadence.
    Supplying a false hook keeps that path and also avoids the no-hook
    lost-wakeup found by the Parakeet smoke.
    """

    return False


def finite_number(value: Any) -> bool:
    """True only for a real (non-bool) finite ``int``/``float``."""

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def parse_proc_status_memory(status_text: str) -> dict[str, int]:
    """Parse Linux peak-memory fields without trusting other proc contents."""

    wanted = {"VmHWM": "vm_hwm_bytes", "VmPeak": "vm_peak_bytes"}
    result: dict[str, int] = {}
    for line in status_text.splitlines():
        key, separator, raw_value = line.partition(":")
        output_key = wanted.get(key)
        if not separator or output_key is None:
            continue
        parts = raw_value.split()
        if len(parts) != 2 or parts[1] != "kB":
            continue
        try:
            kibibytes = int(parts[0])
        except ValueError:
            continue
        if kibibytes < 0:
            continue
        result[output_key] = kibibytes * 1024
    return result


def read_linux_peak_memory(pid: Any) -> dict[str, int]:
    """Read a child's ``VmHWM``/``VmPeak`` from ``/proc``; ``{}`` when unknown."""

    if (
        not sys.platform.startswith("linux")
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
    ):
        return {}
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as stream:
            return parse_proc_status_memory(stream.read())
    except (OSError, UnicodeError):
        return {}


class FramedSession:
    """One lazily launched, sandboxed worker serving one request at a time.

    Subclasses set the frame limits, sandbox ``_op`` label and memory cap, and
    supply the exceptions for an oversized request and a closed session. They
    override ``_worker_error`` to map worker ``error`` frames and ``_failed`` to
    decide which failures keep the session usable.
    """

    _request_limit: ClassVar[int]
    _response_limit: ClassVar[int]
    _wall_seconds: ClassVar[int] = 300
    _memory_mb: ClassVar[int]
    _op: ClassVar[str]
    _invalid_response: ClassVar[str]
    _close_frame: ClassVar[dict[str, Any]] = {"type": "close"}

    def __init__(self, *, cancelled: Callable[[], bool] | None = None) -> None:
        self._cancelled = cancelled or never_cancel
        self._lock = asyncio.Lock()
        self._handle: shim.SandboxedProcess | None = None
        self._closed = False

    def _request_too_large(self) -> Exception:
        raise NotImplementedError

    def _closed_error(self) -> Exception:
        raise NotImplementedError

    def _worker_error(self, frame: dict[str, Any]) -> Exception | None:
        """The exception for a worker ``error`` frame; ``None`` returns the frame."""

        return None

    async def _failed(self, exc: Exception) -> Exception:
        """Settle the session after a request failure; return what to raise."""

        await self._close(abort=True)
        return exc

    async def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = encode_frame(request)
        if len(payload) > self._request_limit:
            raise self._request_too_large()
        assert self._handle is not None
        raw = await self._handle.exchange_frame(
            payload,
            wall_seconds=self._wall_seconds,
            response_limit=self._response_limit,
            request_limit=self._request_limit,
        )
        response = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(response, dict):
            raise ValueError(self._invalid_response)
        if response.get("type") == "error":
            error = self._worker_error(response)
            if error is not None:
                raise error
        return response

    async def _launch(
        self,
        argv: list[str],
        init: dict[str, Any],
        *,
        reads: tuple[str, ...],
        environment: dict[str, str],
    ) -> dict[str, Any]:
        """Start the confined child and return its answer to ``init``."""

        self._handle = await shim.open_sandboxed_process(
            argv,
            policy=shim.SandboxPolicy(
                cpu_seconds=86400,
                memory_mb=self._memory_mb,
                wall_seconds=self._wall_seconds,
                trusted_python_netwall=True,
                allowed_extra_env=list(environment),
                confine=fence.Confinement(
                    op=self._op,
                    read=(*reads, "/proc/cpuinfo", "/sys/devices/system/cpu"),
                ),
            ),
            extra_env=environment,
            should_cancel=self._cancelled,
        )
        return await self._exchange(init)

    async def _serialized[T](self, work: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            if self._closed:
                raise self._closed_error()
            try:
                if self._cancelled():
                    raise asyncio.CancelledError
                return await work()
            except (shim.SandboxProcessCancelledError, asyncio.CancelledError):
                await self._close(abort=True)
                raise asyncio.CancelledError from None
            except Exception as exc:
                failure = await self._failed(exc)
                if failure is exc:
                    raise
                raise failure from exc

    async def _close(self, *, abort: bool) -> None:
        self._closed = True
        if self._handle is not None:
            if abort:
                await self._handle.abort()
            else:
                await self._handle.close(
                    encode_frame(self._close_frame), wall_seconds=10
                )

    async def aclose(self) -> None:
        async with self._lock:
            if not self._closed:
                await self._close(abort=False)
