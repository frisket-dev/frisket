from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from frisket.ai.embeddings.runtime import EmbeddingRuntime
from frisket.retrieval.encoding import (
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
    TextEncodingInput,
)


def _unit(dimensions: int, index: int = 0) -> tuple[float, ...]:
    return tuple(1.0 if offset == index else 0.0 for offset in range(dimensions))


def test_passive_readiness_tracks_install_without_starting_runtime(monkeypatch):
    from frisket.engine._workers import embeddinggemma_artifacts

    ready = False
    monkeypatch.setattr(embeddinggemma_artifacts, "embeddinggemma_ready", lambda: ready)
    runtime = EmbeddingRuntime()
    assert not runtime.available()
    ready = True
    assert runtime.available()
    assert runtime._thread is None
    runtime.close()
    assert not runtime.available()


def test_runtime_reuses_one_session_and_owner_loop_across_sync_and_async_callers():
    instances = []

    class Session:
        def __init__(self, cancelled):
            self.cancelled = cancelled
            self.loops = []
            self.active = 0
            self.max_active = 0
            self.close_count = 0

        async def encode(self, inputs, *, purpose, space):
            self.loops.append((id(asyncio.get_running_loop()), threading.get_ident()))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return [_unit(space.dimensions) for _ in inputs]
            finally:
                self.active -= 1

        async def aclose(self):
            self.close_count += 1

    def factory(*, cancelled=None):
        session = Session(cancelled)
        instances.append(session)
        return session

    runtime = EmbeddingRuntime(factory)
    space = EmbeddingGemma2Space(128)
    cancel_event = threading.Event()
    try:
        assert runtime.encode(
            [TextEncodingInput("sync")],
            purpose=EncodingPurpose.QUERY,
            space=space,
            cancelled=cancel_event,
        ) == [_unit(128)]

        async def caller():
            return await asyncio.gather(
                runtime.encode_async(
                    [TextEncodingInput("async one")],
                    purpose=EncodingPurpose.DOCUMENT,
                    space=space,
                ),
                runtime.encode_async(
                    [TextEncodingInput("async two")],
                    purpose=EncodingPurpose.DOCUMENT,
                    space=space,
                ),
            )

        assert asyncio.run(caller()) == [[_unit(128)], [_unit(128)]]
    finally:
        runtime.close()
        runtime.close()

    assert len(instances) == 1
    assert len({loop for loop, _thread in instances[0].loops}) == 1
    assert len({thread for _loop, thread in instances[0].loops}) == 1
    assert instances[0].max_active == 1
    assert instances[0].close_count == 1


def test_runtime_is_lazy_and_closed_runtime_refuses_new_work():
    called = False

    def factory(*, cancelled=None):
        nonlocal called
        called = True
        raise AssertionError("empty request started session")

    runtime = EmbeddingRuntime(factory)
    assert runtime.encode(
        [], purpose=EncodingPurpose.QUERY, space=EmbeddingGemma2Space(128)
    ) == []
    assert called is False
    runtime.close()
    with pytest.raises(EncodingError) as caught:
        runtime.encode(
            [TextEncodingInput("late")],
            purpose=EncodingPurpose.QUERY,
            space=EmbeddingGemma2Space(128),
        )
    assert caught.value.code == "encoding_runtime_closed"


