"""Unit tests for bounded download retries in prefetch_docling_models.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket

import httpx
from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError
import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "smoke"
    / "prefetch_docling_models.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("prefetch_docling_models", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hf_http_error(
    status_code: int, *, headers: dict[str, str] | None = None
) -> HfHubHTTPError:
    request = httpx.Request("GET", "https://huggingface.co/api/models/test")
    response = httpx.Response(status_code, headers=headers or {}, request=request)
    return HfHubHTTPError(f"HTTP {status_code}", response=response)


def test_retries_transient_dns_connect_error_with_exponential_backoff():
    mod = _load_module()
    calls = 0
    waits: list[float] = []

    def flaky_fetch() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided")

    mod.prefetch_with_retry(flaky_fetch, sleep=waits.append)

    assert calls == 3
    assert waits == [5.0, 15.0]


def test_retries_wrapped_transient_network_error_in_cause_chain():
    mod = _load_module()
    calls = 0
    waits: list[float] = []

    def flaky_fetch() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                raise socket.gaierror(8, "nodename nor servname provided")
            except socket.gaierror as cause:
                raise LocalEntryNotFoundError("cannot reach Hub") from cause

    mod.prefetch_with_retry(flaky_fetch, sleep=waits.append)

    assert calls == 2
    assert waits == [5.0]


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        ConnectionResetError("connection reset by peer"),
        httpx.ReadTimeout("read timed out"),
        _hf_http_error(503),
    ],
)
def test_retries_transient_transport_and_5xx_errors(error: Exception):
    mod = _load_module()
    calls = 0
    waits: list[float] = []

    def flaky_fetch() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error

    mod.prefetch_with_retry(flaky_fetch, sleep=waits.append)

    assert calls == 2
    assert waits == [5.0]


def test_retries_http_429_using_retry_after_header_and_cap():
    mod = _load_module()
    errors = [
        _hf_http_error(429, headers={"Retry-After": "12"}),
        _hf_http_error(429, headers={"Retry-After": "999"}),
    ]
    waits: list[float] = []

    def flaky_fetch() -> None:
        if errors:
            raise errors.pop(0)

    mod.prefetch_with_retry(flaky_fetch, sleep=waits.append)

    assert waits == [12.0, mod.MAX_WAIT_SECONDS]


def test_stops_after_bounded_attempts_on_persistent_network_error():
    mod = _load_module()
    calls = 0
    waits: list[float] = []

    def always_fails() -> None:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided")

    with pytest.raises(httpx.ConnectError):
        mod.prefetch_with_retry(always_fails, sleep=waits.append)

    assert calls == mod.ATTEMPTS == 3
    assert waits == [5.0, 15.0]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("bad pipeline config"),
        FileNotFoundError("missing local file"),
        LocalEntryNotFoundError("offline cache miss without network cause"),
        _hf_http_error(404),
        _hf_http_error(403),
    ],
)
def test_does_not_retry_non_transient_errors(error: Exception):
    mod = _load_module()
    calls = 0
    waits: list[float] = []

    def fails_immediately() -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error)):
        mod.prefetch_with_retry(fails_immediately, sleep=waits.append)

    assert calls == 1
    assert waits == []
