"""Parent-side lifecycle for a bounded run-scoped RapidOCR process pool.

The module is intentionally dependency-light: importing the OCR catalog must
not import RapidOCR, ONNX Runtime, OpenCV, NumPy, or Pillow.  Those packages are
loaded only by the sandboxed child after process ownership and the offline
network wall are established.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.engine.sandbox import fence
from frisket.engine.sandbox import shim as sandbox_shim
from frisket.engine.sandbox.shim import SandboxPolicy
from frisket.runtime.launch import worker_argv

from .rapidocr_worker import SCHEMA_VERSION
from .session_base import duration_ms as _duration_ms
from .session_base import encode_frame as _encode_frame
from .session_base import finite_number as _finite_number
from .session_base import never_cancel as _never_cancel
from .session_base import read_linux_peak_memory as _read_linux_peak_memory
from .session_base import reject_constant as _reject_constant

logger = logging.getLogger(__name__)

STARTUP_WALL_SECONDS = 300.0
REQUEST_WALL_SECONDS = 3_600.0
CLOSE_WALL_SECONDS = 5.0
MAX_ACK_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_CPU_SECONDS = 2_147_483_647
MAX_POOL_WORKERS = 2
MAX_TOPOLOGY_THREADS = 4
DEFAULT_MEMORY_MB = 4096

# Conservative admission estimate, not a claim about the sandbox's exact RSS.
# It prevents an automatic two-process pool on small machines while leaving the
# 4-vCPU/8-GiB install proof shape eligible.  The resolved topology also
# divides memory left after the reserve across its workers, so the SDK can pass
# an aggregate-bounded per-child RLIMIT into each session.
_HOST_MEMORY_RESERVE_BYTES = 1 * 1024**3
_WORKER_MEMORY_ADMISSION_BYTES = 2 * 1024**3

_ENV_WORKERS = "FRISKET_RAPIDOCR_WORKERS"
_ENV_ONNX_THREADS = "FRISKET_RAPIDOCR_ONNX_THREADS"
_ENV_OPENCV_THREADS = "FRISKET_RAPIDOCR_OPENCV_THREADS"
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")


class RapidOCRSessionFailure(RuntimeError):
    """The persistent worker or its protocol is no longer usable."""


class RapidOCRSessionCancelled(RuntimeError):
    """Operator cancellation stopped and reaped an in-flight worker."""


class RapidOCRRowError(RuntimeError):
    """One valid request failed without poisoning the worker."""


@dataclass(frozen=True)
class RapidOCRTopology:
    effective_cpus: int
    effective_memory_bytes: int | None
    workers: int
    onnx_intra_threads: int
    onnx_inter_threads: int
    opencv_threads: int
    memory_mb: int = DEFAULT_MEMORY_MB


def _read_small_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _cgroup_cpu_quota() -> int | None:
    """Return a conservative whole-CPU cgroup quota when one is configured."""

    cpu_max = _read_small_text(Path("/sys/fs/cgroup/cpu.max"))
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) == 2 and parts[0] != "max":
            quota = _positive_int(parts[0])
            period = _positive_int(parts[1])
            if quota is not None and period is not None:
                return max(1, quota // period)
    quota = _positive_int(_read_small_text(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")))
    period = _positive_int(
        _read_small_text(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    )
    if quota is not None and period is not None:
        return max(1, quota // period)
    return None


def _effective_cpu_count() -> int:
    candidates: list[int] = []
    try:
        affinity = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        affinity = None
    if affinity:
        candidates.append(len(affinity))
    try:
        cpu_count = os.cpu_count()
    except Exception:
        cpu_count = None
    if isinstance(cpu_count, int) and not isinstance(cpu_count, bool) and cpu_count > 0:
        candidates.append(cpu_count)
    quota = _cgroup_cpu_quota()
    if quota is not None:
        candidates.append(quota)
    return max(1, min(candidates)) if candidates else 1


def _physical_memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if (
        isinstance(pages, int)
        and isinstance(page_size, int)
        and pages > 0
        and page_size > 0
    ):
        return pages * page_size
    return None


def _cgroup_memory_limit_bytes() -> int | None:
    for path in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        raw = _read_small_text(path)
        if raw and raw != "max":
            value = _positive_int(raw)
            if value is not None:
                return value
    return None


def _effective_memory_bytes() -> int | None:
    values = [
        value
        for value in (_physical_memory_bytes(), _cgroup_memory_limit_bytes())
        if value is not None
    ]
    return min(values) if values else None


def _bounded_env_int(name: str, *, maximum: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}") from error
    if isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def rapidocr_topology(expected_rows: int | None = None) -> RapidOCRTopology:
    """Choose a small uniform topology from effective CPU and memory limits.

    CPU capacity is the minimum of host count, affinity, and a finite cgroup
    quota.  Memory clamps automatic worker count when a physical/cgroup limit
    is readable.  Unknown memory never permits more than the hard two-worker
    ceiling.  Explicit environment requests remain bounded by the same CPU and
    memory capacities; they cannot force oversubscription.
    """

    effective_cpus = _effective_cpu_count()
    effective_memory = _effective_memory_bytes()
    single_row = expected_rows is not None and int(expected_rows) == 1
    cpu_worker_cap = 1 if single_row else min(MAX_POOL_WORKERS, effective_cpus)
    memory_worker_cap = MAX_POOL_WORKERS
    if effective_memory is not None:
        usable = max(0, effective_memory - _HOST_MEMORY_RESERVE_BYTES)
        memory_worker_cap = max(
            1,
            min(MAX_POOL_WORKERS, usable // _WORKER_MEMORY_ADMISSION_BYTES),
        )
    worker_cap = max(1, min(cpu_worker_cap, memory_worker_cap))
    requested_workers = _bounded_env_int(_ENV_WORKERS, maximum=MAX_POOL_WORKERS)
    workers = min(requested_workers or worker_cap, worker_cap)

    if effective_memory is None:
        memory_mb = DEFAULT_MEMORY_MB
    else:
        usable_memory_mb = max(
            1, (effective_memory - _HOST_MEMORY_RESERVE_BYTES) // (1024**2)
        )
        memory_mb = min(DEFAULT_MEMORY_MB, usable_memory_mb // workers)

    uniform_cpu_cap = max(1, effective_cpus // workers)
    requested_onnx = _bounded_env_int(_ENV_ONNX_THREADS, maximum=MAX_TOPOLOGY_THREADS)
    onnx_threads = min(
        requested_onnx or min(MAX_TOPOLOGY_THREADS, uniform_cpu_cap),
        MAX_TOPOLOGY_THREADS,
        uniform_cpu_cap,
    )
    requested_opencv = _bounded_env_int(
        _ENV_OPENCV_THREADS, maximum=MAX_TOPOLOGY_THREADS
    )
    opencv_threads = min(
        requested_opencv or 1,
        MAX_TOPOLOGY_THREADS,
        uniform_cpu_cap,
    )
    return RapidOCRTopology(
        effective_cpus=effective_cpus,
        effective_memory_bytes=effective_memory,
        workers=workers,
        onnx_intra_threads=max(1, onnx_threads),
        onnx_inter_threads=1,
        opencv_threads=max(1, opencv_threads),
        memory_mb=max(1, memory_mb),
    )


def _decode_frame(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RapidOCRSessionFailure(
            "the RapidOCR worker returned invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise RapidOCRSessionFailure("the RapidOCR worker returned a non-object frame")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RapidOCRSessionFailure("the RapidOCR worker returned the wrong schema")
    return value


def _require_exact_fields(
    value: dict[str, Any], allowed: set[str], *, frame: str
) -> None:
    if set(value) != allowed:
        raise RapidOCRSessionFailure(
            f"the RapidOCR worker returned an invalid {frame} frame"
        )


def _validate_error(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RapidOCRSessionFailure("the RapidOCR worker returned an invalid error")
    _require_exact_fields(value, {"code", "message"}, frame="error")
    code = value.get("code")
    message = value.get("message")
    if (
        not isinstance(code, str)
        or not _ERROR_CODE_RE.fullmatch(code)
        or not isinstance(message, str)
        or len(message) > 500
    ):
        raise RapidOCRSessionFailure("the RapidOCR worker returned an invalid error")
    return {"code": code, "message": message}


def _raise_fatal(frame: dict[str, Any]) -> None:
    _require_exact_fields(frame, {"schema_version", "type", "error"}, frame="fatal")
    if frame.get("type") != "fatal":
        raise RapidOCRSessionFailure("the RapidOCR worker returned an invalid fatal")
    error = _validate_error(frame.get("error"))
    if error["code"] == "invalid_language":
        raise RapidOCRRowError(error["message"])
    if error["code"] == "rapidocr_model_unavailable":
        raise RapidOCRSessionFailure("the configured RapidOCR models are unavailable")
    raise RapidOCRSessionFailure("the RapidOCR worker reported a fatal error")


def _validate_ready(
    frame: dict[str, Any],
    *,
    requested_intra: int,
    requested_inter: int,
    requested_opencv: int,
) -> dict[str, Any]:
    _require_exact_fields(
        frame, {"schema_version", "type", "engine", "runtime"}, frame="ready"
    )
    if frame.get("type") != "ready" or frame.get("engine") != "rapidocr":
        raise RapidOCRSessionFailure("the RapidOCR worker did not become ready")
    runtime = frame.get("runtime")
    if not isinstance(runtime, dict):
        raise RapidOCRSessionFailure(
            "the RapidOCR worker returned invalid runtime data"
        )
    _require_exact_fields(
        runtime,
        {
            "rapidocr_version",
            "onnxruntime_version",
            "onnx_threads",
            "opencv_threads",
            "opencv_threads_observed",
            "opencv_opencl",
            "model_init_ms",
        },
        frame="runtime",
    )
    for version_key in ("rapidocr_version", "onnxruntime_version"):
        version = runtime.get(version_key)
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise RapidOCRSessionFailure(
                "the RapidOCR worker returned invalid runtime data"
            )
    onnx = runtime.get("onnx_threads")
    if not isinstance(onnx, dict):
        raise RapidOCRSessionFailure(
            "the RapidOCR worker returned invalid runtime data"
        )
    _require_exact_fields(
        onnx,
        {"mode", "intra", "inter", "sessions_verified"},
        frame="ONNX threads",
    )
    mode = onnx.get("mode")
    intra = onnx.get("intra")
    inter = onnx.get("inter")
    sessions_verified = onnx.get("sessions_verified")
    expected = (requested_intra, requested_inter) if mode == "shared_global" else (1, 1)
    if (
        mode not in {"shared_global", "per_session_fallback"}
        or isinstance(intra, bool)
        or not isinstance(intra, int)
        or isinstance(inter, bool)
        or not isinstance(inter, int)
        or (intra, inter) != expected
        or sessions_verified != (3 if mode == "shared_global" else 0)
        or runtime.get("opencv_threads") != requested_opencv
        or isinstance(runtime.get("opencv_threads_observed"), bool)
        or not isinstance(runtime.get("opencv_threads_observed"), int)
        or runtime["opencv_threads_observed"] < 1
        or runtime.get("opencv_opencl") is not False
        or not _finite_number(runtime.get("model_init_ms"))
        or float(runtime["model_init_ms"]) < 0
    ):
        raise RapidOCRSessionFailure(
            "the RapidOCR worker returned invalid runtime data"
        )
    return runtime


def _validate_ack(
    frame: dict[str, Any], request_id: str, *, expected_pages: int
) -> dict[str, Any]:
    if frame.get("type") == "fatal":
        _raise_fatal(frame)
    if frame.get("type") != "result" or frame.get("request_id") != request_id:
        raise RapidOCRSessionFailure("the RapidOCR worker returned a mismatched result")
    if frame.get("ok") is False:
        _require_exact_fields(
            frame,
            {"schema_version", "type", "request_id", "ok", "error"},
            frame="row error",
        )
        _validate_error(frame.get("error"))
        raise RapidOCRRowError("images could not be read or recognized")
    _require_exact_fields(
        frame,
        {"schema_version", "type", "request_id", "ok", "metrics"},
        frame="result acknowledgement",
    )
    metrics = frame.get("metrics")
    if frame.get("ok") is not True or not isinstance(metrics, dict):
        raise RapidOCRSessionFailure("the RapidOCR worker returned an invalid result")
    _require_exact_fields(
        metrics,
        {
            "duration_ms",
            "page_count",
            "det_ms",
            "cls_ms",
            "rec_ms",
            "recognized_blocks",
            "input_pixels",
            "max_page_pixels",
            "max_page_width",
            "max_page_height",
            "vm_hwm_bytes",
            "vm_peak_bytes",
            "threads",
        },
        frame="metrics",
    )
    if (
        not _finite_number(metrics.get("duration_ms"))
        or float(metrics["duration_ms"]) < 0
        or metrics.get("page_count") != expected_pages
        or any(
            not _finite_number(metrics.get(key)) or float(metrics[key]) < 0
            for key in ("det_ms", "cls_ms", "rec_ms")
        )
        or isinstance(metrics.get("recognized_blocks"), bool)
        or not isinstance(metrics.get("recognized_blocks"), int)
        or metrics["recognized_blocks"] < 0
        or any(
            isinstance(metrics.get(key), bool)
            or not isinstance(metrics.get(key), int)
            or metrics[key] < 0
            for key in (
                "input_pixels",
                "max_page_pixels",
                "max_page_width",
                "max_page_height",
            )
        )
        or metrics["max_page_pixels"] > metrics["input_pixels"]
        or any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for value in (
                metrics.get("vm_hwm_bytes"),
                metrics.get("vm_peak_bytes"),
                metrics.get("threads"),
            )
        )
    ):
        raise RapidOCRSessionFailure("the RapidOCR worker returned invalid metrics")
    return metrics


def _validate_pages(value: Any, *, expected_pages: int) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"pages"}:
        raise RapidOCRSessionFailure("the RapidOCR result file is malformed")
    pages = value.get("pages")
    if not isinstance(pages, list) or len(pages) != expected_pages:
        raise RapidOCRSessionFailure(
            "the RapidOCR result file has the wrong page count"
        )
    for page in pages:
        if not isinstance(page, dict) or set(page) != {"text", "blocks"}:
            raise RapidOCRSessionFailure("the RapidOCR result file is malformed")
        if not isinstance(page.get("text"), str) or not isinstance(
            page.get("blocks"), list
        ):
            raise RapidOCRSessionFailure("the RapidOCR result file is malformed")
        for block in page["blocks"]:
            if (
                not isinstance(block, dict)
                or not {"text"} <= set(block) <= {"text", "bbox", "score"}
                or not isinstance(block.get("text"), str)
            ):
                raise RapidOCRSessionFailure("the RapidOCR result file is malformed")
            if "bbox" in block:
                bbox = block["bbox"]
                if (
                    not isinstance(bbox, list)
                    or len(bbox) != 4
                    or any(
                        not isinstance(point, list)
                        or len(point) != 2
                        or any(
                            isinstance(coordinate, bool)
                            or not isinstance(coordinate, int)
                            for coordinate in point
                        )
                        for point in bbox
                    )
                ):
                    raise RapidOCRSessionFailure(
                        "the RapidOCR result file is malformed"
                    )
            if "score" in block and not _finite_number(block["score"]):
                raise RapidOCRSessionFailure("the RapidOCR result file is malformed")
    return pages


def _read_result_file(
    path: Path, *, request_root: Path, expected_pages: int
) -> list[dict[str, Any]]:
    try:
        path_metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(request_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise RapidOCRSessionFailure(
            "the RapidOCR worker did not produce a contained result file"
        ) from error
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
        raise RapidOCRSessionFailure(
            "the RapidOCR worker produced a non-regular result file"
        )
    if not 0 < path_metadata.st_size <= MAX_RESULT_BYTES:
        raise RapidOCRSessionFailure("the RapidOCR result file exceeded its limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_size != path_metadata.st_size
                or opened_metadata.st_size > MAX_RESULT_BYTES
            ):
                raise RapidOCRSessionFailure(
                    "the RapidOCR result file changed during validation"
                )
            payload = stream.read(MAX_RESULT_BYTES + 1)
    except RapidOCRSessionFailure:
        raise
    except OSError as error:
        raise RapidOCRSessionFailure(
            "the RapidOCR result file could not be read"
        ) from error
    if len(payload) > MAX_RESULT_BYTES:
        raise RapidOCRSessionFailure("the RapidOCR result file exceeded its limit")
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RapidOCRSessionFailure(
            "the RapidOCR result file is invalid JSON"
        ) from error
    return _validate_pages(value, expected_pages=expected_pages)


def _cumulative_cpu_seconds(expected_rows: int) -> int:
    return min(max(1, int(expected_rows)) * 1_800, MAX_CPU_SECONDS)


class RapidOCRProcessSession:
    """One serial framed child and its fixed, parent-owned staging root."""

    def __init__(
        self,
        *,
        worker_index: int,
        expected_rows: int,
        language: str | None,
        model_root_dir: str | None,
        onnx_intra_threads: int,
        onnx_inter_threads: int,
        opencv_threads: int,
        should_cancel: Callable[[], bool] | None,
        memory_mb: int = DEFAULT_MEMORY_MB,
    ) -> None:
        self.worker_index = worker_index
        self.expected_rows = max(1, int(expected_rows))
        self.language = language
        self.model_root_dir = (
            str(Path(model_root_dir).resolve()) if model_root_dir is not None else None
        )
        self.onnx_intra_threads = _checked_positive_int(
            onnx_intra_threads, name="onnx_intra_threads", maximum=64
        )
        self.onnx_inter_threads = _checked_positive_int(
            onnx_inter_threads, name="onnx_inter_threads", maximum=64
        )
        self.opencv_threads = _checked_positive_int(
            opencv_threads, name="opencv_threads", maximum=64
        )
        self.memory_mb = _checked_positive_int(
            memory_mb, name="memory_mb", maximum=1_048_576
        )
        self._should_cancel = should_cancel
        self._handle: Any | None = None
        self._staging_root: Path | None = None
        self._runtime: dict[str, Any] | None = None
        self._startup_row_error: str | None = None
        self._peak_memory: dict[str, int] = {}
        self._request_count = 0
        self._closed = False
        self._in_flight = False
        self.teardown_failed = False

    @property
    def started(self) -> bool:
        return self._handle is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def _sample_peak_memory(self, handle: Any) -> dict[str, int]:
        try:
            pid = handle.pid
        except (AttributeError, OSError, RuntimeError):
            pid = None
        for key, value in _read_linux_peak_memory(pid).items():
            self._peak_memory[key] = max(value, self._peak_memory.get(key, 0))
        return dict(self._peak_memory)

    def _log_fields(self, handle: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "worker_index": self.worker_index,
            **self._sample_peak_memory(handle),
        }
        if self._runtime is not None:
            onnx = self._runtime["onnx_threads"]
            fields.update(
                {
                    "onnx_thread_mode": onnx["mode"],
                    "onnx_intra_threads": onnx["intra"],
                    "onnx_inter_threads": onnx["inter"],
                    "opencv_threads": self._runtime["opencv_threads"],
                    "opencv_threads_observed": self._runtime["opencv_threads_observed"],
                    "opencv_opencl": self._runtime["opencv_opencl"],
                    "onnxruntime_version": self._runtime["onnxruntime_version"],
                    "rapidocr_version": self._runtime["rapidocr_version"],
                    "model_init_ms": self._runtime["model_init_ms"],
                    "onnx_sessions_verified": onnx["sessions_verified"],
                }
            )
        return fields

    def _cleanup_staging_root(self) -> None:
        if self.teardown_failed:
            return
        root, self._staging_root = self._staging_root, None
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    async def _abort_handle(self, handle: Any) -> None:
        try:
            await handle.abort()
        except sandbox_shim.SandboxTeardownError:
            self.teardown_failed = True
            raise
        else:
            self._cleanup_staging_root()

    async def _abort(self) -> None:
        self._closed = True
        handle, self._handle = self._handle, None
        if handle is None:
            self._cleanup_staging_root()
            return
        await self._abort_handle(handle)

    def _cancelled(self) -> bool:
        if self._should_cancel is None:
            return False
        try:
            return bool(self._should_cancel())
        except Exception as error:
            raise RapidOCRSessionFailure(
                "the RapidOCR cancellation check failed"
            ) from error

    def _confinement(self) -> fence.Confinement:
        """What this OCR worker may touch, derived from what it does.

        A session reads ONNX weights and page images and writes JSON results.
        The page images need no rule at all: `_stage_request` copies every
        input into `staging_root/requests/<id>/`, and `staging_root` is the
        child's own cwd, which the fence grants read+write by construction --
        so a long-lived session whose inputs change per request still needs a
        fixed ruleset, which is the only thing Landlock can express.

        What is named here:

        `model_root_dir` -- the shared model cache, when the package's own
        models are incomplete and OCR is running from
        `~/.cache/frisket/models/rapidocr` instead. None means the weights are
        in site-packages, which is already readable because it is importable.

        `/proc/cpuinfo` and `/sys/devices/system/cpu` -- measured, not guessed:
        without them onnxruntime's cpuinfo probe fails, it logs "May cause CPU
        EP performance degradation due to undetected CPU features", and it
        stops seeing this machine's AVX support. They are machine facts, hold
        nothing of the operator's, and buy back the inference speed.

        Not granted, on purpose: `/sys/class/drm`. onnxruntime's GPU discovery
        walks it and logs a permission denial; the CPU execution provider does
        not need it and this build has no GPU provider to find.
        """
        read: list[str] = ["/proc/cpuinfo", "/sys/devices/system/cpu"]
        if self.model_root_dir:
            read.append(str(self.model_root_dir))
        return fence.Confinement(op="ocr (rapidocr worker)", read=tuple(read))

    async def _ensure_started(self) -> None:
        if self._startup_row_error is not None:
            raise RapidOCRRowError(self._startup_row_error)
        if self._closed:
            raise RapidOCRSessionFailure("the RapidOCR session is already closed")
        if self._handle is not None:
            return
        if self._cancelled():
            self._closed = True
            raise RapidOCRSessionCancelled("OCR was cancelled")

        staging_root = Path(
            tempfile.mkdtemp(prefix="frisket-rapidocr-session-")
        ).resolve()
        (staging_root / "requests").mkdir(mode=0o700)
        self._staging_root = staging_root
        policy = SandboxPolicy(
            cpu_seconds=_cumulative_cpu_seconds(self.expected_rows),
            wall_seconds=int(REQUEST_WALL_SECONDS),
            memory_mb=self.memory_mb,
            trusted_python_netwall=True,
            confine=self._confinement(),
        )
        startup_started = time.perf_counter()
        try:
            handle = await sandbox_shim.open_sandboxed_process(
                worker_argv("rapidocr-session"),
                policy=policy,
                scratch_dir=staging_root,
                should_cancel=self._should_cancel or _never_cancel,
            )
        except sandbox_shim.SandboxProcessCancelledError as error:
            self._closed = True
            self._cleanup_staging_root()
            raise RapidOCRSessionCancelled("OCR was cancelled") from error
        except sandbox_shim.SandboxTeardownError:
            self._closed = True
            self.teardown_failed = True
            raise
        except asyncio.CancelledError:
            self._closed = True
            self._cleanup_staging_root()
            raise
        except Exception as error:
            self._closed = True
            self._cleanup_staging_root()
            raise RapidOCRSessionFailure(
                "the RapidOCR worker process could not be started"
            ) from error
        self._handle = handle
        try:
            response = await handle.exchange_frame(
                _encode_frame(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "type": "init",
                        "config": {
                            "language": self.language,
                            "model_root_dir": self.model_root_dir,
                            "onnx_intra_threads": self.onnx_intra_threads,
                            "onnx_inter_threads": self.onnx_inter_threads,
                            "opencv_threads": self.opencv_threads,
                        },
                    }
                ),
                wall_seconds=STARTUP_WALL_SECONDS,
                response_limit=MAX_ACK_BYTES,
            )
            frame = _decode_frame(response)
            if frame.get("type") == "fatal":
                _raise_fatal(frame)
            self._runtime = _validate_ready(
                frame,
                requested_intra=self.onnx_intra_threads,
                requested_inter=self.onnx_inter_threads,
                requested_opencv=self.opencv_threads,
            )
        except sandbox_shim.SandboxProcessCancelledError as error:
            await self._abort()
            raise RapidOCRSessionCancelled("OCR was cancelled") from error
        except asyncio.CancelledError:
            await self._abort()
            raise
        except sandbox_shim.SandboxTeardownError:
            self._closed = True
            self.teardown_failed = True
            raise
        except Exception as error:
            await self._abort()
            if isinstance(error, RapidOCRRowError):
                self._startup_row_error = str(error)
                raise
            if isinstance(error, RapidOCRSessionFailure):
                raise
            raise RapidOCRSessionFailure(
                "the RapidOCR worker failed during startup"
            ) from error
        logger.info(
            "RapidOCR model worker ready",
            extra={
                "event": "rapidocr_model_ready",
                "duration_ms": _duration_ms(startup_started),
                **self._log_fields(handle),
            },
        )

    def _stage_request(self, paths: list[str | Path], request_id: str) -> Path:
        if self._staging_root is None:
            raise RapidOCRSessionFailure("the RapidOCR staging root is unavailable")
        request_root = self._staging_root / "requests" / request_id
        try:
            request_root.mkdir(mode=0o700)
            for index, raw_path in enumerate(paths):
                source = Path(raw_path).resolve(strict=True)
                source_metadata = source.stat()
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise OSError("OCR input is not a regular file")
                destination = request_root / f"page-{index:06d}.img"
                with (
                    source.open("rb") as input_stream,
                    destination.open("xb") as output_stream,
                ):
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        except (OSError, ValueError) as error:
            shutil.rmtree(request_root, ignore_errors=True)
            raise RapidOCRRowError("OCR input could not be staged") from error
        return request_root

    def _cleanup_request(self, request_root: Path) -> None:
        if not self.teardown_failed:
            shutil.rmtree(request_root, ignore_errors=True)

    async def ocr(self, paths: list[str | Path]) -> list[dict[str, Any]]:
        if self._startup_row_error is not None:
            raise RapidOCRRowError(self._startup_row_error)
        if self._closed:
            raise RapidOCRSessionFailure("the RapidOCR session is already closed")
        if self._in_flight:
            raise RapidOCRSessionFailure("the RapidOCR session already has a request")
        if not isinstance(paths, list) or not paths:
            raise RapidOCRRowError("OCR needs at least one staged image")
        self._in_flight = True
        request_root: Path | None = None
        request_total_started: float | None = None
        try:
            await self._ensure_started()
            assert self._handle is not None
            assert self._staging_root is not None
            handle = self._handle
            self._request_count += 1
            request_id = f"r{self._request_count}"
            request_total_started = time.perf_counter()
            staging_started = time.perf_counter()
            try:
                request_root = self._stage_request(paths, request_id)
            except RapidOCRRowError:
                logger.info(
                    "RapidOCR request completed",
                    extra={
                        "event": "rapidocr_request_completed",
                        "duration_ms": 0.0,
                        "staging_ms": _duration_ms(staging_started),
                        "total_duration_ms": _duration_ms(request_total_started),
                        "request_index": self._request_count,
                        "page_count": len(paths),
                        "status": "row_error",
                        **self._log_fields(handle),
                    },
                )
                raise
            staging_ms = _duration_ms(staging_started)
            relative_paths = [
                f"requests/{request_id}/page-{index:06d}.img"
                for index in range(len(paths))
            ]
            result_path = request_root / "result.json"
            request_started = time.perf_counter()
            try:
                response = await handle.exchange_frame(
                    _encode_frame(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "type": "request",
                            "request_id": request_id,
                            "operation": "ocr",
                            "input": {"paths": relative_paths},
                            "output": {"path": f"requests/{request_id}/result.json"},
                        }
                    ),
                    wall_seconds=REQUEST_WALL_SECONDS,
                    response_limit=MAX_ACK_BYTES,
                )
                metrics = _validate_ack(
                    _decode_frame(response), request_id, expected_pages=len(paths)
                )
                pages = _read_result_file(
                    result_path,
                    request_root=request_root,
                    expected_pages=len(paths),
                )
            except RapidOCRRowError:
                logger.info(
                    "RapidOCR request completed",
                    extra={
                        "event": "rapidocr_request_completed",
                        "duration_ms": _duration_ms(request_started),
                        "staging_ms": staging_ms,
                        "total_duration_ms": _duration_ms(request_total_started),
                        "request_index": self._request_count,
                        "page_count": len(paths),
                        "status": "row_error",
                        **self._log_fields(handle),
                    },
                )
                raise
            except sandbox_shim.SandboxProcessCancelledError as error:
                await self._abort()
                raise RapidOCRSessionCancelled("OCR was cancelled") from error
            except asyncio.CancelledError:
                await self._abort()
                raise
            except sandbox_shim.SandboxTeardownError:
                self._closed = True
                self.teardown_failed = True
                raise
            except Exception as error:
                await self._abort()
                if isinstance(error, RapidOCRSessionFailure):
                    raise
                raise RapidOCRSessionFailure(
                    "the RapidOCR worker failed during OCR"
                ) from error
            logger.info(
                "RapidOCR request completed",
                extra={
                    "event": "rapidocr_request_completed",
                    "duration_ms": _duration_ms(request_started),
                    "staging_ms": staging_ms,
                    "total_duration_ms": _duration_ms(request_total_started),
                    "worker_duration_ms": metrics["duration_ms"],
                    "native_det_ms": metrics["det_ms"],
                    "native_cls_ms": metrics["cls_ms"],
                    "native_rec_ms": metrics["rec_ms"],
                    "recognized_blocks": metrics["recognized_blocks"],
                    "input_pixels": metrics["input_pixels"],
                    "max_page_pixels": metrics["max_page_pixels"],
                    "max_page_width": metrics["max_page_width"],
                    "max_page_height": metrics["max_page_height"],
                    "worker_vm_hwm_bytes": metrics["vm_hwm_bytes"],
                    "worker_vm_peak_bytes": metrics["vm_peak_bytes"],
                    "worker_threads": metrics["threads"],
                    "request_index": self._request_count,
                    "page_count": len(paths),
                    "status": "ok",
                    **self._log_fields(handle),
                },
            )
            return pages
        finally:
            if request_root is not None:
                self._cleanup_request(request_root)
            self._in_flight = False

    async def close(self) -> None:
        self._closed = True
        if self.teardown_failed:
            return
        handle, self._handle = self._handle, None
        if handle is None:
            self._cleanup_staging_root()
            return
        close_started = time.perf_counter()
        try:
            cancelled = self._cancelled()
            if self._in_flight or cancelled:
                await self._abort_handle(handle)
                status_value = "cancelled" if cancelled else "aborted"
                logger.info(
                    "RapidOCR session closed",
                    extra={
                        "event": "rapidocr_session_closed",
                        "duration_ms": _duration_ms(close_started),
                        "requests": self._request_count,
                        "status": status_value,
                        **self._log_fields(handle),
                    },
                )
                return
            returncode = await handle.close(
                _encode_frame({"schema_version": SCHEMA_VERSION, "type": "close"}),
                wall_seconds=CLOSE_WALL_SECONDS,
            )
            self._cleanup_staging_root()
            logger.info(
                "RapidOCR session closed",
                extra={
                    "event": "rapidocr_session_closed",
                    "duration_ms": _duration_ms(close_started),
                    "requests": self._request_count,
                    "status": "ok" if returncode == 0 else "nonzero",
                    "returncode": returncode,
                    **self._log_fields(handle),
                },
            )
        except sandbox_shim.SandboxTeardownError:
            self.teardown_failed = True
            raise
        except asyncio.CancelledError:
            await self._abort_handle(handle)
            raise
        except Exception as error:
            await self._abort_handle(handle)
            raise RapidOCRSessionFailure(
                "the RapidOCR worker needed teardown escalation during close"
            ) from error


def _checked_positive_int(value: Any, *, name: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _describe_close_failure(error: BaseException) -> str:
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _aggregate_close_failure(
    errors: list[BaseException], *, teardown: bool
) -> BaseException:
    """Build one exception that keeps every concurrent close failure's detail.

    Teardown failures keep precedence (an unprovable process-tree survivor is
    more serious than a worker/protocol failure) and their type, so callers can
    still distinguish an infrastructure teardown from a session failure.  A lone
    failure is surfaced unchanged; when several sessions fail differently their
    messages are aggregated and the originals are attached as ``close_failures``
    so no diagnostic is silently dropped during multi-worker teardown triage.
    """

    if len(errors) == 1:
        only = errors[0]
        if isinstance(
            only, (sandbox_shim.SandboxTeardownError, RapidOCRSessionFailure)
        ):
            return only
        wrapped = RapidOCRSessionFailure("a RapidOCR worker could not be closed")
        wrapped.close_failures = (only,)
        return wrapped
    detail = "; ".join(_describe_close_failure(error) for error in errors)
    aggregated: BaseException
    if teardown:
        aggregated = sandbox_shim.SandboxTeardownError(
            f"{len(errors)} RapidOCR workers failed to close cleanly: {detail}"
        )
    else:
        aggregated = RapidOCRSessionFailure(
            f"{len(errors)} RapidOCR workers could not be closed: {detail}"
        )
    aggregated.close_failures = tuple(errors)
    return aggregated


class RapidOCRProcessPool:
    """Lazy bounded pool of serial RapidOCR child sessions."""

    def __init__(
        self,
        *,
        max_workers: int,
        expected_rows: int,
        language: str | None,
        model_root_dir: str | None,
        onnx_intra_threads: int,
        onnx_inter_threads: int,
        opencv_threads: int,
        should_cancel: Callable[[], bool] | None,
        memory_mb: int = DEFAULT_MEMORY_MB,
    ) -> None:
        self.max_workers = _checked_positive_int(
            max_workers, name="max_workers", maximum=MAX_POOL_WORKERS
        )
        self.expected_rows = max(1, int(expected_rows))
        self.language = language
        self.model_root_dir = model_root_dir
        self.onnx_intra_threads = _checked_positive_int(
            onnx_intra_threads, name="onnx_intra_threads", maximum=64
        )
        self.onnx_inter_threads = _checked_positive_int(
            onnx_inter_threads, name="onnx_inter_threads", maximum=64
        )
        self.opencv_threads = _checked_positive_int(
            opencv_threads, name="opencv_threads", maximum=64
        )
        self.memory_mb = _checked_positive_int(
            memory_mb, name="memory_mb", maximum=1_048_576
        )
        self._should_cancel = should_cancel
        self._slots: asyncio.Queue[int] = asyncio.Queue()
        for index in range(self.max_workers):
            self._slots.put_nowait(index)
        self._sessions: dict[int, RapidOCRProcessSession] = {}
        self._active: set[int] = set()
        self._closed = False
        self._failed = False
        self._teardown_failed = False
        self._close_error: BaseException | None = None

    @property
    def worker_count(self) -> int:
        return self.max_workers

    @property
    def started_workers(self) -> int:
        return sum(1 for session in self._sessions.values() if session.started)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def teardown_failed(self) -> bool:
        return self._teardown_failed or any(
            session.teardown_failed for session in self._sessions.values()
        )

    def _cancelled(self) -> bool:
        if self._should_cancel is None:
            return False
        try:
            return bool(self._should_cancel())
        except Exception as error:
            raise RapidOCRSessionFailure(
                "the RapidOCR cancellation check failed"
            ) from error

    async def _acquire_slot(self) -> int:
        while True:
            if self._closed:
                raise RapidOCRSessionFailure("the RapidOCR pool is already closed")
            if self._failed:
                raise RapidOCRSessionFailure("the RapidOCR pool is unavailable")
            if self._cancelled():
                raise RapidOCRSessionCancelled("OCR was cancelled")
            try:
                return await asyncio.wait_for(self._slots.get(), timeout=0.2)
            except TimeoutError:
                continue

    def _session(self, slot: int) -> RapidOCRProcessSession:
        session = self._sessions.get(slot)
        if session is None:
            session = RapidOCRProcessSession(
                worker_index=slot + 1,
                expected_rows=self.expected_rows,
                language=self.language,
                model_root_dir=self.model_root_dir,
                onnx_intra_threads=self.onnx_intra_threads,
                onnx_inter_threads=self.onnx_inter_threads,
                opencv_threads=self.opencv_threads,
                should_cancel=self._should_cancel,
                memory_mb=self.memory_mb,
            )
            self._sessions[slot] = session
        return session

    async def ocr(self, paths: list[str | Path]) -> list[dict[str, Any]]:
        slot = await self._acquire_slot()
        self._active.add(slot)
        try:
            return await self._session(slot).ocr(paths)
        except RapidOCRRowError:
            raise
        except sandbox_shim.SandboxTeardownError:
            self._failed = True
            self._teardown_failed = True
            raise
        except (RapidOCRSessionFailure, RapidOCRSessionCancelled):
            self._failed = True
            raise
        finally:
            self._active.discard(slot)
            if not self._closed:
                self._slots.put_nowait(slot)

    async def _close_sessions(self) -> list[Any]:
        sessions = list(self._sessions.values())
        if not sessions:
            return []
        return list(
            await asyncio.gather(
                *(session.close() for session in sessions), return_exceptions=True
            )
        )

    async def close(self) -> None:
        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self._closed = True
        close_task = asyncio.ensure_future(self._close_sessions())
        outer_cancelled = False
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                outer_cancelled = True
        results = close_task.result()
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            teardown = [
                error
                for error in errors
                if isinstance(error, sandbox_shim.SandboxTeardownError)
            ]
            if teardown:
                self._teardown_failed = True
            self._close_error = _aggregate_close_failure(
                errors, teardown=bool(teardown)
            )
            raise self._close_error
        logger.info(
            "RapidOCR process pool closed",
            extra={
                "event": "rapidocr_pool_closed",
                "workers_configured": self.max_workers,
                "workers_started": len(self._sessions),
                "status": "ok",
            },
        )
        if outer_cancelled:
            raise asyncio.CancelledError


__all__ = [
    "RapidOCRProcessPool",
    "RapidOCRRowError",
    "RapidOCRSessionCancelled",
    "RapidOCRSessionFailure",
    "RapidOCRTopology",
    "rapidocr_topology",
]