def test_runtime_retries_after_factory_startup_failure():
    attempts = 0

    class Session:
        async def encode(self, inputs, *, purpose, space):
            return [_unit(space.dimensions) for _ in inputs]

        async def aclose(self):
            return None

    def factory(*, cancelled=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EncodingError("model_unavailable", "model is not installed")
        return Session()

    runtime = EmbeddingRuntime(factory)
    space = EmbeddingGemma2Space(128)
    try:
        with pytest.raises(EncodingError) as caught:
            runtime.encode(
                [TextEncodingInput("first")],
                purpose=EncodingPurpose.QUERY,
                space=space,
            )
        assert caught.value.code == "model_unavailable"
        assert runtime.encode(
            [TextEncodingInput("second")],
            purpose=EncodingPurpose.QUERY,
            space=space,
        ) == [_unit(128)]
    finally:
        runtime.close()

    assert attempts == 2


def test_runtime_closes_failed_session_before_replacing_it():
    sessions = []

    class Session:
        def __init__(self, *, fail):
            self.fail = fail
            self.close_count = 0

        async def encode(self, inputs, *, purpose, space):
            if self.fail:
                raise EncodingError("model_unavailable", "child exited")
            return [_unit(space.dimensions) for _ in inputs]

        async def aclose(self):
            self.close_count += 1

    def factory(*, cancelled=None):
        session = Session(fail=not sessions)
        sessions.append(session)
        return session

    runtime = EmbeddingRuntime(factory)
    space = EmbeddingGemma2Space(128)
    try:
        with pytest.raises(EncodingError) as caught:
            runtime.encode(
                [TextEncodingInput("first")],
                purpose=EncodingPurpose.QUERY,
                space=space,
            )
        assert caught.value.code == "model_unavailable"
        assert sessions[0].close_count == 1
        assert runtime.encode(
            [TextEncodingInput("second")],
            purpose=EncodingPurpose.QUERY,
            space=space,
        ) == [_unit(128)]
    finally:
        runtime.close()

    assert len(sessions) == 2
    assert [session.close_count for session in sessions] == [1, 1]


def _wait_for_pending(runtime: EmbeddingRuntime, count: int) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with runtime._state_lock:  # noqa: SLF001 - lifecycle boundary test
            if len(runtime._pending) == count:  # noqa: SLF001
                return
        time.sleep(0.001)
    pytest.fail(f"runtime did not register {count} pending requests")


def test_close_cancels_active_and_queued_sync_callers_before_stopping_loop():
    started = threading.Event()
    sessions = []

    class Session:
        def __init__(self):
            self.close_count = 0

        async def encode(self, inputs, *, purpose, space):
            started.set()
            await asyncio.sleep(60)

        async def aclose(self):
            self.close_count += 1

    def factory(*, cancelled=None):
        session = Session()
        sessions.append(session)
        return session

    runtime = EmbeddingRuntime(factory)
    outcomes = []

    def call(value):
        try:
            runtime.encode(
                [TextEncodingInput(value)],
                purpose=EncodingPurpose.QUERY,
                space=EmbeddingGemma2Space(128),
            )
        except BaseException as exc:
            outcomes.append(exc)

    callers = [
        threading.Thread(target=call, args=(value,), daemon=True)
        for value in ("active", "queued")
    ]
    for caller in callers:
        caller.start()
    assert started.wait(1)
    _wait_for_pending(runtime, 2)

    runtime.close(timeout=1)
    for caller in callers:
        caller.join(1)

    assert all(not caller.is_alive() for caller in callers)
    assert len(outcomes) == 2
    assert all(isinstance(error, concurrent.futures.CancelledError) for error in outcomes)
    assert sessions[0].close_count == 1


def test_aclose_cancels_active_and_queued_async_callers_before_stopping_loop():
    started = threading.Event()
    sessions = []

    class Session:
        def __init__(self):
            self.close_count = 0

        async def encode(self, inputs, *, purpose, space):
            started.set()
            await asyncio.sleep(60)

        async def aclose(self):
            self.close_count += 1

    def factory(*, cancelled=None):
        session = Session()
        sessions.append(session)
        return session

    async def scenario():
        runtime = EmbeddingRuntime(factory)
        callers = [
            asyncio.create_task(
                runtime.encode_async(
                    [TextEncodingInput(value)],
                    purpose=EncodingPurpose.QUERY,
                    space=EmbeddingGemma2Space(128),
                )
            )
            for value in ("active", "queued")
        ]
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.to_thread(_wait_for_pending, runtime, 2)
        await runtime.aclose(timeout=1)
        return await asyncio.gather(*callers, return_exceptions=True)

    outcomes = asyncio.run(scenario())

    assert len(outcomes) == 2
    assert all(isinstance(error, asyncio.CancelledError) for error in outcomes)
    assert sessions[0].close_count == 1
