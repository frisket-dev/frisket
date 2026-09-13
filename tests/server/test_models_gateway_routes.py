from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server import provider_config
from frisket.server.routes.models_gateway import register_models_gateway_routes
from frisket.server.services.models_gateway import (
    ModelsGatewayProbeCache,
    ModelsGatewayService,
)


ORIGIN = "https://models.example.test"
TOKEN = "gateway-secret"


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _capabilities() -> dict:
    return {
        "service": "frisket-models",
        "version": "1.2.3",
        "engines": [
            {
                "name": "parakeet-tdt",
                "route": "/v1/transcribe",
                "available": True,
                "loaded": False,
                "models": ["nvidia/parakeet-tdt-0.6b-v3"],
                "error": None,
            }
        ],
        "concurrency": {"limit": 2, "in_flight": 0},
    }


def _client(tmp_path, monkeypatch, handler) -> TestClient:
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    service = ModelsGatewayService.workspace(
        tmp_path,
        transport=_transport(handler),
    )
    app = FastAPI()
    register_models_gateway_routes(app, service=service)
    return TestClient(app)


def test_gateway_metadata_and_connection_repr_do_not_echo_bearer(
    tmp_path, monkeypatch
) -> None:
    from frisket.ai.models.gateway_config import ModelsGatewayConnection

    body = _capabilities()
    body["version"] = TOKEN
    body["engines"][0]["error"] = f"failed with bearer {TOKEN}"
    body["engines"][0]["options"] = {TOKEN: True}
    client = _client(
        tmp_path, monkeypatch, lambda _request: httpx.Response(200, json=body)
    )
    result = client.post(
        "/api/models-gateway/validate", json={"origin": ORIGIN, "token": TOKEN}
    )
    assert result.status_code == 200
    assert result.json()["probe"]["ok"] is True
    assert TOKEN not in result.text
    connection = ModelsGatewayConnection(origin=ORIGIN, token=TOKEN, source="stored")
    assert TOKEN not in repr(connection)


def test_invalid_candidate_request_does_not_echo_submitted_token(
    tmp_path, monkeypatch
) -> None:
    client = _client(
        tmp_path,
        monkeypatch,
        lambda _request: pytest.fail("invalid request must not probe"),
    )
    response = client.post("/api/models-gateway/validate", json={"token": TOKEN})
    assert response.status_code == 422
    assert TOKEN not in response.text


