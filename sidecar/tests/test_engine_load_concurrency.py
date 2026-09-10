"""A slow first load of any engine (weights download, resident-model
construction — the Chandra 2 HF path can take minutes) must never block the
event loop. Before the fix, ``_engine()``/the ``/v1/embeddings`` route
called ``Engine.get()`` directly inside the async route's own coroutine —
that's synchronous, so a slow loader froze the ENTIRE event loop: /health,
/capabilities, and every other in-flight request on the box, not just the
slow one. Not chandra-specific — every route shared the same helper.

The fix routes engine loading through ``run_in_threadpool`` (the same
executor path inference calls already use). This test proves it: a stubbed
engine with a genuinely slow, BLOCKING loader (``time.sleep`` — real
synchronous work, the same shape a torch/transformers load does) must not
delay a concurrent /health response. TestClient can't exercise real
concurrency (it drives one request at a time through a portal), so this
uses httpx.AsyncClient over an ASGI transport with asyncio.gather, wrapped
in a plain (non-async) test via asyncio.run() — no new test-framework
dependency (no pytest-asyncio/anyio-pytest-plugin needed)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from frisket_models.app import create_app
from frisket_models.engines import Engine, Registry

# realtime: proves real event-loop non-blocking by measuring actual wall-clock
# response latency against a genuinely blocking (time.sleep) loader running
# under run_in_threadpool — no async rewrite and no virtual clock because the timing
# thresholds below are bound checks racing a real clock, not rendezvous-backed.
pytestmark = pytest.mark.realtime

TOKEN = "concurrency-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

LOAD_SECONDS = 0.4


def _slow_loader():
    """A stand-in for a slow real-model construction (weights download /
    torch model build): genuinely blocking, not an asyncio-friendly sleep —
    exactly the kind of call load_chandra/load_docling/etc. make."""
    time.sleep(LOAD_SECONDS)
    return lambda name, data: {"markdown": "loaded", "ocr_used": [True]}


def _slow_registry() -> Registry:
    return Registry([Engine("slow-to-markdown", "/to-markdown", [], _slow_loader)])


async def _run_scenario():
    app = create_app(token=TOKEN, registry=_slow_registry(), concurrency=2)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://sidecar"
    ) as client:

        async def slow_request():
            return await client.post(
                "/to-markdown",
                files=[("files", ("d.pdf", b"%PDF- stub", "application/octet-stream"))],
                data={"engine": "slow-to-markdown"},
                headers=AUTH,
            )

        # t0 is BEFORE either request starts — the elapsed-to-health-response
        # measurement must span the window where the slow load's blocking
        # call actually executes. (An earlier version of this test started
        # the clock only after a head-start `asyncio.sleep`, which — if the
        # event loop were blocked — would itself simply resume late, hiding
        # the delay instead of exposing it in the measured elapsed time.)
        t0 = time.monotonic()
        slow_task = asyncio.create_task(slow_request())
        # give the slow request a head start into ITS coroutine so it's the
        # one that reaches the blocking loader call first
        await asyncio.sleep(LOAD_SECONDS / 4)
        health_resp = await client.get("/health")
        health_elapsed = time.monotonic() - t0
        slow_resp = await slow_task

    return slow_resp, health_resp, health_elapsed


def test_slow_first_load_does_not_block_concurrent_health():
    slow_resp, health_resp, health_elapsed = asyncio.run(_run_scenario())

    assert slow_resp.status_code == 200, slow_resp.text
    assert slow_resp.json()["documents"][0]["markdown"] == "loaded"

    assert health_resp.status_code == 200
    # /health must return shortly after the head-start sleep (~LOAD_SECONDS/4)
    # — well before the slow loader finishes (~LOAD_SECONDS). If the event
    # loop were blocked by the loader, /health could only be dispatched
    # AFTER the block ends, so health_elapsed would be close to LOAD_SECONDS
    # instead. The threshold sits strictly between the two.
    assert health_elapsed < LOAD_SECONDS * 0.75, (
        f"/health took {health_elapsed:.3f}s (head start was "
        f"{LOAD_SECONDS / 4:.3f}s, the full load is {LOAD_SECONDS}s) while "
        "a slow engine load was in flight — the event loop was blocked"
    )


async def _run_capabilities_scenario():
    app = create_app(token=TOKEN, registry=_slow_registry(), concurrency=2)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://sidecar"
    ) as client:

        async def slow_request():
            return await client.post(
                "/to-markdown",
                files=[("files", ("d.pdf", b"%PDF- stub", "application/octet-stream"))],
                data={"engine": "slow-to-markdown"},
                headers=AUTH,
            )

        t0 = time.monotonic()
        slow_task = asyncio.create_task(slow_request())
        await asyncio.sleep(LOAD_SECONDS / 4)
        caps_resp = await client.get("/capabilities", headers=AUTH)
        caps_elapsed = time.monotonic() - t0
        await slow_task

    return caps_resp, caps_elapsed


def test_slow_first_load_does_not_block_concurrent_capabilities():
    caps_resp, caps_elapsed = asyncio.run(_run_capabilities_scenario())

    assert caps_resp.status_code == 200, caps_resp.text
    assert caps_elapsed < LOAD_SECONDS * 0.75, (
        f"/capabilities took {caps_elapsed:.3f}s (head start was "
        f"{LOAD_SECONDS / 4:.3f}s, the full load is {LOAD_SECONDS}s) while "
        "a slow engine load was in flight — the event loop was blocked"
    )


def test_concurrent_first_loads_of_the_same_engine_load_once():
    """The per-engine threading.Lock in Engine.get (engines.py) already
    prevents a double-load; this pins that it still holds now that loading
    runs off the event loop via run_in_threadpool."""
    calls = {"n": 0}

    def counting_slow_loader():
        calls["n"] += 1
        time.sleep(LOAD_SECONDS)
        return lambda name, data: {"markdown": "loaded", "ocr_used": [True]}

    registry = Registry(
        [Engine("slow-to-markdown", "/to-markdown", [], counting_slow_loader)]
    )

    async def scenario():
        app = create_app(token=TOKEN, registry=registry, concurrency=2)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://sidecar"
        ) as client:

            async def one_request():
                return await client.post(
                    "/to-markdown",
                    files=[
                        (
                            "files",
                            ("d.pdf", b"%PDF- stub", "application/octet-stream"),
                        )
                    ],
                    data={"engine": "slow-to-markdown"},
                    headers=AUTH,
                )

            responses = await asyncio.gather(one_request(), one_request())
        return responses

    responses = asyncio.run(scenario())
    for resp in responses:
        assert resp.status_code == 200, resp.text
    assert calls["n"] == 1
