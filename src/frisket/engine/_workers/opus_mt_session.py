"""One invocation-owned offline translation worker, shared across rows."""

from __future__ import annotations

from collections.abc import Callable

from frisket.ai.models import model_cache
from frisket.engine._workers.opus_mt_worker import MAX_FRAME_BYTES
from frisket.engine._workers.session_base import FramedSession
from frisket.engine.sandbox import shim
from frisket.ops.integrations import opus_mt
from frisket.ops.integrations.translate_common import TranslateEngineError
from frisket.runtime.launch import worker_argv
from frisket.runtime.translation_install import runtime_for_worker


class OpusMtSession(FramedSession):
    _request_limit = MAX_FRAME_BYTES
    _response_limit = MAX_FRAME_BYTES
    _memory_mb = 8192
    _op = "local translation"
    _invalid_response = "invalid translation response"

    def __init__(self, *, cancelled: Callable[[], bool] | None = None) -> None:
        super().__init__(cancelled=cancelled)
        self._pair = None

    def _request_too_large(self) -> Exception:
        return TranslateEngineError(
            code="invalid_input", message="This translation input is too large."
        )

    def _closed_error(self) -> Exception:
        return TranslateEngineError(
            code="unavailable", message="The translation session is closed."
        )

    async def _failed(self, exc: Exception) -> Exception:
        if isinstance(exc, (opus_mt.OpusPairNotInstalled, TranslateEngineError)):
            # A missing pair can be provisioned by the host and retried.
            # Oversized frames never reach the child; later rows remain usable.
            return exc
        if isinstance(exc, shim.SandboxTeardownError):
            self._closed = True
            return exc
        await self._close(abort=True)
        return TranslateEngineError(
            code="unavailable",
            message="The local translation worker stopped unexpectedly. Retry or check available memory.",
        )

    async def translate(self, src: str, tgt: str, texts: list[str]) -> list[str]:
        return await self._serialized(lambda: self._translate(src, tgt, texts))

    async def _translate(self, src: str, tgt: str, texts: list[str]) -> list[str]:
        root = model_cache.default_cache_root().absolute()
        if not opus_mt.is_pair_installed(src, tgt, root):
            raise opus_mt.OpusPairNotInstalled(f"opus-mt:{src}-{tgt} is not installed")
        if self._handle is None:
            directory = opus_mt._pair_dir(src, tgt, root)
            reads = tuple(
                str(path.resolve(strict=True))
                for path in (
                    directory,
                    *(directory / name for name in opus_mt._CT2_REQUIRED_FILES),
                )
            )
            self._pair = (src, tgt)
            ready = await self._launch(
                worker_argv("opus-mt-session", runtime=runtime_for_worker()),
                {"type": "init", "directory": str(directory)},
                reads=reads,
                environment={},
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
