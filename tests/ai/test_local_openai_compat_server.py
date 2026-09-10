"""Generic local OpenAI-compatible server support.

The "ollama" provider slot is ALREADY a generic OpenAI-compatible local
server on the wire — the router builds a plain OpenAICompatAdapter at
``{url}/v1`` (router.py) — but three things are Ollama-specific for no
functional reason:

1. The reachability probe hits Ollama's proprietary ``/api/tags`` only, so
   LM Studio / llama.cpp-server / vLLM (which serve OpenAI-compat
   ``GET /v1/models``) show "reachable" with NO model list.
2. The catalog/UI label says "Ollama".
3. The remediation copy says "Ollama isn't running at <url>".

Every configured server has an ordinary endpoint ID. Model IDs use the strict
``ollama/@<endpoint-id>/<model>`` grammar, including artifact pulls.

DONE means:
- ``ollama_reachable`` falls back to ``GET {url}/v1/models`` (OpenAI-compat)
  when ``/api/tags`` yields no models, parsing ``data[].id`` — an LM Studio
  instance gets a real installed-models list. Ollama's richer ``/api/tags``
  stays preferred when it answers.
- Each catalog row keeps its endpoint identity and is labeled "Local server".
- The remediation copy names the class, not the brand: it starts with
  ``"No local AI server is responding at <url>"``.
"""

from __future__ import annotations

import httpx

from frisket.ai.llm.remediation import OLLAMA_UNREACHABLE, classify_llm_error
from frisket.ai.llm.types import LLMError
from frisket.server import provider_config


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(
            request.url.path, httpx.Response(404, json={"error": "not found"})
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_falls_back_to_openai_compat_models_listing() -> None:
    """LM Studio shape: no /api/tags, but GET /v1/models lists loaded models."""
    client = _client(
        {
            "/v1/models": httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "qwen2.5-7b-instruct"}, {"id": "llava-1.5"}],
                },
            )
        }
    )
    probe = provider_config.ollama_reachable("http://localhost:1234", client=client)

    assert probe["reachable"] is True
    assert probe["models"] == ["qwen2.5-7b-instruct", "llava-1.5"]


def test_probe_prefers_ollama_tags_when_present() -> None:
    client = _client(
        {
            "/api/tags": httpx.Response(
                200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]}
            ),
            "/v1/models": httpx.Response(
                200, json={"data": [{"id": "SHOULD-NOT-WIN"}]}
            ),
        }
    )
    probe = provider_config.ollama_reachable("http://localhost:11434", client=client)

    assert probe["reachable"] is True
    assert probe["models"] == ["qwen3:8b", "llama3.2:3b"]


def test_probe_connect_error_still_means_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable("http://localhost:1234", client=client)

    assert probe["reachable"] is False
    assert probe["models"] == []


def test_probe_connect_error_redacts_only_the_selected_endpoint_token() -> None:
    selected = "CANARY-PROBE-TOKEN"
    unrelated = "UNRELATED-ENDPOINT-TOKEN"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"dial failed with bearer {selected}; unrelated {unrelated}",
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable(
        "http://localhost:1234", client=client, token=selected
    )

    assert probe["reachable"] is False
    assert selected not in probe["detail"]
    assert "[REDACTED]" in probe["detail"]
    assert unrelated in probe["detail"]


def _first_local(catalog: dict) -> dict:
    return next(
        provider
        for provider in catalog["providers"]
        if provider["kind"] == "local_http"
    )


def test_empty_catalog_has_no_synthetic_local_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": False,
            "status": None,
            "models": [],
            "detail": "x",
        },
    )
    catalog = provider_config.build_provider_catalog(tmp_path / "ws", {})
    assert not [p for p in catalog["providers"] if p["kind"] == "local_http"]


# ---------------------------------------------------------------------------
# Capability facts: the probe reports protocol and auth as explicit state,
# never inferred from "which
# endpoint happened to supply models".


