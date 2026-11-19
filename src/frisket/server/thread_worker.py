"""Offload blocking work without abandoning caller-owned resources on cancellation."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from functools import partial
from typing import Any, Callable, TypeVar

from frisket.server.unit_of_work import ends_unit_of_work

_WorkerResult = TypeVar("_WorkerResult")


def _unit_name(worker: Callable[..., object]) -> str:
    qualname = getattr(worker, "__qualname__", None)
    if qualname is None:
        return repr(worker)
    return f"{getattr(worker, '__module__', '?')}.{qualname}"


async def await_thread_worker(
    worker: Callable[..., _WorkerResult],
    /,
    *args: Any,
    on_cancel: Callable[[], None] | None = None,
    **kwargs: Any,
) -> _WorkerResult:
    """Keep caller-owned inputs alive until a cancelled worker has finished.

    The worker is one unit of work on its executor thread: a project
    transaction it leaves open when it returns or raises is rolled back
    there, before the thread serves another hand-off.

    ``on_cancel`` is deliberately tiny: a query owner can set its own event
    before this helper drains the worker, allowing SQLite's progress handler
    to interrupt only that query's connection.
    """

    future = asyncio.get_running_loop().run_in_executor(
        None,
        copy_context().run,
        ends_unit_of_work(partial(worker, *args, **kwargs), unit=_unit_name(worker)),
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
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
