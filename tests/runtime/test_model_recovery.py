"""Explicit setup recovery targets only the failed resident adapter."""

from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from frisket.runtime import model_server


@pytest.mark.parametrize("failed", [False, True])
def test_setup_recovers_failed_engine_once_and_leaves_cold_engines_lazy(
    monkeypatch, failed
):
    requests = []
    entry = {
        "name": "docling",
        "route": "/to-markdown",
        "error": "transient load failure" if failed else None,
        "loaded": False,
    }

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout <= 45
            assert request.get_header("Authorization") == "Bearer test-secret"
            if request.get_method() == "GET":
                result = {"engines": [entry]}
            else:
                location = urlsplit(request.full_url)
                assert location.path == "/engines/docling/retry"
                assert parse_qs(location.query) == {"route": ["/to-markdown"]}
                result = {**entry, "loaded": True, "error": None}
            return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(
        model_server.urllib.request, "build_opener", lambda *_: Opener()
    )
    model_server.retry_failed_engine(
        "http://127.0.0.1:1234",
        "test-secret",
        name="docling",
        route="/to-markdown",
        stopped=lambda: False,
    )
    assert [request.get_method() for request in requests] == (
        ["GET", "POST"] if failed else ["GET"]
    )


def test_cancelled_setup_does_not_retry_a_loader_after_probe(monkeypatch):
    requests = []
    cancelled = False

    class Opener:
        def open(self, request, *, timeout):
            nonlocal cancelled
            requests.append(request.get_method())
            cancelled = True
            return io.BytesIO(
                json.dumps(
                    {
                        "engines": [
                            {
                                "name": "docling",
                                "route": "/to-markdown",
                                "error": "failed",
                            }
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr(
        model_server.urllib.request, "build_opener", lambda *_: Opener()
    )
    model_server.retry_failed_engine(
        "http://127.0.0.1:1234",
        "test-secret",
        name="docling",
        route="/to-markdown",
        stopped=lambda: cancelled,
    )
    assert requests == ["GET"]


@pytest.mark.parametrize("malformed_at", ["GET", "POST"])
def test_malformed_recovery_response_remains_a_typed_setup_failure(
    monkeypatch, malformed_at
):
    class Opener:
        def open(self, request, *, timeout):
            if request.get_method() == malformed_at:
                return io.BytesIO(b"malformed JSON")
            return io.BytesIO(
                json.dumps(
                    {
                        "engines": [
                            {
                                "name": "docling",
                                "route": "/to-markdown",
                                "error": "failed",
                            }
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr(
        model_server.urllib.request, "build_opener", lambda *_: Opener()
    )
    with pytest.raises(RuntimeError, match="invalid"):
        model_server.retry_failed_engine(
            "http://127.0.0.1:1234",
            "test-secret",
            name="docling",
            route="/to-markdown",
            stopped=lambda: False,
        )
