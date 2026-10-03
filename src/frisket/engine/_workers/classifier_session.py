"""One run-owned, offline classifier process, shared across its rows and fields."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable

from frisket.contracts.classification import ClassifierError, LOCAL_CLASSIFIERS
from frisket.engine._workers.classifier_artifacts import (
    cached_classifier_path,
    classifier_runtime_present,
)
from frisket.engine._workers.local_engine_lease import (
    LocalEngineLeaseBusy,
    acquire_local_engine_lease,
)
from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache
from frisket.engine._workers.session_base import (
    encode_frame,
    never_cancel,
    reject_constant,
)
from frisket.engine.sandbox import fence, shim
from frisket.runtime.classifier_install import runtime_python
from frisket.runtime.launch import PythonRuntime, worker_argv


class ClassifierSession:
    def __init__(
        self, engine_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> None:
        self.spec = LOCAL_CLASSIFIERS[engine_id]
        self._cancelled = cancelled or never_cancel
        self._lock = asyncio.Lock()
        self._handle = None
        self._lease = None
        self._closed = False

    async def _exchange(self, request: dict) -> dict:
        payload = encode_frame(request)
        if len(payload) > 4 * 1024 * 1024:
            raise ClassifierError(
                "classify_input_too_long",
                "This input is too long for the local classifier. Use a shorter passage.",
            )
        assert self._handle is not None
        raw = await self._handle.exchange_frame(
            payload,
            wall_seconds=300,
            response_limit=8192,
            request_limit=4 * 1024 * 1024,
        )
        response = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(response, dict):
            raise ValueError("invalid classifier response")
        if response.get("type") == "error":
            raise ClassifierError(
                str(response.get("code", "classify_failed")),
                str(response.get("message", "The classifier failed.")),
            )
        return response

    async def _start(self) -> None:
        if self._handle is not None:
            return
        path = cached_classifier_path(self.spec.engine_id)
        if not classifier_runtime_present() or path is None:
            raise ClassifierError(
                "model_unavailable",
                "Download this classifier in the model picker before running it.",
            )
        try:
            # Share one lease: loading both CPU models concurrently is expensive.
            self._lease = acquire_local_engine_lease("classification")
        except LocalEngineLeaseBusy as exc:
            raise ClassifierError("engine_busy", str(exc)) from exc
        runtime = PythonRuntime(runtime_python(), PythonRuntime.current().app_code_root)
        environment = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "4",
            "OPENBLAS_NUM_THREADS": "4",
        }
        self._handle = await shim.open_sandboxed_process(
            worker_argv("classifier-session", runtime=runtime),
            policy=shim.SandboxPolicy(
                cpu_seconds=86400,
                memory_mb=16384,
                wall_seconds=300,
                trusted_python_netwall=True,
                allowed_extra_env=list(environment),
                confine=fence.Confinement(
                    op="local classification",
                    read=(
                        str(huggingface_hub_cache()),
                        "/proc/cpuinfo",
                        "/sys/devices/system/cpu",
                    ),
                ),
            ),
            extra_env=environment,
            should_cancel=self._cancelled,
        )
        response = await self._exchange(
            {"type": "init", "engine": self.spec.engine_id, "snapshot": str(path)}
        )
        if response != {"type": "ready"}:
            raise ValueError("classifier did not become ready")

    async def classify(
        self,
        text: str,
        labels: list[str],
        *,
        descriptions: dict[str, str],
        instruction: str,
    ) -> dict:
        if not text.strip():
            raise ClassifierError(
                "classify_input_empty", "There is no text to classify in this row."
            )
        async with self._lock:
            if self._closed:
                raise ClassifierError(
                    "classify_failed", "The classifier session is closed."
                )
            try:
                if self._cancelled():
                    raise asyncio.CancelledError
                await self._start()
                result = await self._exchange(
                    {
                        "type": "classify",
                        "text": text,
                        "labels": labels,
                        "descriptions": descriptions,
                        "instruction": instruction,
                    }
                )
                score = result.get("score")
                if (
                    result.get("type") != "result"
                    or result.get("label") not in labels
                    or type(score) not in (float, int)
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                    or result.get("model_revision") != self.spec.revision
                ):
                    raise ValueError("invalid classifier result")
                return {
                    key: result[key] for key in ("label", "score", "model_revision")
                }
            except ClassifierError as exc:
                if exc.code not in {
                    "classify_input_too_long",
                    "classify_invalid_input",
                    "classify_input_empty",
                }:
                    await self._close(abort=True)
                raise
            except (shim.SandboxProcessCancelledError, asyncio.CancelledError):
                await self._close(abort=True)
                raise asyncio.CancelledError from None
            except shim.SandboxTeardownError:
                if self._lease is not None:
                    self._lease.poison()
                self._closed = True
                raise
            except Exception as exc:
                await self._close(abort=True)
                raise ClassifierError(
                    "classify_failed",
                    "The local classifier stopped unexpectedly. Retry or check available memory.",
                ) from exc

    async def _close(self, *, abort: bool) -> None:
        self._closed = True
        try:
            if self._handle is not None:
                if abort:
                    await self._handle.abort()
                else:
                    await self._handle.close(
                        encode_frame({"type": "close"}), wall_seconds=10
                    )
        except shim.SandboxTeardownError:
            if self._lease is not None:
                self._lease.poison()
            raise
        finally:
            if self._lease is not None:
                self._lease.release()

    async def aclose(self) -> None:
        async with self._lock:
            if not self._closed:
                await self._close(abort=False)
