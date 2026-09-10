"""SSRF guard for server-side fetches. Host
classification lives in ``frisket.ops.egress_policy``; this module keeps the
strict-tier ``url_is_safe`` surface for non-media fetchers (RSS polling, URL
capture, notifications, API sources) and ``safe_request`` — the one transport
every guarded fetch goes through, for the per-row API-call op and the research
agent's fetch tool alike. It resolves once, pins each hop's connection to a
vetted resolved address so httpx cannot re-resolve the name to a rebinding
answer at connect, and re-resolves/re-vets/re-pins on every redirect it
follows. There is deliberately no second, name-based fetch helper: a caller
that vets a hostname and then hands the *name* to a client is the
rebinding gap ``egress_policy``'s docstring describes."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from frisket.ops.egress_policy import (  # noqa: F401 — url_is_safe is this module's public surface
    BLOCKED_HOSTS,
    METADATA_HOSTS,
    EgressRefused,
    safe_pinned_addresses,
    url_is_safe,
)

MAX_REQUEST_BYTES = 20 * 1024 * 1024

# ``socket.getaddrinfo`` is blocking and a cancelled executor future cannot
# stop a lookup already running in libc.  Keep API-call classification off the
# process-wide default executor and admit at most this many live resolver jobs;
# timed-out rows waiting for a slot never enqueue more abandoned work.
_MAX_CONCURRENT_URL_CHECKS = 8
_URL_CHECK_SLOT_POLL_SECONDS = 0.01
_URL_CHECK_STATE_LOCK = threading.Lock()
_URL_CHECK_STATE: tuple[int, ThreadPoolExecutor, threading.BoundedSemaphore] | None = (
    None
)


def _url_check_state() -> tuple[ThreadPoolExecutor, threading.BoundedSemaphore]:
    """Return the bounded resolver pool owned by this process.

    State is lazy and PID-keyed so a process fork never reuses an executor
    whose worker threads existed only in the parent.
    """

    global _URL_CHECK_STATE
    pid = os.getpid()
    with _URL_CHECK_STATE_LOCK:
        if _URL_CHECK_STATE is None or _URL_CHECK_STATE[0] != pid:
            _URL_CHECK_STATE = (
                pid,
                ThreadPoolExecutor(
                    max_workers=_MAX_CONCURRENT_URL_CHECKS,
                    thread_name_prefix="frisket-url-guard",
                ),
                threading.BoundedSemaphore(_MAX_CONCURRENT_URL_CHECKS),
            )
        return _URL_CHECK_STATE[1], _URL_CHECK_STATE[2]


async def _safe_pinned_addresses_async(url: str) -> list[str] | None:
    """Resolve+vet a host with bounded, cancellation-safe admission, and return
    the vetted addresses to pin the connection to (``None`` if refused).

    Resolving here — once — and connecting to a returned literal is what closes
    the resolve/connect race: httpx would otherwise re-resolve the hostname at
    connect time and could dial a rebinding answer this vet never saw.

    The outer request deadline may stop awaiting an in-flight lookup, but its
    dedicated worker retains the slot until libc actually returns.  Subsequent
    timed-out rows therefore cannot flood either this pool's queue or asyncio's
    shared default executor with abandoned DNS work.
    """

    executor, slots = _url_check_state()
    while not slots.acquire(blocking=False):
        await asyncio.sleep(_URL_CHECK_SLOT_POLL_SECONDS)
    try:
        future = executor.submit(safe_pinned_addresses, url, allow_private=False)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _future: slots.release())
    # Do not propagate caller cancellation into the concurrent future: a
    # running getaddrinfo cannot be cancelled, and its completion callback owns
    # the admission slot until the real work has stopped.
    return await asyncio.shield(asyncio.wrap_future(future))


class TooManyRedirects(RuntimeError):
    """A ``safe_request`` redirect chain exceeded ``max_redirects``."""


class ResponseTooLarge(RuntimeError):
    """A ``safe_request`` decoded response body exceeded ``max_bytes``.

    Raised, never truncated. ``safe_request`` negotiates identity encoding so
    this decoded-byte limit is also a wire-byte limit; encoded responses are
    refused before HTTPX's automatic decoder can allocate their output.
    """


class EncodedResponseRefused(ResponseTooLarge):
    """A peer ignored identity negotiation and returned encoded content.

    HTTPX's automatic decoders inflate each incoming chunk fully before an
    application iterator can inspect its size.  Treat an encoded response as a
    response-size refusal so callers retain their existing bounded-response
    error path without exposing the process to a compressed-chunk bomb.
    """


@dataclass(frozen=True)
class SafeResponse:
    """What ``safe_request`` hands back — no ``.json()`` here, callers parse."""

    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str


# Request-scoped channel carrying the address the guard already vetted for this
# hop. Read only by ``_BorrowedAsyncTransport`` when a real socket transport is
# about to dial; it never leaves this module.
_PINNED_IP_EXTENSION = "_frisket_pinned_ip"


def _pin_to_vetted_ip(request: httpx.Request) -> httpx.Request:
    """Force this hop's socket onto the address the guard already vetted.

    ``egress_policy`` resolved the host and vetted every answer; without pinning
    httpx would resolve the *name* a second time at connect and could dial a
    different (rebinding) address. Rewrite the connect target to the vetted
    literal while keeping the Host header and TLS identity on the original
    hostname — the ``sni_hostname`` extension drives SNI and certificate
    verification, and the Host header httpx set at build time is preserved.

    Returns the original request unchanged when there is nothing to pin (no
    vetted address recorded, or the host is already that literal).
    """

    ip = request.extensions.get(_PINNED_IP_EXTENSION)
    hostname = request.url.host
    if not ip or ip == hostname:
        return request
    extensions = {
        key: value
        for key, value in request.extensions.items()
        if key != _PINNED_IP_EXTENSION
    }
    extensions["sni_hostname"] = hostname
    # ``stream=`` is passed, so httpx does not re-run header preparation: the
    # Host header (and content-length) already on ``request`` carry over verbatim
    # and the rewritten IP authority never reaches the Host header or a decoder.
    return httpx.Request(
        method=request.method,
        url=request.url.copy_with(host=ip),
        headers=request.headers,
        stream=request.stream,
        extensions=extensions,
    )


class _BorrowedAsyncTransport(httpx.AsyncBaseTransport):
    """Route through an existing client's pools without owning or closing them.

    ``httpx.AsyncClient`` always stores response cookies on its own jar. API
    calls share a long-lived client for connection pooling, so sending through
    that client directly would let one row's ``Set-Cookie`` affect another.
    A short-lived client backed by this non-owning transport gets an isolated
    cookie jar while all sockets and proxy/mount routing remain shared.

    It is also where IP pinning lands: a real ``AsyncHTTPTransport`` opens a
    socket and would re-resolve the hostname, so its request is rewritten to the
    guard's vetted literal. In-memory transports (``MockTransport`` and the
    like) open no socket and are delegated to untouched.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._client._transport_for_url(request.url)
        if isinstance(transport, httpx.AsyncHTTPTransport):
            request = _pin_to_vetted_ip(request)
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        # The caller owns the underlying client and its connection pools.
        return None


