"""One lazy, offline spaCy child owned by an action, shared across its rows."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from frisket.engine._workers.session_base import (
    encode_frame,
    never_cancel,
    reject_constant,
)
from frisket.engine.sandbox import fence, shim
from frisket.runtime.launch import PythonRuntime, worker_argv
from frisket.runtime.spacy_install import is_installed, runtime_python

_FRAME_LIMIT = 4 * 1024 * 1024


class SpacySession:
    def __init__(self, *, cancelled: Callable[[], bool] | None = None) -> None:
        self._cancelled = cancelled or never_cancel
        self._lock = asyncio.Lock()
        self._handle = None
        self._closed = False

    async def _exchange(self, request: dict) -> dict:
        payload = encode_frame(request)
        if len(payload) > _FRAME_LIMIT:
            raise ValueError("This input is too long for local entity extraction.")
        assert self._handle is not None
        raw = await self._handle.exchange_frame(
            payload,
            wall_seconds=300,
            response_limit=_FRAME_LIMIT,
            request_limit=_FRAME_LIMIT,
        )
        response = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(response, dict):
            raise ValueError("invalid spaCy response")
        if response.get("type") == "error":
            raise RuntimeError(
                "The local spaCy worker could not process this row. Retry setup or check available memory."
            )
        return response

    async def _start(self) -> None:
        if self._handle is not None:
            return
        from frisket.ai.models.spacy_model import (
            ensure_loadable_model_path,
            model_state,
        )

        from frisket.ops.spacy_ner import DEFAULT_SPACY_MODEL, SPACY_MODEL

        if SPACY_MODEL != DEFAULT_SPACY_MODEL:
            raise RuntimeError(
                "Custom spaCy models require a host-installed spaCy runtime."
            )
        if (
            not is_installed()
            or model_state(include_python_package=False).status != "present"
        ):
            raise RuntimeError("Set up spaCy in the engine picker before running it.")
        path = ensure_loadable_model_path()
        runtime = PythonRuntime(runtime_python(), PythonRuntime.current().app_code_root)
        environment = {"OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"}
        self._handle = await shim.open_sandboxed_process(
            worker_argv("spacy-session", runtime=runtime),
            policy=shim.SandboxPolicy(
                cpu_seconds=86400,
                memory_mb=4096,
                wall_seconds=300,
                trusted_python_netwall=True,
                allowed_extra_env=list(environment),
                confine=fence.Confinement(
                    op="local entity extraction",
                    read=(str(path), "/proc/cpuinfo", "/sys/devices/system/cpu"),
                ),
            ),
            extra_env=environment,
            should_cancel=self._cancelled,
        )
        if await self._exchange({"type": "init", "model_path": str(path)}) != {
            "type": "ready"
        }:
            raise ValueError("spaCy did not become ready")

    async def extract(self, text: str, labels: list[str]) -> list[dict]:
        if not labels or any(
            not isinstance(label, str) or not label.strip() for label in labels
        ):
            raise ValueError("NER requires nonblank entity labels")
        async with self._lock:
            if self._closed:
                raise RuntimeError("The spaCy session is closed.")
            try:
                if self._cancelled():
                    raise asyncio.CancelledError
                if not text.strip():
                    return []
                await self._start()
                from frisket.ops.spacy_ner import requested_spacy_types

                wanted = requested_spacy_types(labels)
                result = await self._exchange(
                    {"type": "extract", "text": text, "labels": sorted(wanted or ())}
                )
                entities = result.get("entities")
                if result.get("type") != "result" or not isinstance(entities, list):
                    raise ValueError("invalid spaCy result")
                from frisket.engine._workers.spacy_entities import SPACY_CANONICAL_TYPES

                for entity in entities:
                    if not isinstance(entity, dict) or set(entity) != {
                        "text",
                        "type",
                        "start",
                        "end",
                        "score",
                    }:
                        raise ValueError("invalid spaCy entity")
                    start, end = entity["start"], entity["end"]
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or not 0 <= start < end <= len(text)
                        or entity["text"] != text[start:end]
                        or entity["type"] not in SPACY_CANONICAL_TYPES
                        or (wanted is not None and entity["type"] not in wanted)
                        or entity["score"] is not None
                    ):
                        raise ValueError("invalid spaCy entity")
                from frisket.ops.entities import canonicalize_entities

                return canonicalize_entities(entities)
            except (shim.SandboxProcessCancelledError, asyncio.CancelledError):
                await self._close(abort=True)
                raise asyncio.CancelledError from None
            except Exception:
                await self._close(abort=True)
                raise

    async def _close(self, *, abort: bool) -> None:
        self._closed = True
        if self._handle is not None:
            if abort:
                await self._handle.abort()
            else:
                await self._handle.close(
                    encode_frame({"type": "close"}), wall_seconds=10
                )

    async def aclose(self) -> None:
        async with self._lock:
            if not self._closed:
                await self._close(abort=False)
