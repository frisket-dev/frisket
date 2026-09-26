from __future__ import annotations

import asyncio
import threading

import pytest

from frisket.server.thread_worker import await_thread_worker


def test_cancellation_callback_runs_before_worker_drain() -> None:
    async def scenario() -> None:
        started = threading.Event()
        stopped = threading.Event()
        saw_stop = threading.Event()

        def worker() -> None:
            started.set()
            if stopped.wait(2):
                saw_stop.set()

        task = asyncio.create_task(await_thread_worker(worker, on_cancel=stopped.set))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert saw_stop.is_set(), "cancellation callback ran after worker draining"

    asyncio.run(scenario())