def test_probe_reports_native_protocol_for_fresh_empty_ollama() -> None:
    """The fresh-install state the provisioning work exists for: /api/tags
    answers 200 with a schema-valid EMPTY list. That is authoritative native
    evidence — the probe must not wander to /v1/models and misclassify the
    server as generic."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"data": [{"id": "SHOULD-NOT-WIN"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable("http://localhost:11434", client=client)

    assert probe["reachable"] is True
    assert probe["protocol"] == "ollama_native"
    assert probe["auth_status"] == "ok"
    assert probe["models"] == []
    assert "/v1/models" not in calls


def test_probe_reports_openai_compatible_protocol_for_lm_studio_shape() -> None:
    client = _client(
        {
            "/v1/models": httpx.Response(
                200, json={"object": "list", "data": [{"id": "qwen2.5-7b-instruct"}]}
            )
        }
    )
    probe = provider_config.ollama_reachable("http://localhost:1234", client=client)

    assert probe["reachable"] is True
    assert probe["protocol"] == "openai_compatible"
    assert probe["auth_status"] == "ok"
    assert probe["models"] == ["qwen2.5-7b-instruct"]


def test_probe_reports_unauthorized_as_auth_state_not_empty_models() -> None:
    """Behind a bearer-checking front door the probe gets 401 — that is an
    authentication state with its own copy, NOT
    "reachable with no models"."""
    client = _client(
        {
            "/api/tags": httpx.Response(401, json={"error": "unauthorized"}),
            "/v1/models": httpx.Response(401, json={"error": "unauthorized"}),
        }
    )
    probe = provider_config.ollama_reachable("http://localhost:11434", client=client)

    assert probe["reachable"] is True
    assert probe["auth_status"] == "unauthorized"
    assert probe["protocol"] == "unknown"
    assert probe["models"] == []
    assert "auth" in (probe["detail"] or "").lower()


def test_probe_html_200_is_not_native_evidence() -> None:
    """A 200 without the {"models": [...]} shape (an SPA answering every
    path) proves nothing — protocol stays unknown rather than native."""
    client = _client(
        {
            "/api/tags": httpx.Response(200, text="<!doctype html><html></html>"),
            "/v1/models": httpx.Response(200, text="<!doctype html><html></html>"),
        }
    )
    probe = provider_config.ollama_reachable("http://localhost:8080", client=client)

    assert probe["reachable"] is True
    assert probe["protocol"] == "unknown"
    assert probe["models"] == []


def test_probe_connect_error_reports_unknown_protocol_and_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable("http://localhost:1234", client=client)

    assert probe["reachable"] is False
    assert probe["protocol"] == "unknown"
    assert probe["auth_status"] == "unknown"


def test_catalog_reachable_empty_native_server_lists_no_phantom_models(
    tmp_path, monkeypatch
) -> None:
    """Reachable-but-empty must show empty (plus guidance), not the static
    qwen3:8b stub — a copilot defaulting to a phantom model just walks into
    model_not_installed."""
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": True,
            "status": 200,
            "models": [],
            "detail": None,
            "protocol": "ollama_native",
            "auth_status": "ok",
        },
    )
    endpoint = provider_config.create_local_endpoint(
        tmp_path / "ws", name="Local server", url="http://127.0.0.1:11434"
    )
    catalog = provider_config.build_provider_catalog(tmp_path / "ws", {})
    entry = _first_local(catalog)

    assert entry["endpoint_id"] == endpoint.endpoint_id
    assert entry["models"] == []
    assert entry["installed_models"] == []
    assert entry["protocol"] == "ollama_native"
    assert entry["auth_status"] == "ok"


def test_catalog_exposes_probe_facts_for_generic_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": True,
            "status": 200,
            "models": ["llava-1.5"],
            "detail": None,
            "protocol": "openai_compatible",
            "auth_status": "ok",
        },
    )
    endpoint = provider_config.create_local_endpoint(
        tmp_path / "ws", name="Local server", url="http://127.0.0.1:1234"
    )
    catalog = provider_config.build_provider_catalog(tmp_path / "ws", {})
    entry = _first_local(catalog)

    assert entry["protocol"] == "openai_compatible"
    assert [m["id"] for m in entry["models"]] == [
        f"ollama/@{endpoint.endpoint_id}/llava-1.5"
    ]


# ---------------------------------------------------------------------------
# Token-bearing probe + edge-auth enforcement verification. HTTP 200 alone
# never implies the auth boundary exists; the probe must ALSO prove a tokenless
# request is rejected.
# ---------------------------------------------------------------------------


def test_probe_sends_configured_bearer_token() -> None:
    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable(
        "http://localhost:11434", client=client, token="secret-inf-token"
    )

    assert probe["reachable"] is True
    assert probe["token_configured"] is True
    assert seen_auth == ["Bearer secret-inf-token"]


def test_probe_tokenless_call_has_no_authorization_header() -> None:
    """Wire-shape pin: NOT sending a token must not send an empty/placeholder
    Authorization header either — byte-identical to before this stage."""
    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"models": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable("http://localhost:11434", client=client)

    assert probe["token_configured"] is False
    assert seen_auth == [None]


def test_probe_edge_auth_enforced_stays_ok() -> None:
    """A front door that enforces auth: tokened request succeeds, a
    deliberately tokenless follow-up is rejected -> auth_status stays 'ok'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer secret":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable(
        "http://localhost:11434",
        client=client,
        token="secret",
        edge_auth=True,
    )

    assert probe["auth_status"] == "ok"


