"""Offload blocking work without abandoning caller-owned resources on cancellation."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from functools import partial
from typing import Any, Callable, TypeVar

_WorkerResult = TypeVar("_WorkerResult")


async def await_thread_worker(
    worker: Callable[..., _WorkerResult], /, *args: Any, **kwargs: Any
) -> _WorkerResult:
    """Keep caller-owned inputs alive until a cancelled worker has finished."""

    future = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, partial(worker, *args, **kwargs)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if future.done():
            try:
                future.result()
            except BaseException:
                pass
        raise
