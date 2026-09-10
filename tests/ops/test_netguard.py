"""``safe_request`` — the one guarded transport: any method, full
header/param/body/cookie passthrough, per-hop SSRF re-check *and* connection
pinning on redirects, and a decoded-byte hard failure instead of a truncation.
All HTTP is mocked via ``httpx.MockTransport`` (the
``test_geocode``/``test_datalab`` pattern) — never a live call."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from frisket.ops.egress_policy import EgressRefused
from frisket.ops.netguard import (
    EncodedResponseRefused,
    ResponseTooLarge,
    SafeResponse,
    TooManyRedirects,
    safe_request,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def _public_dns(monkeypatch):
    """url_is_safe resolves hostnames for real; give the fictitious
    ``good.example`` a public address so these tests stay offline. Redirect
    targets that are already IP literals (the blocked-hop cases) fall through
    to the real resolver, which handles literals without any network I/O."""
    from frisket.ops import egress_policy

    real_getaddrinfo = egress_policy.socket.getaddrinfo

    def fake_getaddrinfo(host, *a, **k):
        if host in {"good.example", "sub.good.example", "other.example"}:
            return [
                (egress_policy.socket.AF_INET, None, None, "", ("93.184.216.34", 443))
            ]
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.asyncio
async def test_blocks_private_loopback_url():
    def handler(request):
        raise AssertionError("must not egress: URL is blocked before send")

    with pytest.raises(EgressRefused, match="blocked URL"):
        await safe_request(_client(handler), "GET", "http://127.0.0.1:9999/admin")


@pytest.mark.asyncio
async def test_blocks_private_url_by_hostname_too():
    def handler(request):
        raise AssertionError("must not egress: URL is blocked before send")

    with pytest.raises(EgressRefused, match="blocked URL"):
        await safe_request(_client(handler), "GET", "http://localhost/admin")


@pytest.mark.asyncio
async def test_blocks_redirect_target_mid_chain(_public_dns):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == "https://good.example/start":
            return httpx.Response(
                302, headers={"Location": "http://10.0.0.5/private-target"}
            )
        raise AssertionError("must not follow through to the private hop")

    with pytest.raises(EgressRefused, match="blocked redirect target"):
        await safe_request(_client(handler), "GET", "https://good.example/start")
    # only the first, public hop was actually sent
    assert calls == ["https://good.example/start"]


@pytest.mark.asyncio
async def test_oversized_response_raises_not_truncates(_public_dns):
    body = b"x" * 1024

    def handler(request):
        return httpx.Response(200, content=body)

    with pytest.raises(ResponseTooLarge):
        await safe_request(
            _client(handler), "GET", "https://good.example/big", max_bytes=16
        )


@pytest.mark.asyncio
async def test_post_with_json_body_returns_status_and_content(_public_dns):
    def handler(request):
        assert request.method == "POST"
        assert request.content == b'{"n": 1}'
        return httpx.Response(201, json={"ok": True})

    result = await safe_request(
        _client(handler),
        "POST",
        "https://good.example/create",
        content=b'{"n": 1}',
        headers={"content-type": "application/json"},
    )
    assert isinstance(result, SafeResponse)
    assert result.status_code == 201
    assert result.content == b'{"ok":true}'
    assert result.url == "https://good.example/create"


@pytest.mark.asyncio
async def test_inline_query_and_repeated_params_are_appended(_public_dns):
    def handler(request):
        assert str(request.url) == (
            "https://good.example/search?inline=kept&tag=inline&tag=one&tag=two"
        )
        return httpx.Response(200)

    await safe_request(
        _client(handler),
        "GET",
        "https://good.example/search?inline=kept&tag=inline",
        params=[("tag", "one"), ("tag", "two")],
    )


@pytest.mark.asyncio
async def test_empty_params_do_not_remove_inline_query(_public_dns):
    def handler(request):
        assert str(request.url) == "https://good.example/search?inline=kept"
        return httpx.Response(200)

    await safe_request(
        _client(handler),
        "GET",
        "https://good.example/search?inline=kept",
        params=[],
    )


@pytest.mark.asyncio
async def test_repeated_headers_and_form_fields_survive_encoding(_public_dns):
    def handler(request):
        assert request.headers.get_list("x-tag") == ["one", "two"]
        assert request.content == b"tag=one&tag=two"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        return httpx.Response(200)

    await safe_request(
        _client(handler),
        "POST",
        "https://good.example/form",
        headers=[("X-Tag", "one"), ("X-Tag", "two")],
        data=[("tag", "one"), ("tag", "two")],
    )


@pytest.mark.asyncio
async def test_transport_forces_identity_encoding_even_over_explicit_header(
    _public_dns,
):
    class NeverRead(httpx.AsyncByteStream):
        iterated = False
        closed = False

        async def __aiter__(self):
            self.iterated = True
            yield b"compressed bytes that must never reach a decoder"

        async def aclose(self) -> None:
            self.closed = True

    stream = NeverRead()

    def handler(request):
        assert request.headers.get_list("accept-encoding") == ["identity"]
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=stream,
        )

    with pytest.raises(EncodedResponseRefused):
        await safe_request(
            _client(handler),
            "GET",
            "https://good.example/compressed",
            headers={"accept-encoding": "gzip, br"},
        )

    # Refusal is based on headers, before HTTPX's decoder can inflate even one
    # attacker-controlled compressed chunk. The response still closes cleanly.
    assert stream.iterated is False
    assert stream.closed is True


@pytest.mark.asyncio
async def test_response_cookies_do_not_bleed_into_later_call(_public_dns):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        if request.url.path == "/first":
            return httpx.Response(200, headers={"Set-Cookie": "session=leaked; Path=/"})
        return httpx.Response(200)

    client = _client(handler)
    await safe_request(client, "GET", "https://good.example/first")
    await safe_request(client, "GET", "https://good.example/second")

    assert seen == [None, None]


@pytest.mark.asyncio
async def test_prior_cookie_does_not_bleed_into_later_redirect_chain(_public_dns):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("cookie")))
        if request.url.path == "/seed":
            return httpx.Response(200, headers={"Set-Cookie": "old=leak; Path=/"})
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "https://good.example/next"}
            )
        return httpx.Response(200)

    client = _client(handler)
    await safe_request(client, "GET", "https://good.example/seed")
    await safe_request(client, "GET", "https://good.example/start")

    assert seen == [("/seed", None), ("/start", None), ("/next", None)]


@pytest.mark.asyncio
async def test_redirect_chain_keeps_its_own_response_cookie(_public_dns):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://good.example/next",
                    "Set-Cookie": "chain=kept; Path=/",
                },
            )
        return httpx.Response(200)

    client = _client(handler)
    await safe_request(client, "GET", "https://good.example/start")

    assert seen == [None, "chain=kept"]


@pytest.mark.asyncio
async def test_explicit_cookie_survives_same_host_redirect(_public_dns):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "https://good.example/next"}
            )
        return httpx.Response(200)

    client = _client(handler)
    await safe_request(
        client,
        "GET",
        "https://good.example/start",
        cookies={"configured": "kept"},
    )

    assert seen == ["configured=kept", "configured=kept"]


@pytest.mark.asyncio
async def test_same_host_redirect_combines_configured_and_response_cookies(_public_dns):
    seen: list[set[str]] = []

    def handler(request):
        raw_cookie = request.headers.get("cookie")
        seen.append(set(raw_cookie.split("; ")) if raw_cookie else set())
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://good.example/next",
                    "Set-Cookie": "chain=kept; Path=/",
                },
            )
        return httpx.Response(200)

    await safe_request(
        _client(handler),
        "GET",
        "https://good.example/start",
        cookies={"configured": "kept"},
    )

    assert seen == [
        {"configured=kept"},
        {"configured=kept", "chain=kept"},
    ]


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_refused_before_configured_cookie_can_leak(
    _public_dns,
):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("cookie")))
        if request.url.host == "good.example":
            return httpx.Response(
                302, headers={"Location": "https://sub.good.example/next"}
            )
        return httpx.Response(200)

    with pytest.raises(EgressRefused, match="cross-origin redirect refused"):
        await safe_request(
            _client(handler),
            "GET",
            "https://good.example/start",
            cookies={"configured": "host-only"},
        )

    assert seen == [("good.example", "configured=host-only")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/receive",
        "https://good.example:444/receive",
        "http://good.example/receive",
    ],
)
async def test_cross_origin_307_is_refused_before_custom_secret_header_or_body_leaks(
    _public_dns, location
):
    seen = []

    def handler(request):
        seen.append(
            (
                str(request.url),
                request.headers.get("x-api-key"),
                request.content,
            )
        )
        if request.url.path == "/start":
            return httpx.Response(307, headers={"Location": location})
        raise AssertionError("cross-origin redirect must not be sent")

    with pytest.raises(EgressRefused, match="cross-origin redirect refused"):
        await safe_request(
            _client(handler),
            "POST",
            "https://good.example/start",
            headers={"x-api-key": "secret"},
            content=b"sensitive body",
        )

    assert seen == [
        ("https://good.example/start", "secret", b"sensitive body"),
    ]


@pytest.mark.asyncio
async def test_same_origin_307_preserves_method_headers_and_replayable_body(
    _public_dns,
):
    seen = []

    def handler(request):
        seen.append(
            (
                request.url.path,
                request.method,
                request.headers.get("x-api-key"),
                request.content,
            )
        )
        if request.url.path == "/start":
            return httpx.Response(307, headers={"Location": "/next"})
        return httpx.Response(200, json={"ok": True})

    result = await safe_request(
        _client(handler),
        "POST",
        "https://good.example/start",
        headers={"x-api-key": "same-origin"},
        content=b"replayable body",
    )

    assert result.status_code == 200
    assert seen == [
        ("/start", "POST", "same-origin", b"replayable body"),
        ("/next", "POST", "same-origin", b"replayable body"),
    ]


@pytest.mark.asyncio
async def test_explicit_cookie_works_for_ipv6_literal_and_same_host_redirect(
    monkeypatch,
):
    from frisket.ops import netguard

    address = "2606:2800:220:1:248:1893:25c8:1946"
    monkeypatch.setattr(
        netguard, "safe_pinned_addresses", lambda url, **kwargs: [address]
    )
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("cookie")))
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": f"https://[{address}]/next"}
            )
        return httpx.Response(200)

    await safe_request(
        _client(handler),
        "GET",
        f"https://[{address}]/start",
        cookies={"configured": "ipv6"},
    )

    assert seen == [
        ("/start", "configured=ipv6"),
        ("/next", "configured=ipv6"),
    ]


@pytest.mark.asyncio
async def test_304_without_location_is_terminal(_public_dns):
    def handler(request):
        return httpx.Response(304)

    result = await safe_request(
        _client(handler), "GET", "https://good.example/not-modified"
    )
    assert result.status_code == 304


@pytest.mark.asyncio
async def test_url_guard_is_nonblocking_and_inside_total_timeout(monkeypatch):
    from frisket.ops import netguard

    def slow_guard(url, *, allow_private=False):
        # realtime: the claim is that a SYNCHRONOUS blocking resolver cannot
        # stall the event loop, so the block has to be real — a manual clock
        # cannot make a thread occupy a worker.
        time.sleep(0.2)  # realtime: see above
        return ["93.184.216.34"]

    monkeypatch.setattr(netguard, "safe_pinned_addresses", slow_guard)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await safe_request(
            _client(lambda request: httpx.Response(200)),
            "GET",
            "https://good.example/slow-dns",
            timeout=0.02,
        )
    elapsed = asyncio.get_running_loop().time() - started
    await beat

    assert elapsed < 0.1
    assert ticks == 5


@pytest.mark.asyncio
async def test_timed_out_url_guards_have_bounded_dedicated_capacity(monkeypatch):
    from frisket.ops import netguard

    release = threading.Event()
    counts_lock = threading.Lock()
    counts = {"started": 0, "active": 0, "peak": 0}

    def blocked_guard(url, *, allow_private=False):
        del url, allow_private
        with counts_lock:
            counts["started"] += 1
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
        try:
            release.wait(timeout=1)
            return ["93.184.216.34"]
        finally:
            with counts_lock:
                counts["active"] -= 1

    monkeypatch.setattr(netguard, "safe_pinned_addresses", blocked_guard)
    client = _client(lambda request: httpx.Response(200))
    attempts = netguard._MAX_CONCURRENT_URL_CHECKS * 3
    try:
        results = await asyncio.gather(
            *(
                safe_request(
                    client,
                    "GET",
                    f"https://slow-{index}.example/",
                    timeout=0.05,
                )
                for index in range(attempts)
            ),
            return_exceptions=True,
        )
        assert all(isinstance(result, TimeoutError) for result in results)
        with counts_lock:
            assert 0 < counts["started"] <= netguard._MAX_CONCURRENT_URL_CHECKS
            assert counts["peak"] <= netguard._MAX_CONCURRENT_URL_CHECKS
            assert counts["started"] < attempts
    finally:
        release.set()

    # Do not leave a blocked resolver behind for later tests or interpreter
    # shutdown; its slot is released by the concurrent-future callback.
    for _ in range(100):
        with counts_lock:
            if counts["active"] == 0:
                break
        await asyncio.sleep(0.01)
    with counts_lock:
        assert counts["active"] == 0


@pytest.mark.asyncio
async def test_redirect_count_cap(_public_dns):
    def handler(request):
        n = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            302, headers={"Location": f"https://good.example/{n + 1}"}
        )

    with pytest.raises(TooManyRedirects):
        await safe_request(
            _client(handler),
            "GET",
            "https://good.example/0",
            max_redirects=2,
        )


@pytest.mark.asyncio
async def test_follow_redirects_false_returns_redirect_without_following(_public_dns):
    def handler(request):
        assert str(request.url) == "https://good.example/start"
        # the redirect target is private; it must never be dereferenced
        return httpx.Response(
            302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )

    result = await safe_request(
        _client(handler),
        "GET",
        "https://good.example/start",
        follow_redirects=False,
    )
    assert result.status_code == 302
    assert result.headers["location"] == "http://169.254.169.254/latest/meta-data/"


@pytest.mark.asyncio
async def test_follow_redirects_false_still_caps_body(_public_dns):
    # a kept-as-is redirect body must go through the same decoded-byte cap
    def handler(request):
        return httpx.Response(
            302, headers={"Location": "https://good.example/next"}, content=b"x" * 1024
        )

    with pytest.raises(ResponseTooLarge):
        await safe_request(
            _client(handler),
            "GET",
            "https://good.example/start",
            follow_redirects=False,
            max_bytes=16,
        )


# ---------------------------------------------------------------------------
# IP pinning (DNS-rebinding TOCTOU closure)
#
# The MockTransport suite above deliberately exercises the *unpinned* path:
# an in-memory transport opens no socket, so ``_BorrowedAsyncTransport`` leaves
# its request on the hostname. The tests below use a real ``AsyncHTTPTransport``
# subclass (recognized for pinning, but overridden to open no socket) so they
# observe exactly what a live connection would be dialed at.
# ---------------------------------------------------------------------------


class _RecordingHTTPTransport(httpx.AsyncHTTPTransport):
    """A real transport (so pinning applies) that records and never egresses."""

    def __init__(self, responder) -> None:
        super().__init__()
        self.requests: list[httpx.Request] = []
        self._responder = responder

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _fixed_addr(*addresses: str):
    from frisket.ops import egress_policy

    return [
        (egress_policy.socket.AF_INET, None, None, "", (addr, 0)) for addr in addresses
    ]


@pytest.mark.asyncio
async def test_connection_is_pinned_to_the_vetted_address_not_a_rebind(monkeypatch):
    """A resolver that answers public (to the guard) then private (to a would-be
    connect re-resolution) must never see its private answer dialed: resolution
    happens once and the socket is pinned to the vetted literal."""
    from frisket.ops import egress_policy

    answers = iter(
        [
            _fixed_addr("93.184.216.34"),  # vetted by the guard
            _fixed_addr("169.254.169.254"),  # a rebind would hand this to connect
        ]
    )
    calls = {"n": 0}

    def fake_getaddrinfo(host, *a, **k):
        calls["n"] += 1
        return next(answers)

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fake_getaddrinfo)

    recording = _RecordingHTTPTransport(
        lambda request: httpx.Response(200, json={"ok": True})
    )
    result = await safe_request(
        httpx.AsyncClient(transport=recording), "GET", "https://good.example/data"
    )

    assert result.status_code == 200
    # Exactly one resolution occurred; the poisoned second answer was never
    # requested, and the socket target is the vetted public literal.
    assert calls["n"] == 1
    sent = recording.requests[-1]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "good.example"
    # TLS SNI + certificate verification stay bound to the hostname, never the IP.
    assert sent.extensions.get("sni_hostname") == "good.example"


@pytest.mark.asyncio
async def test_every_redirect_hop_is_pinned_to_its_vetted_address(monkeypatch):
    from frisket.ops import egress_policy

    monkeypatch.setattr(
        egress_policy.socket,
        "getaddrinfo",
        lambda host, *a, **k: _fixed_addr("93.184.216.34"),
    )

    def responder(request):
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "https://good.example/next"}
            )
        return httpx.Response(200, json={"ok": True})

    recording = _RecordingHTTPTransport(responder)
    result = await safe_request(
        httpx.AsyncClient(transport=recording), "GET", "https://good.example/start"
    )

    assert result.status_code == 200
    # Both the initial request and the followed hop reached the transport pinned
    # to the vetted literal with the hostname preserved for Host + TLS identity.
    assert [request.url.path for request in recording.requests] == ["/start", "/next"]
    for request in recording.requests:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "good.example"
        assert request.extensions.get("sni_hostname") == "good.example"


@pytest.mark.asyncio
async def test_redirect_to_a_rebinding_private_answer_is_refused(monkeypatch):
    """The per-hop re-resolution vets the redirect target; a hop that resolves
    to a private address is refused before any socket is dialed to it."""
    from frisket.ops import egress_policy

    def fake_getaddrinfo(host, *a, **k):
        if host == "good.example":
            return _fixed_addr("93.184.216.34")
        return _fixed_addr("10.0.0.5")  # the redirect hostname rebinds internal

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fake_getaddrinfo)

    def responder(request):
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "https://sneaky.example/next"}
            )
        raise AssertionError("the rebinding redirect hop must never be dialed")

    recording = _RecordingHTTPTransport(responder)
    with pytest.raises(EgressRefused, match="blocked redirect target"):
        await safe_request(
            httpx.AsyncClient(transport=recording),
            "GET",
            "https://good.example/start",
        )
    assert [request.url.path for request in recording.requests] == ["/start"]


@pytest.mark.asyncio
async def test_cross_origin_redirect_option_still_vets_and_pins_each_hop(monkeypatch):
    """The option lifts the same-origin rule and nothing else: the new origin is
    resolved, vetted and pinned exactly like the first one."""
    from frisket.ops import egress_policy

    monkeypatch.setattr(
        egress_policy.socket,
        "getaddrinfo",
        lambda host, *a, **k: _fixed_addr("93.184.216.34"),
    )

    def responder(request):
        if request.headers["host"] == "good.example":
            return httpx.Response(
                302, headers={"Location": "https://other.example/landed"}
            )
        return httpx.Response(200, json={"ok": True})

    recording = _RecordingHTTPTransport(responder)
    result = await safe_request(
        httpx.AsyncClient(transport=recording),
        "GET",
        "https://good.example/start",
        cross_origin_redirects=True,
    )

    assert result.status_code == 200
    assert [request.headers["host"] for request in recording.requests] == [
        "good.example",
        "other.example",
    ]
    for request in recording.requests:
        assert request.url.host == "93.184.216.34"
        assert request.extensions.get("sni_hostname") == request.headers["host"]


@pytest.mark.asyncio
async def test_cross_origin_redirect_option_still_refuses_a_private_new_origin(
    monkeypatch,
):
    from frisket.ops import egress_policy

    def fake_getaddrinfo(host, *a, **k):
        if host == "good.example":
            return _fixed_addr("93.184.216.34")
        return _fixed_addr("10.0.0.5")

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fake_getaddrinfo)

    def responder(request):
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "https://sneaky.example/next"}
            )
        raise AssertionError("the private new origin must never be dialed")

    recording = _RecordingHTTPTransport(responder)
    with pytest.raises(EgressRefused, match="blocked redirect target"):
        await safe_request(
            httpx.AsyncClient(transport=recording),
            "GET",
            "https://good.example/start",
            cross_origin_redirects=True,
        )
    assert [request.url.path for request in recording.requests] == ["/start"]


@pytest.mark.parametrize(
    "kwargs, named",
    [
        ({"headers": {"x-api-key": "secret"}}, "headers"),
        ({"cookies": {"session": "secret"}}, "cookies"),
        ({"content": b"replayable"}, "content"),
        ({"data": [("a", "b")]}, "data"),
        ({"json": {"a": "b"}}, "json"),
    ],
)
@pytest.mark.asyncio
async def test_cross_origin_redirects_refused_when_there_is_something_to_forward(
    _public_dns, kwargs, named
):
    """The option is admitted by construction, not by caller discipline: a
    request carrying anything forwardable cannot ask for it at all."""

    def handler(request):
        raise AssertionError("must not egress: the request is refused at the door")

    with pytest.raises(ValueError, match=named):
        await safe_request(
            _client(handler),
            "POST",
            "https://good.example/start",
            cross_origin_redirects=True,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_still_refused_by_default(_public_dns):
    """Default is unchanged: the option is opt-in per call site."""

    def handler(request):
        if request.url.host == "good.example":
            return httpx.Response(
                302, headers={"Location": "https://other.example/next"}
            )
        raise AssertionError("must not follow to another origin by default")

    with pytest.raises(EgressRefused, match="cross-origin redirect refused"):
        await safe_request(_client(handler), "GET", "https://good.example/start")