def _append_query_params(
    url: str, params: dict[str, Any] | list[tuple[str, Any]] | None
) -> httpx.URL:
    """Append encoded params without replacing a query embedded in ``url``."""
    parsed = httpx.URL(url)
    encoded = str(httpx.QueryParams(params)) if params else ""
    if not encoded:
        return parsed
    query = parsed.query
    suffix = encoded.encode("ascii")
    if query:
        suffix = query + b"&" + suffix
    return parsed.copy_with(query=suffix)


def _prepare_form_content(
    headers: dict[str, str] | list[tuple[str, str]] | None,
    content: bytes | str | None,
    data: dict[str, Any] | list[tuple[str, Any]] | None,
) -> tuple[
    dict[str, str] | list[tuple[str, str]] | None,
    bytes | str | None,
    dict[str, Any] | None,
]:
    """Encode ordered form pairs without collapsing duplicate field names."""
    if not isinstance(data, list):
        return headers, content, data
    if content is not None:
        raise ValueError("content and form data cannot both be supplied")

    rendered_headers = (
        list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    )
    if not any(name.lower() == "content-type" for name, _value in rendered_headers):
        rendered_headers.append(("content-type", "application/x-www-form-urlencoded"))
    return rendered_headers, urlencode(data, doseq=True).encode("ascii"), None


