"""Egress guarantees of the ``research.answer`` fetch tool.

The agent loop lets the MODEL choose the URLs, so this is the path where an
attacker-influenced target is most likely and the guard has to be authoritative
rather than advisory.

Every response here is served by an in-process transport that resolves the
request's hostname *itself* at "connect" time — exactly what a real socket
transport does, and exactly what makes DNS rebinding possible. It subclasses
``httpx.AsyncHTTPTransport`` so the guard's IP pinning applies as it does in
production. No live network call is made, and no internal address is dialed for
real: the "internal" service is a dictionary entry.
"""

from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest

from frisket.ai.research.row_answer import FETCH_MAX_BYTES, fetch_page

PUBLIC_IP = "93.184.216.34"
INTERNAL_IP = "127.0.0.1"
PRIVATE_IP = "10.0.0.5"

PUBLIC_BODY = (
    b"<html><head><style>p{color:red}</style></head>"
    b"<body><p>badger census 2026</p></body></html>"
)
INTERNAL_BODY = b"<html><body>INTERNAL-SECRET admin console</body></html>"
OTHER_ORIGIN_BODY = b"<html><body><p>syndicated copy</p></body></html>"


def _addr(address: str):
    return [(socket.AF_INET, None, None, "", (address, 0))]


class _ResolvingTransport(httpx.AsyncHTTPTransport):
    """A real-transport stand-in that resolves the host at connect time.

    Keyed on the *logical* origin (scheme + Host header + path) so a request the
    guard pinned to an IP literal still finds its fixture, and on the address it
    actually dialed for the body — which is the whole point: a rebound name and
    a vetted name reach different services.
    """

    def __init__(
        self, bodies: dict[str, bytes], *, redirects: dict[str, str] | None = None
    ):
        super().__init__()
        self._bodies = bodies
        self._redirects = redirects or {}
        self.dialed: list[str] = []
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        address = socket.getaddrinfo(request.url.host, None)[0][4][0]
        self.dialed.append(address)
        host_header = request.headers.get("host", request.url.host)
        self.hosts.append(host_header)
        logical = f"{request.url.scheme}://{host_header}{request.url.path}"
        target = self._redirects.get(logical)
        if target is not None:
            return httpx.Response(302, headers={"Location": target}, request=request)
        return httpx.Response(200, content=self._bodies[address], request=request)


@pytest.fixture
def rebinding_dns(monkeypatch):
    """``wiki.example`` answers public once, then loopback forever after.

    The first answer is the one the guard vets; every later answer is what a
    client that re-resolves the name at connect time would dial. IP literals
    resolve to themselves, so a pinned connection reaches exactly its pin.
    """
    from frisket.ops import egress_policy

    calls = {"n": 0}

    def fake_getaddrinfo(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return _addr(host)
        if host == "wiki.example":
            calls["n"] += 1
            return _addr(PUBLIC_IP if calls["n"] == 1 else INTERNAL_IP)
        if host == "internal.example":
            return _addr(PRIVATE_IP)
        return _addr(PUBLIC_IP)

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fake_getaddrinfo)
    return calls


async def _fetch(url: str, transport: httpx.AsyncBaseTransport) -> str:
    async with httpx.AsyncClient(transport=transport) as client:
        return await fetch_page(url, client)


@pytest.mark.asyncio
async def test_rebinding_answer_is_never_dialed(rebinding_dns):
    """The demonstration: a name that resolves public at check time and
    loopback at connect time must not reach the loopback service."""
    transport = _ResolvingTransport(
        {PUBLIC_IP: PUBLIC_BODY, INTERNAL_IP: INTERNAL_BODY}
    )

    text = await _fetch("https://wiki.example/page", transport)

    assert transport.dialed == [PUBLIC_IP], "connected to a rebound address"
    assert "INTERNAL-SECRET" not in text
    assert "badger census 2026" in text
    # Host header (and therefore TLS identity) stays on the name, not the pin.
    assert transport.hosts == ["wiki.example"]


@pytest.mark.asyncio
async def test_normal_page_is_fetched_and_stripped(rebinding_dns):
    transport = _ResolvingTransport(
        {PUBLIC_IP: PUBLIC_BODY, INTERNAL_IP: INTERNAL_BODY}
    )

    text = await _fetch("https://wiki.example/page", transport)

    # script/style dropped, tags stripped, whitespace collapsed
    assert text == "badger census 2026"


@pytest.mark.asyncio
async def test_oversized_page_is_refused_not_silently_truncated(rebinding_dns):
    """A clipped page silently becomes a wrong answer, so the model is told."""
    huge = b"<html><body>" + b"x" * (FETCH_MAX_BYTES + 1) + b"</body></html>"
    transport = _ResolvingTransport({PUBLIC_IP: huge})

    text = await _fetch("https://wiki.example/huge", transport)

    assert text.startswith("fetch failed:")
    assert str(FETCH_MAX_BYTES) in text
    assert "xxxx" not in text


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_followed_with_every_hop_pinned(rebinding_dns):
    """A public page redirecting off-site is a normal read for this tool; each
    hop is still resolved, vetted and pinned independently."""
    transport = _ResolvingTransport(
        {PUBLIC_IP: OTHER_ORIGIN_BODY},
        redirects={"http://wiki.example/page": "https://cdn.other.example/copy"},
    )

    text = await _fetch("http://wiki.example/page", transport)

    assert text == "syndicated copy"
    assert transport.hosts == ["wiki.example", "cdn.other.example"]
    assert transport.dialed == [PUBLIC_IP, PUBLIC_IP]


@pytest.mark.asyncio
async def test_redirect_to_a_private_host_is_refused(rebinding_dns):
    transport = _ResolvingTransport(
        {PUBLIC_IP: PUBLIC_BODY, PRIVATE_IP: INTERNAL_BODY},
        redirects={"https://wiki.example/page": "https://internal.example/secrets"},
    )

    text = await _fetch("https://wiki.example/page", transport)

    assert text.startswith("fetch failed:")
    assert "blocked redirect target" in text
    assert transport.dialed == [PUBLIC_IP]


@pytest.mark.asyncio
async def test_blocked_url_is_reported_as_a_tool_error(rebinding_dns):
    transport = _ResolvingTransport({})

    text = await _fetch("http://internal.example/secrets", transport)

    assert text.startswith("fetch failed:")
    assert "blocked URL" in text
    assert transport.dialed == []


@pytest.mark.asyncio
async def test_non_http_scheme_is_rejected_before_any_egress(rebinding_dns):
    transport = _ResolvingTransport({})

    assert await _fetch("file:///etc/passwd", transport) == "invalid url"
    assert transport.dialed == []
