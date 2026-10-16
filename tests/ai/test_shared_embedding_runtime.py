from __future__ import annotations

import asyncio
import threading

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