def _has_header(
    headers: dict[str, str] | list[tuple[str, str]] | None, name: str
) -> bool:
    pairs = headers.items() if isinstance(headers, dict) else headers or []
    return any(header_name.lower() == name.lower() for header_name, _value in pairs)


def _identity_encoding_headers(
    headers: dict[str, str] | list[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    """Negotiate only an identity response while retaining ordered headers.

    This deliberately overrides a caller-supplied ``Accept-Encoding``.  The
    transport promises a hard decoded-byte bound; HTTPX cannot provide that
    guarantee for gzip/brotli/zstd because it materializes one decoded chunk
    before yielding it to us.
    """

    pairs = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    return [
        (name, value) for name, value in pairs if name.lower() != "accept-encoding"
    ] + [("accept-encoding", "identity")]


def _apply_exact_host_cookies(
    request: httpx.Request,
    *,
    initial_host: str,
    configured: dict[str, str],
    response_cookies: httpx.Cookies,
) -> None:
    """Attach configured cookies only when this hop exactly matches the first host.

    A CookieJar entry with ``domain=initial_host`` is a domain cookie: it leaks
    to subdomains and does not match IPv6 literals.  Instead, build a temporary
    one-request jar from response cookies plus configured values and copy only
    its header.  The configured values never enter the redirect client's jar.
    """

    if not configured or request.url.host != initial_host:
        return
    merged = httpx.Cookies(response_cookies)
    for name, value in configured.items():
        # Explicit request configuration wins over a same-named Set-Cookie on
        # same-host redirects, matching its behavior on the initial request.
        merged.delete(name)
        merged.set(name, value)
    cookie_request = httpx.Request("GET", request.url, cookies=merged)
    cookie_header = cookie_request.headers.get("cookie")
    if cookie_header:
        request.headers["cookie"] = cookie_header
    else:  # pragma: no cover - configured is non-empty and validated upstream
        request.headers.pop("cookie", None)


def _refuse_encoded_response(resp: httpx.Response) -> None:
    encodings = [
        value.strip().lower()
        for value in resp.headers.get_list("content-encoding", split_commas=True)
        if value.strip()
    ]
    unsupported = [value for value in encodings if value != "identity"]
    if unsupported:
        raise EncodedResponseRefused(
            "encoded response refused because the decoded-byte limit requires "
            f"identity encoding (received {', '.join(unsupported)})"
        )


def _same_origin(left: httpx.URL, right: httpx.URL) -> bool:
    """Whether two normalized HTTPX URLs share scheme, host, and effective port."""

    return (
        left.scheme == right.scheme
        and left.host == right.host
        and left.port == right.port
    )


async def _read_capped(resp: httpx.Response, max_bytes: int, url: str) -> bytes:
    """Buffer an identity body, raising ``ResponseTooLarge`` above the cap.

    ``safe_request`` negotiates identity and rejects an encoded terminal response
    before this function runs.  Raw and decoded bytes are therefore identical,
    so the check occurs before any content decoder can materialize an oversized
    chunk. Every returned response passes through here — including a redirect
    kept as-is under ``follow_redirects=False``.
    """
    # ``MockTransport`` and other in-memory transports may hand back a response
    # constructed with ``content=``. HTTPX marks that stream consumed and keeps
    # the bytes on the response; production streaming responses take the raw
    # iterator below. The same bound applies in either shape.
    if resp.is_stream_consumed:
        content = resp.content
        if len(content) > max_bytes:
            raise ResponseTooLarge(
                f"response exceeded {max_bytes} decoded bytes: {url}"
            )
        return content

    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_raw():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(
                f"response exceeded {max_bytes} decoded bytes: {url}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def safe_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    content: bytes | str | None = None,
    data: dict[str, Any] | list[tuple[str, Any]] | None = None,
    json: Any = None,
    cookies: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_REQUEST_BYTES,
    follow_redirects: bool = True,
    max_redirects: int = 5,
    cross_origin_redirects: bool = False,
) -> SafeResponse:
    """Arbitrary-method request with SSRF guard on every hop and a hard byte cap.

    Any method, headers/params/body/cookies passed through, and a caller-chosen
    ``max_bytes`` enforced on the decoded stream (raises ``ResponseTooLarge``
    instead of truncating). To make that bound a memory guarantee, the transport
    negotiates ``Accept-Encoding: identity`` and refuses a terminal response
    carrying any other content encoding before iterating its body. With
    ``follow_redirects=False`` the first redirect response is returned as-is
    — its target is never re-checked because it's never fetched.

    Followed redirects must stay on the same origin by default: that is what
    stops custom credential headers and replayable 307/308 bodies reaching a
    different public service. ``cross_origin_redirects=True`` lifts only that
    rule — every hop is still resolved, re-vetted and re-pinned — and is
    admitted only for a request that has nothing to forward: no caller headers,
    no cookies, no body. Reading a web page the way a browser does needs it
    (an ``http://`` → ``https://`` upgrade is already cross-origin here); a
    request carrying credentials is refused the option outright rather than
    trusting the caller to have checked.
    """
    if cross_origin_redirects:
        carried = [
            name
            for name, value in (
                ("headers", headers),
                ("cookies", cookies),
                ("content", content),
                ("data", data),
                ("json", None if json is None else True),
            )
            if value
        ]
        if carried:
            raise ValueError(
                "cross_origin_redirects=True is only available for a request "
                "with nothing to forward to another origin; got " + ", ".join(carried)
            )
    hops = 0
    # ``timeout`` is httpx's per-hop inactivity budget; the outer deadline makes
    # it a wall-clock bound on the whole guarded call, including DNS-based URL
    # classification and every redirect. Host resolution is blocking, so run
    # the guard off the event loop.
    async with asyncio.timeout(timeout):
        pinned = await _safe_pinned_addresses_async(url)
        if pinned is None:
            raise EgressRefused(f"blocked URL (private/loopback/metadata): {url}")

        request_url = _append_query_params(url, params)
        explicit_cookie_header = _has_header(headers, "cookie")
        headers, content, data = _prepare_form_content(headers, content, data)
        headers = _identity_encoding_headers(headers)
        configured_cookies = {} if explicit_cookie_header else dict(cookies or {})
        async with httpx.AsyncClient(
            transport=_BorrowedAsyncTransport(client),
            follow_redirects=False,
        ) as isolated_client:
            request = isolated_client.build_request(
                method,
                request_url,
                headers=headers,
                content=content,
                data=data,
                json=json,
                timeout=timeout,
            )
            request.extensions[_PINNED_IP_EXTENSION] = pinned[0]
            _apply_exact_host_cookies(
                request,
                initial_host=request_url.host,
                configured=configured_cookies,
                response_cookies=isolated_client.cookies,
            )
            while True:
                resp = await isolated_client.send(
                    request, stream=True, follow_redirects=False
                )
                try:
                    # ``is_redirect`` also includes 304, which has no Location
                    # and therefore no ``next_request``. Only an actionable
                    # redirect enters the guarded follow loop.
                    if resp.has_redirect_location and follow_redirects:
                        hops += 1
                        if hops > max_redirects:
                            raise TooManyRedirects(
                                f"exceeded {max_redirects} redirects: {url}"
                            )
                        next_request = resp.next_request
                        if next_request is None:  # defensive against transports
                            raise httpx.RemoteProtocolError(
                                "redirect response did not provide a next request",
                                request=request,
                            )
                        nxt = str(next_request.url)
                        next_pinned = await _safe_pinned_addresses_async(nxt)
                        if next_pinned is None:
                            raise EgressRefused(f"blocked redirect target: {nxt}")
                        if not cross_origin_redirects and not _same_origin(
                            request.url, next_request.url
                        ):
                            raise EgressRefused(f"cross-origin redirect refused: {nxt}")
                        request = next_request
                        # Pin this hop too: every followed redirect re-resolves
                        # and re-vets, then dials the vetted literal — the same
                        # guarantee as the initial request, applied per hop.
                        request.extensions[_PINNED_IP_EXTENSION] = next_pinned[0]
                        _apply_exact_host_cookies(
                            request,
                            initial_host=request_url.host,
                            configured=configured_cookies,
                            response_cookies=isolated_client.cookies,
                        )
                        continue

                    # Terminal response, or a redirect kept as-is under
                    # follow_redirects=False — either way the body is capped.
                    _refuse_encoded_response(resp)
                    response_content = await _read_capped(resp, max_bytes, url)
                    return SafeResponse(
                        status_code=resp.status_code,
                        headers=resp.headers,
                        content=response_content,
                        url=str(resp.url),
                    )
                finally:
                    await resp.aclose()
