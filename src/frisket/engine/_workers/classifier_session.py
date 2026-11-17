"""One run-owned, offline classifier process, shared across its rows and fields."""

from __future__ import annotations

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
from frisket.engine._workers.session_base import FramedSession
from frisket.engine.sandbox import shim
from frisket.runtime.classifier_install import runtime_python
from frisket.runtime.launch import PythonRuntime, worker_argv


_INPUT_ERRORS = frozenset(
    {"classify_input_too_long", "classify_invalid_input", "classify_input_empty"}
)


class ClassifierSession(FramedSession):
    _request_limit = 4 * 1024 * 1024
    _response_limit = 8192
    _memory_mb = 16384
    _op = "local classification"
    _invalid_response = "invalid classifier response"

    def __init__(
        self, engine_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> None:
        self.spec = LOCAL_CLASSIFIERS[engine_id]
        super().__init__(cancelled=cancelled)
        self._lease = None

    def _request_too_large(self) -> Exception:
        return ClassifierError(
            "classify_input_too_long",
            "This input is too long for the local classifier. Use a shorter passage.",
        )

    def _closed_error(self) -> Exception:
        return ClassifierError("classify_failed", "The classifier session is closed.")

    def _worker_error(self, frame: dict) -> Exception:
        return ClassifierError(
            str(frame.get("code", "classify_failed")),
            str(frame.get("message", "The classifier failed.")),
        )

    async def _failed(self, exc: Exception) -> Exception:
        if isinstance(exc, ClassifierError):
            if exc.code not in _INPUT_ERRORS:
                await self._close(abort=True)
            return exc
        if isinstance(exc, shim.SandboxTeardownError):
            if self._lease is not None:
                self._lease.poison()
            self._closed = True
            return exc
        await self._close(abort=True)
        return ClassifierError(
            "classify_failed",
            "The local classifier stopped unexpectedly. Retry or check available memory.",
        )

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
        response = await self._launch(
            worker_argv("classifier-session", runtime=runtime),
            {"type": "init", "engine": self.spec.engine_id, "snapshot": str(path)},
            reads=(str(huggingface_hub_cache()),),
            environment={
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "TOKENIZERS_PARALLELISM": "false",
                "OMP_NUM_THREADS": "4",
                "OPENBLAS_NUM_THREADS": "4",
            },
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
        return await self._serialized(
            lambda: self._classify(text, labels, descriptions, instruction)
        )

    async def _classify(
        self,
        text: str,
        labels: list[str],
        descriptions: dict[str, str],
        instruction: str,
    ) -> dict:
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
        return {key: result[key] for key in ("label", "score", "model_revision")}

    async def _close(self, *, abort: bool) -> None:
        try:
            await super()._close(abort=abort)
        except shim.SandboxTeardownError:
            if self._lease is not None:
                self._lease.poison()
            raise
        finally:
            if self._lease is not None:
                self._lease.release()
