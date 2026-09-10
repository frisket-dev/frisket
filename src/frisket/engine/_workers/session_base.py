"""Shared parent-side machinery for run-scoped sandboxed framed-IPC sessions.

These helpers are the parts that are *genuinely identical* between the local
engine session managers (RapidOCR, Parakeet, and the GPU transcription session
that is expected next): JSON frame encoding with the non-finite-number guard,
monotonic duration measurement, the bounded ``should_cancel`` poll hook, and
Linux peak-memory (``VmHWM``/``VmPeak``) sampling.

Engine-specific wire validation (frame decoders, exact-field/schema checks,
``ready``/``ack``/result validators) and each session's abort/close state
machine deliberately stay in their own module: those differ in error type,
diagnostic wording, schema version, and cleanup steps (e.g. RapidOCR's staging
root), and unifying them would change behavior rather than remove duplication.

The module must stay dependency-light: importing it must not pull in ONNX
Runtime, NumPy, OpenCV, Pillow, or any model library.
"""

from __future__ import annotations

import json
import math
import sys
import time
from typing import Any


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


def encode_frame(frame: dict[str, Any]) -> bytes:
    """Encode a protocol frame as compact UTF-8 JSON, rejecting non-finite."""

    return json.dumps(
        frame, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def reject_constant(value: str) -> None:
    """``json.loads`` ``parse_constant`` hook that bars ``NaN``/``Infinity``."""

    raise ValueError(f"non-finite JSON number is not allowed: {value}")


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
