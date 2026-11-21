"""Drive renderer-only awaitables beneath synchronous media capabilities."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

from frisket.engine.sandbox.shim import SandboxTeardownError

T = TypeVar("T")


def run_media_sync(
    operation: Callable[[Callable[[], bool] | None], Awaitable[T]],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> T:
    """Keep project work/cancellation checks on the caller; join owned renderers.

    Only renderer data (paths, policies and captured projection facts) may enter
    ``operation``. Its sandbox retains sole ownership of timeout/tree teardown.
    """

    async def _run_operation(cancel_check: Callable[[], bool] | None) -> T:
        return await operation(cancel_check)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_operation(cancelled))

    stop, done = threading.Event(), threading.Event()
    results, errors = [], []

    def render():
        try:
            results.append(asyncio.run(_run_operation(stop.is_set)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=render)
    worker.start()
    try:
        while not done.wait(0.05):
            if cancelled is not None and cancelled():
                stop.set()
    finally:
        stop.set()
        worker.join()
        if errors and isinstance(errors[0], SandboxTeardownError):
            raise errors[0]
    if errors:
        raise errors[0]
    return results[0]
