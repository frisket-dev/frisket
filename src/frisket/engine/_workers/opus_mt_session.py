"""One invocation-owned offline translation worker, shared across rows."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from frisket.engine._workers.session_base import (
    encode_frame,
    never_cancel,
    reject_constant,
)
from frisket.engine._workers.opus_mt_worker import MAX_FRAME_BYTES
from frisket.engine.sandbox import fence, shim
from frisket.ops.integrations.translate_common import TranslateEngineError
from frisket.runtime.launch import worker_argv
from frisket.runtime.translation_install import runtime_for_worker


class OpusMtSession:
    def __init__(self, *, cancelled: Callable[[], bool] | None = None) -> None:
        self._cancelled = cancelled or never_cancel
        self._lock = asyncio.Lock()
        self._handle = None
        self._pair = None
        self._closed = False

    async def _exchange(self, request: dict) -> dict:
        payload = encode_frame(request)
        if len(payload) > MAX_FRAME_BYTES:
            raise TranslateEngineError(
                code="invalid_input", message="This translation input is too large."
            )
        raw = await self._handle.exchange_frame(
            payload,
            wall_seconds=300,
            response_limit=MAX_FRAME_BYTES,
            request_limit=MAX_FRAME_BYTES,
        )
        response = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(response, dict):
            raise ValueError("invalid translation response")
        return response

    async def translate(self, src: str, tgt: str, texts: list[str]) -> list[str]:
        from frisket.ai.models import model_cache
        from frisket.ops.integrations import opus_mt

        async with self._lock:
            if self._closed:
                raise TranslateEngineError(
                    code="unavailable", message="The translation session is closed."
                )
            try:
                if self._cancelled():
                    raise asyncio.CancelledError
                root = model_cache.default_cache_root().absolute()
                if not opus_mt.is_pair_installed(src, tgt, root):
                    raise opus_mt.OpusPairNotInstalled(
                        f"opus-mt:{src}-{tgt} is not installed"
                    )
                if self._handle is None:
                    directory = opus_mt._pair_dir(src, tgt, root)
                    reads = tuple(
                        str(path.resolve(strict=True))
                        for path in (
                            directory,
                            *(directory / name for name in opus_mt._CT2_REQUIRED_FILES),
                        )
                    )
                    self._handle = await shim.open_sandboxed_process(
                        worker_argv("opus-mt-session", runtime=runtime_for_worker()),
                        policy=shim.SandboxPolicy(
                            cpu_seconds=86400,
                            memory_mb=8192,
                            wall_seconds=300,
                            trusted_python_netwall=True,
                            confine=fence.Confinement(
                                op="local translation",
                                read=(
                                    *reads,
                                    "/proc/cpuinfo",
                                    "/sys/devices/system/cpu",
                                ),
                            ),
                        ),
                        should_cancel=self._cancelled,
                    )
                    self._pair = (src, tgt)
                    ready = await self._exchange(
                        {"type": "init", "directory": str(directory)}
                    )
                    if ready != {"type": "ready"}:
                        raise ValueError("translation worker did not become ready")
                if self._pair != (src, tgt):
                    raise ValueError("translation invocation changed language pair")
                response = await self._exchange({"type": "translate", "texts": texts})
                results = response.get("texts")
                if (
                    set(response) != {"type", "texts"}
                    or response["type"] != "result"
                    or not isinstance(results, list)
                    or len(results) != len(texts)
                    or not all(isinstance(text, str) for text in results)
                ):
                    raise ValueError("invalid translation result")
                return results
            except (opus_mt.OpusPairNotInstalled, TranslateEngineError):
                # A missing pair can be provisioned by the host and retried.
                # Oversized frames never reach the child; later rows remain usable.
                raise
            except (shim.SandboxProcessCancelledError, asyncio.CancelledError):
                await self._close(abort=True)
                raise asyncio.CancelledError from None
            except shim.SandboxTeardownError:
                self._closed = True
                raise
            except Exception as exc:
                await self._close(abort=True)
                raise TranslateEngineError(
                    code="unavailable",
                    message="The local translation worker stopped unexpectedly. Retry or check available memory.",
                ) from exc

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
