"""Owner-thread async production behind the table host's existing pull iterator."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import ExitStack
from time import monotonic

from frisket.actions.types import TableError


class TableProducer:
    """One loop and resource lifetime, shared by preparation and row consumption."""

    def __init__(self, *, cancelled=None):
        self.cancelled = cancelled
        self.runner = None
        self.worker = None
        self.pending = None
        self.resources = ExitStack()
        self.owned = set()
        self._last_cancel_check = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            owner = asyncio.current_task()
        except RuntimeError:
            owner = None
        # Runner.close() cancels an idle worker itself. Read actual cancellation
        # requests before that cleanup, then settle only after resources join.
        task_cancelled = bool(
            (owner is not None and owner.cancelling())
            or (self.worker is not None and self.worker.cancelling())
        )
        self.close()
        if (
            isinstance(exc, asyncio.CancelledError)
            and not task_cancelled
            and self.cancelled is not None
            and self.cancelled()
        ):
            raise TableError(
                "action_cancelled", "Table production was cancelled."
            ) from None
        return False

    def check_cancelled(self, *, poll=False):
        if self.cancelled is None:
            return
        now = monotonic()
        if (
            poll
            and self._last_cancel_check is not None
            and now - self._last_cancel_check < 0.05
        ):
            return
        self._last_cancel_check = now
        if self.cancelled():
            raise TableError("action_cancelled", "Table production was cancelled.")

    async def _produce(self):
        # A generator's context managers must enter, resume and exit in the
        # same task/context, including aclose after a bounded preview.
        while True:
            awaitable, started, result = await self.pending.get()
            started.set_result(None)
            try:
                value = await awaitable
            except BaseException as exc:
                if not result.done():
                    result.set_exception(exc)
            else:
                if not result.done():
                    result.set_result(value)

    async def _wait(self, awaitable, *, cancellable):
        if self.worker is None or self.worker.done():
            self.pending = asyncio.Queue()
            self.worker = asyncio.create_task(self._produce())
        result = asyncio.get_running_loop().create_future()
        started = asyncio.get_running_loop().create_future()
        self.pending.put_nowait((awaitable, started, result))
        try:
            while not result.done():
                if cancellable:
                    self.check_cancelled(poll=True)
                await asyncio.wait((result,), timeout=0.05)
            # Deliver completed values so the host can own returned resources
            # before observing a cancellation racing with completion.
            return result.result()
        except BaseException:
            if not result.done():
                # Cancel the operation, not its completion channel. The worker
                # stays alive for same-context aclose even when cancellation is
                # suppressed or a generator's finally raises another exception.
                await asyncio.shield(started)
                if not result.done():
                    self.worker.cancel()
            try:
                value = await asyncio.shield(result)
            except asyncio.CancelledError:
                pass
            else:
                # A cancellation-suppressing handler may still return an owned
                # stream. Close it before propagating the original stop.
                self.own_rows(getattr(value, "rows", None))
            raise

    def resolve(self, value, *, cancellable=True, poll_cancelled=False):
        if not inspect.isawaitable(value):
            return value
        if cancellable:
            try:
                self.check_cancelled(poll=poll_cancelled)
            except BaseException:
                if inspect.iscoroutine(value):
                    value.close()
                raise
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if inspect.iscoroutine(value):
                value.close()
            raise TableError(
                "async_table_requires_worker",
                "Async table production requires its invocation's synchronous worker",
            )
        if self.runner is None:
            self.runner = asyncio.Runner()
        return self.runner.run(self._wait(value, cancellable=cancellable))

    def own_rows(self, rows):
        if id(rows) in self.owned:
            return
        self.owned.add(id(rows))
        close = getattr(rows, "aclose", None) or getattr(rows, "close", None)
        if callable(close):
            self.resources.callback(lambda: self.resolve(close(), cancellable=False))

    def rows(self, rows):
        self.check_cancelled()
        if hasattr(rows, "__aiter__"):
            iterator = aiter(rows)
            self.own_rows(iterator)
            while True:
                self.check_cancelled(poll=True)
                try:
                    row = self.resolve(anext(iterator), poll_cancelled=True)
                except StopAsyncIteration:
                    self.check_cancelled()
                    return
                self.check_cancelled(poll=True)
                yield row
        else:
            iterator = iter(rows)
            self.own_rows(iterator)
            for row in iterator:
                self.check_cancelled(poll=True)
                yield row
            # Even a whole stream shorter than the polling interval must not
            # publish using a cached pre-production cancellation observation.
            self.check_cancelled()

    def close(self):
        try:
            self.resources.close()
        finally:
            if self.runner is not None:
                self.runner.close()