def test_probe_edge_auth_unenforced_when_tokenless_also_succeeds() -> None:
    """review High #4: a token is configured, edge_auth says a front door is
    supposed to enforce it, but a deliberately-tokenless request ALSO
    succeeds -- the door isn't actually checking anything. This is its own
    loud state, never silently reported as 'ok'."""

    def handler(request: httpx.Request) -> httpx.Response:
        # answers 200 regardless of Authorization -- talking straight to the
        # daemon, or a front door that isn't actually gating this route.
        return httpx.Response(200, json={"models": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable(
        "http://localhost:11434",
        client=client,
        token="secret",
        edge_auth=True,
    )

    assert probe["auth_status"] == "unenforced"
    assert "no token" in (probe["detail"] or "").lower()


def test_probe_edge_auth_skips_verification_without_a_token() -> None:
    """edge_auth alone (no token configured) has nothing to compare a
    tokened call against -- stays whatever the single tokenless probe
    reports, no extra request fired."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"models": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.ollama_reachable(
        "http://localhost:11434", client=client, edge_auth=True
    )

    assert probe["auth_status"] == "ok"
    assert calls.count("/api/tags") == 1


def test_catalog_reports_unenforced_edge_auth(tmp_path) -> None:
    endpoint = provider_config.create_local_endpoint(
        tmp_path,
        name="Token front door",
        url="http://127.0.0.1:11434",
        inference_token="secret",
        provisioning_token=None,
        edge_auth=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    import unittest.mock as mock

    with mock.patch("httpx.Client", return_value=client):
        catalog = provider_config.build_provider_catalog(tmp_path, {})
    entry = _first_local(catalog)

    assert entry["endpoint_id"] == endpoint.endpoint_id
    assert entry["auth_status"] == "unenforced"
    assert entry["token_configured"] is True


def test_remediation_names_the_class_not_the_brand() -> None:
    url = "http://localhost:1234"
    exc = LLMError("transport failed", transport_kind="connect")
    remedied = classify_llm_error(exc, provider="ollama", endpoint_origin=url)

    assert remedied.code == OLLAMA_UNREACHABLE
    assert remedied.message.startswith(f"No local AI server is responding at {url}")
