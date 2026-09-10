"""Regression: a long-lived process doing TWO remote embeds must not die on the 2nd.

gateway._embed_remote wraps each call in asyncio.run (a fresh loop per call). If the
router reuses one persistent httpx.AsyncClient across those loops, the 2nd call fails with
"Event loop is closed" — which would break a worker's 2nd remote embedding.index_refresh
job. Reproduced against a local OpenAI-shaped server (no keys, deterministic in CI).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from frisket.ai.embeddings import EmbeddingGateway
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.adapters import OpenAICompatAdapter
from tests.deterministic_time import controlled_time


class _EmbedHandler(BaseHTTPRequestHandler):
    # keep-alive so httpx POOLS the connection — that pooled, loop1-bound socket is
    # what blows up when the client is reused on loop2 (the real-provider failure).
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        inputs = body.get("input", [])
        out = {
            "model": body.get("model"),
            "object": "list",
            "data": [
                {"index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(len(inputs))
            ],
            "usage": {},
        }
        payload = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the test server
        pass


@pytest.fixture
def local_embed_server():
    srv = HTTPServer(("127.0.0.1", 0), _EmbedHandler)
    with controlled_time() as t:
        t.background(srv.serve_forever)
        try:
            yield srv.server_address[1]
        finally:
            srv.shutdown()


def test_two_sequential_remote_embeds_survive(local_embed_server):
    router = ModelRouter()
    router._adapters["test"] = OpenAICompatAdapter(
        "k", f"http://127.0.0.1:{local_embed_server}"
    )
    gw = EmbeddingGateway(router=router)
    first = gw.embed(["hi"], provider="test", model="m", modality="text")
    second = gw.embed(
        ["yo"], provider="test", model="m", modality="text"
    )  # was: dead loop
    assert len(first["vectors"][0]) == 3
    assert len(second["vectors"][0]) == 3


def test_router_client_is_loop_aware():
    # The genre guard: the shared client (used by chat + any path) must be rebound to
    # the current loop, so sequential asyncio.run callers never reuse a dead-loop one.
    import asyncio

    async def _grab(router):
        return router.client, id(asyncio.get_running_loop())

    router = ModelRouter()
    c1, loop1 = asyncio.run(_grab(router))
    c2, loop2 = asyncio.run(_grab(router))
    assert loop1 != loop2  # asyncio.run made a fresh loop each time
    assert c1 is not c2  # ...and the client was recreated for it