def test_candidate_validate_receipt_and_atomic_save(tmp_path, monkeypatch) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url == f"{ORIGIN}/capabilities"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json=_capabilities())

    client = _client(tmp_path, monkeypatch, handler)
    validation = client.post(
        "/api/models-gateway/validate",
        json={"origin": f"{ORIGIN}/", "token": TOKEN},
    )
    assert validation.status_code == 200
    body = validation.json()
    assert body["normalized_origin"] == ORIGIN
    assert body["probe"]["ok"] is True
    assert body["probe"]["service"] == "frisket-models"
    assert body["probe"]["engines"][0]["route"] == "/v1/transcribe"

    saved = client.put(
        "/api/models-gateway",
        json={
            "origin": ORIGIN,
            "token": TOKEN,
            "validation_token": body["validation_token"],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["source"] == "stored"
    assert saved.json()["token_configured"] is True
    assert TOKEN not in saved.text
    assert len(seen) == 1  # save reuses the validated cached probe

    raw = json.loads(provider_config.local_secrets_path(tmp_path).read_text())
    stored = json.loads(raw[provider_config.MODELS_GATEWAY_CONFIG_KEY])
    assert stored == {"origin": ORIGIN, "token": TOKEN}


def test_failed_validation_preserves_previous_config(tmp_path, monkeypatch) -> None:
    provider_config.save_local_models_gateway(
        tmp_path, origin="https://working.example.test", token="working-token"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": TOKEN})

    client = _client(tmp_path, monkeypatch, handler)
    response = client.post(
        "/api/models-gateway/validate",
        json={"origin": ORIGIN, "token": TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["probe"] == {
        "ok": False,
        "reachable": True,
        "status": 401,
        "detail": "models gateway rejected authentication",
        "service": None,
        "version": None,
        "engines": [],
    }
    assert TOKEN not in response.text
    resolved = provider_config.resolve_models_gateway(tmp_path, env={})
    assert resolved is not None
    assert resolved.origin == "https://working.example.test"
    assert resolved.token == "working-token"


def test_redirect_wrong_service_and_malformed_engine_fail_closed(
    tmp_path, monkeypatch
) -> None:
    responses = iter(
        [
            httpx.Response(307, headers={"location": "https://elsewhere.test"}),
            httpx.Response(200, json={**_capabilities(), "service": "other"}),
            httpx.Response(
                200,
                json={
                    **_capabilities(),
                    "engines": [{"name": "parakeet-tdt", "route": 3}],
                },
            ),
        ]
    )
    client = _client(tmp_path, monkeypatch, lambda _request: next(responses))
    details = []
    for _ in range(3):
        result = client.post(
            "/api/models-gateway/validate",
            json={"origin": ORIGIN, "token": TOKEN},
        ).json()
        assert result["validation_token"] is None
        details.append(result["probe"]["detail"])
    assert details == [
        "models gateway redirects are not allowed",
        "models gateway service is incompatible",
        "models gateway capabilities are malformed",
    ]


def test_environment_pair_wins_read_only_and_partial_pair_does_not_fall_back(
    tmp_path, monkeypatch
) -> None:
    provider_config.save_local_models_gateway(tmp_path, origin=ORIGIN, token=TOKEN)
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://localhost:9321")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "env-token")

    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://localhost:9321/capabilities"
        return httpx.Response(200, json=_capabilities())

    service = ModelsGatewayService.workspace(tmp_path, transport=_transport(ok))
    status = service.status(can_mutate=True)
    assert status["source"] == "environment"
    assert status["can_mutate"] is False
    assert status["origin"] == "http://localhost:9321"
    with pytest.raises(ValueError, match="environment-owned.*read-only"):
        service.save(origin=ORIGIN, token=TOKEN, validation_token="unused")

    monkeypatch.delenv("FRISKET_MODELS_TOKEN")
    partial = service.status(can_mutate=True)
    assert partial["configured"] is False
    assert partial["source"] == "environment"
    assert partial["error"] == (
        "FRISKET_MODELS_TOKEN is required when FRISKET_MODELS_URL is set"
    )


def test_receipt_binds_scope_normalized_origin_token_and_protocol(
    tmp_path, monkeypatch
) -> None:
    client = _client(
        tmp_path,
        monkeypatch,
        lambda _request: httpx.Response(200, json=_capabilities()),
    )
    receipt = client.post(
        "/api/models-gateway/validate", json={"origin": ORIGIN, "token": TOKEN}
    ).json()["validation_token"]
    for changed in (
        {"origin": "https://changed.example.test", "token": TOKEN},
        {"origin": ORIGIN, "token": "changed-token"},
    ):
        response = client.put(
            "/api/models-gateway",
            json={**changed, "validation_token": receipt},
        )
        assert response.status_code == 400
    assert provider_config.resolve_models_gateway(tmp_path, env={}) is None


def test_passive_status_never_probes_and_recheck_bypasses_cached_probe(
    tmp_path, monkeypatch
) -> None:
    provider_config.save_local_models_gateway(tmp_path, origin=ORIGIN, token=TOKEN)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_capabilities())

    service = ModelsGatewayService.workspace(
        tmp_path,
        transport=_transport(handler),
    )
    passive = service.passive_status(can_mutate=True)
    assert passive["configured"] is True
    assert passive["probe"] is None
    assert calls == 0

    assert service.status(can_mutate=True)["probe"]["ok"] is True
    assert service.status(can_mutate=True)["probe"]["ok"] is True
    assert calls == 1
    assert service.validate_candidate(origin=None, token=None)["probe"]["ok"] is True
    assert calls == 2


@pytest.mark.parametrize("warm_cache", [False, True])
def test_passive_status_returns_while_recheck_loader_is_blocked(
    tmp_path, monkeypatch, warm_cache
) -> None:
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    provider_config.save_local_models_gateway(tmp_path, origin=ORIGIN, token=TOKEN)
    entered = threading.Event()
    release = threading.Event()
    block_loader = False

    def handler(_request: httpx.Request) -> httpx.Response:
        if block_loader:
            entered.set()
            assert release.wait(10), "test did not release the probe loader"
        return httpx.Response(200, json=_capabilities())

    service = ModelsGatewayService.workspace(
        tmp_path,
        transport=_transport(handler),
        cache=ModelsGatewayProbeCache(clock=lambda: 0.0),
    )
    previous_probe = service.status(can_mutate=True)["probe"] if warm_cache else None
    block_loader = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        recheck = executor.submit(service.status, can_mutate=True, recheck=True)
        try:
            assert entered.wait(5), "recheck did not enter the probe loader"
            passive = executor.submit(service.passive_status, can_mutate=True).result(
                timeout=5
            )
            assert not release.is_set()
            assert not recheck.done()
            assert passive["configured"] is True
            assert passive["probe"] == previous_probe
        finally:
            release.set()
        assert recheck.result(timeout=5)["probe"]["ok"] is True


def test_gateway_receipt_expires_and_rejects_another_scope_or_protocol() -> None:
    receipt = provider_config.issue_models_gateway_validation_token(
        "organization:9", ORIGIN, TOKEN, now=100
    )
    assert provider_config.models_gateway_validation_token_is_valid(
        "organization:9", ORIGIN, TOKEN, receipt, now=100
    )
    assert not provider_config.models_gateway_validation_token_is_valid(
        "organization:10", ORIGIN, TOKEN, receipt, now=100
    )
    assert not provider_config.models_gateway_validation_token_is_valid(
        "organization:9", ORIGIN, TOKEN, receipt, protocol="other", now=100
    )
    assert not provider_config.models_gateway_validation_token_is_valid(
        "organization:9",
        ORIGIN,
        TOKEN,
        receipt,
        now=101 + provider_config.VALIDATION_TOKEN_TTL_SECONDS,
    )


def test_candidate_requires_pair_and_https_or_loopback_before_probe(
    tmp_path, monkeypatch
) -> None:
    def must_not_probe(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid candidate reached the network")

    client = _client(tmp_path, monkeypatch, must_not_probe)
    assert (
        client.post("/api/models-gateway/validate", json={"origin": ORIGIN}).status_code
        == 422
    )
    rejected = client.post(
        "/api/models-gateway/validate",
        json={"origin": "http://remote.example.test", "token": TOKEN},
    )
    assert rejected.status_code == 200
    assert rejected.json()["probe"]["ok"] is False
    assert rejected.json()["validation_token"] is None
    assert rejected.json()["probe"]["detail"] == (
        "models gateway origin must use https unless it is loopback"
    )
