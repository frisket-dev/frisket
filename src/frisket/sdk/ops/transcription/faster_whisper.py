"""Local Faster Whisper adapter and its sandbox boundary."""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable
from typing import Any

from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.transcription_sidecar import (
    LocalFasterWhisperRequest,
    TRANSCRIPTION_CONTRACT_VERSION,
)
from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.runtime.launch import worker_argv

from .common import (
    TranscribeCancelled,
    input_units,
    transcription_v1_options,
    transcription_v1_result,
    v1_provenance_units,
)


def faster_whisper_policy() -> SandboxPolicy:
    return SandboxPolicy(
        cpu_seconds=3600,
        wall_seconds=7200,
        memory_mb=4096,
        env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
        allowed_extra_env=["HF_HUB_CACHE"],
    )


def faster_whisper_env() -> dict[str, str]:
    """Resolve the persistent Hub cache before sandbox HOME is replaced."""
    return {"HF_HUB_CACHE": str(huggingface_hub_cache())}


_NOISY_WORKER_STDERR_FRAGMENTS = (
    "[W:onnxruntime:",
    "coreml_execution_provider.cc",
    "CoreMLExecutionProvider::GetCapability",
    "Context leak detected, msgtracer returned",
)


def _clean_worker_stderr(stderr: Any | None, *, limit: int = 500) -> str:
    if stderr is None:
        text = ""
    elif isinstance(stderr, bytes):
        text = stderr.decode(errors="replace")
    else:
        text = str(stderr)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if (
            not stripped
            or ("msgtracer" in lowered and "context leak detected" in lowered)
            or ("onnxruntime" in lowered and "coreml" in lowered)
            or any(fragment in stripped for fragment in _NOISY_WORKER_STDERR_FRAGMENTS)
        ):
            continue
        lines.append(stripped)
    joined = "\n".join(lines)
    if len(joined) <= limit:
        return joined
    kept: list[str] = []
    length = 0
    for line in reversed(lines):
        extra = len(line) + (1 if kept else 0)
        if kept and length + extra > limit:
            break
        kept.append(line)
        length += extra
        if length >= limit:
            break
    if not kept:
        return "...(truncated)\n" + joined[-limit:]
    kept.reverse()
    return "...(truncated)\n" + "\n".join(kept)


def _worker_failure_message(result: Any) -> str:
    detail = _clean_worker_stderr(result.stderr)
    if getattr(result, "timed_out", False):
        message = "worker timed out before transcription completed"
    elif result.returncode < 0:
        signum = -int(result.returncode)
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = "UNKNOWN"
        message = f"worker terminated by signal {signum} ({sig_name})"
        if sig_name in {"SIGKILL", "SIGTERM"}:
            message += "; no Python traceback was produced"
    else:
        message = f"worker exited with code {result.returncode}"
    return f"{message}: {detail}" if detail else message


class FasterWhisperAdapter:
    async def transcribe(
        self, path: str, spec: dict, *, should_cancel: Callable[[], bool] | None = None
    ) -> dict:
        path = os.path.abspath(path)
        options = transcription_v1_options("faster_whisper", spec)
        request = LocalFasterWhisperRequest(
            contract_version=TRANSCRIPTION_CONTRACT_VERSION,
            engine="faster-whisper",
            audio_path=path,
            options=options,
        )
        sandbox_result = await run_sandboxed(
            worker_argv("faster-whisper"),
            policy=faster_whisper_policy(),
            extra_env=faster_whisper_env(),
            stdin_data=request.model_dump_json(exclude_none=True).encode(),
            should_cancel=should_cancel,
        )
        if getattr(sandbox_result, "cancelled", False):
            raise TranscribeCancelled("transcription cancelled")
        if not sandbox_result.ok:
            raise RuntimeError(
                f"transcription failed: {_worker_failure_message(sandbox_result)}"
            )
        try:
            body = json.loads(sandbox_result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise RuntimeError(
                "malformed local transcription v1 response: invalid JSON"
            ) from None
        return transcription_v1_result(
            body,
            wire_engine=request.engine,
            requested_options=options,
            boundary="local",
        )

    def model_calls(
        self, engine: str, path: str, spec: dict[str, Any], out: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            ModelCallMeta.local(
                capability="transcribe",
                engine=engine,
                model_ids=list(out.get("model_ids") or []),
                units=v1_provenance_units(out, input_units(path, out)),
                warnings=[str(item) for item in (out.get("warnings") or [])],
            ).as_dict()
        ]
