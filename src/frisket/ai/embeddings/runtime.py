"""One warm, loop-owned runtime for the optional EmbeddingGemma encoder.

The sandbox process and its asyncio transports must stay on the event loop that
created them.  Frisket callers do not: HTTP search runs in worker threads while
queued actions each use their own short-lived loop.  This class owns one small,
embedding-specific loop thread and exposes only typed encoding requests.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from frisket.retrieval.encoding import (
    EmbeddingGemma2Encoder,
    EmbeddingGemma2SessionProtocol,
    EmbeddingGemma2Space,
    EmbeddingVector,
    EncodingError,
    EncodingInput,
    EncodingPurpose,
)


class CancelEvent(Protocol):
    def is_set(self) -> bool: ...


Cancellation = Callable[[], bool] | CancelEvent
CancelCheck = Callable[[], bool]


def _is_cancelled(value: Cancellation | None) -> bool:
    if value is None:
        return False
    return bool(value() if callable(value) else value.is_set())


class SessionFactory(Protocol):
    def __call__(
        self, *, cancelled: CancelCheck | None = None
    ) -> EmbeddingGemma2SessionProtocol: ...


def _installed_session(
    *, cancelled: CancelCheck | None = None
) -> EmbeddingGemma2SessionProtocol:
    from frisket.engine._workers.embeddinggemma_session import EmbeddingGemma2Session

    return EmbeddingGemma2Session.from_installed(cancelled=cancelled)


class EmbeddingRuntime:
    """Own one resident EmbeddingGemma session on one private event loop.

    The thread starts lazily on the first non-empty request.  Requests from all
    caller threads and event loops are serialized so the active cancellation
    callback always belongs to the batch currently using the child process.
    """

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory or _installed_session
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._encoder: EmbeddingGemma2Encoder | None = None
        self._request_lock: asyncio.Lock | None = None
        self._active_cancelled: Cancellation | None = None
        self._startup_error: BaseException | None = None
        self._pending: set[
            concurrent.futures.Future[list[EmbeddingVector]]
        ] = set()
        self._closed = False

    def _cancelled(self) -> bool:
        callback = self._active_cancelled
        return _is_cancelled(callback)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            session = self._session_factory(cancelled=self._cancelled)
            self._encoder = EmbeddingGemma2Encoder(session)
            self._request_lock = asyncio.Lock()
            self._loop = loop
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        while True:
            with self._state_lock:
                if self._closed:
                    raise EncodingError(
                        "encoding_runtime_closed",
                        "The local embedding runtime is closed.",
                    )
                if self._thread is None:
                    self._ready = threading.Event()
                    self._startup_error = None
                    self._thread = threading.Thread(
                        target=self._run_loop,
                        name="frisket-embedding-runtime",
                        daemon=False,
                    )
                    self._thread.start()
                thread = self._thread
                ready = self._ready
            ready.wait()
            with self._state_lock:
                if self._thread is not thread:
                    continue
                error = self._startup_error
                loop = self._loop
                if error is not None:
                    self._thread = None
                    self._loop = None
                    self._request_lock = None
                    self._startup_error = None
            if error is not None:
                thread.join()
                if isinstance(error, EncodingError):
                    raise error
                raise EncodingError(
                    "model_unavailable", "The local embedding runtime could not start."
                ) from error
            if loop is None:
                raise RuntimeError("embedding runtime started without an event loop")
            return loop

    async def _discard_encoder(self) -> None:
        encoder = self._encoder
        self._encoder = None
        if encoder is None:
            return
        try:
            await encoder.aclose()
        except BaseException:
            pass

    async def _encode_on_owner_loop(
        self,
        inputs: tuple[EncodingInput, ...],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
        cancelled: Cancellation | None,
    ) -> list[EmbeddingVector]:
        if self._request_lock is None:
            raise RuntimeError("embedding runtime is not initialized")
        async with self._request_lock:
            self._active_cancelled = cancelled
            try:
                if self._encoder is None:
                    self._encoder = EmbeddingGemma2Encoder(
                        self._session_factory(cancelled=self._cancelled)
                    )
                if _is_cancelled(cancelled):
                    raise asyncio.CancelledError
                return await self._encoder.encode(inputs, purpose=purpose, space=space)
            except EncodingError as exc:
                if exc.code in {
                    "invalid_input",
                    "encoding_input_too_large",
                    "invalid_encoder_result",
                }:
                    raise
                await self._discard_encoder()
                raise
            except BaseException:
                await self._discard_encoder()
                raise
            finally:
                self._active_cancelled = None

    def _submit(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
        cancelled: Cancellation | None,
    ) -> concurrent.futures.Future[list[EmbeddingVector]]:
        values = tuple(inputs)
        if not values:
            future: concurrent.futures.Future[list[EmbeddingVector]] = (
                concurrent.futures.Future()
            )
            future.set_result([])
            return future
        loop = self._ensure_started()
        with self._state_lock:
            if self._closed:
                raise EncodingError(
                    "encoding_runtime_closed", "The local embedding runtime is closed."
                )
            future = asyncio.run_coroutine_threadsafe(
                self._encode_on_owner_loop(
                    values, purpose=purpose, space=space, cancelled=cancelled
                ),
                loop,
            )
            self._pending.add(future)
        future.add_done_callback(self._forget_request)
        return future

    def _forget_request(
        self, future: concurrent.futures.Future[list[EmbeddingVector]]
    ) -> None:
        with self._state_lock:
            self._pending.discard(future)

    def encode(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
        cancelled: Cancellation | None = None,
    ) -> list[EmbeddingVector]:
        """Encode from a synchronous caller while retaining the warm child."""

        return self._submit(
            inputs, purpose=purpose, space=space, cancelled=cancelled
        ).result()

    async def encode_async(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
        cancelled: Cancellation | None = None,
    ) -> list[EmbeddingVector]:
        """Encode from any caller loop through the runtime's owning loop."""

        return await asyncio.wrap_future(
            self._submit(inputs, purpose=purpose, space=space, cancelled=cancelled)
        )

    async def _close_on_owner_loop(self) -> None:
        current = asyncio.current_task()
        requests = [
            task for task in asyncio.all_tasks() if task is not current and not task.done()
        ]
        for task in requests:
            if not task.cancelling():
                task.cancel()
        if requests:
            await asyncio.gather(*requests, return_exceptions=True)
        if self._request_lock is None:
            return
        async with self._request_lock:
            encoder = self._encoder
            self._encoder = None
            if encoder is not None:
                await encoder.aclose()

    def close(self, *, timeout: float = 30.0) -> None:
        """Close the child and join the owner thread.  Safe to call repeatedly."""

        if timeout <= 0:
            raise ValueError("embedding runtime close timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            loop = self._loop
            pending = tuple(self._pending)
        if thread is None:
            return
        if loop is None:
            self._ready.wait(max(0.0, deadline - time.monotonic()))
            loop = self._loop
        for request in pending:
            request.cancel()
        try:
            if loop is not None and not loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(
                    self._close_on_owner_loop(), loop
                )
                future.result(timeout=max(0.0, deadline - time.monotonic()))
        finally:
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
            thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            raise TimeoutError("local embedding runtime did not stop")

    async def aclose(self, *, timeout: float = 30.0) -> None:
        """Close without blocking a caller event loop."""

        await asyncio.to_thread(self.close, timeout=timeout)


__all__ = ["Cancellation", "EmbeddingRuntime", "SessionFactory"]
