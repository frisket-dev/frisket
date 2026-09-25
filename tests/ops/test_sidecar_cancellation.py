"""Cancellation stops new HTTP work, not the shared model-server process."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from frisket.execution.provider import ConnectionConfig
from frisket.ops._sidecar import sidecar_post


@pytest.mark.parametrize("status", [200, 429])
def test_cancel_after_response_discards_result_and_does_not_retry(status):
    cancelled = False
    requests = []

    async def post(url, **kwargs):
        nonlocal cancelled
        requests.append(url)
        cancelled = True
        return httpx.Response(status, json={"documents": [{"markdown": "ignored"}]})

    async def run():
        await sidecar_post(
            SimpleNamespace(http=SimpleNamespace(post=post)),
            "/to-markdown",
            connection=ConnectionConfig(base_url="http://127.0.0.1:1234", token="test"),
            should_cancel=lambda: cancelled,
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert len(requests) == 1


def test_cancel_before_request_does_not_submit():
    async def post(*args, **kwargs):
        pytest.fail("cancelled document must not be submitted")

    async def run():
        await sidecar_post(
            SimpleNamespace(http=SimpleNamespace(post=post)),
            "/to-markdown",
            connection=ConnectionConfig(base_url="http://127.0.0.1:1234", token="test"),
            should_cancel=lambda: True,
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


@pytest.mark.parametrize("during_backoff", [False, True])
def test_cancel_stops_waiting_without_waiting_for_server(during_backoff):
    async def run():
        cancelled = asyncio.Event()
        stopped = asyncio.Event()
        calls = []

        async def post(*args, **kwargs):
            calls.append(1)
            asyncio.get_running_loop().call_later(0.01, cancelled.set)
            if during_backoff:
                return httpx.Response(429, headers={"Retry-After": "30"})
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                sidecar_post(
                    SimpleNamespace(http=SimpleNamespace(post=post)),
                    "/to-markdown",
                    connection=ConnectionConfig(
                        base_url="http://127.0.0.1:1234", token="test"
                    ),
                    should_cancel=cancelled.is_set,
                ),
                timeout=2,
            )
        assert calls == [1]
        assert during_backoff or stopped.is_set()

    asyncio.run(run())
