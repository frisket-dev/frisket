"""Provider setup behavior stays coherent across catalogs, probes and key scopes."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server import provider_config
from frisket.server.app import create_app


PROBES = [
    (
        "anthropic",
        "https://api.anthropic.com/v1/models",
        "x-api-key",
        "",
        {"anthropic-version": "2023-06-01"},
    ),
    ("openai", "https://api.openai.com/v1/models", "authorization", "Bearer ", {}),
    (
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "authorization",
        "Bearer ",
        {},
    ),
    (
        "openrouter",
        "https://openrouter.ai/api/v1/models",
        "authorization",
        "Bearer ",
        {},
    ),
    (
        "opendocrouter",
        "https://www.opendocrouter.ai/v1/credits",
        "authorization",
        "Bearer ",
        {},
    ),
    ("datalab", "https://www.datalab.to/api/v1/user_health", "x-api-key", "", {}),
    ("exa", "https://api.exa.ai/v0/teams/me", "x-api-key", "", {}),
    ("tavily", "https://api.tavily.com/usage", "authorization", "Bearer ", {}),
]


@pytest.mark.parametrize("provider,url,header,prefix,extra", PROBES)
def test_probe_wire_contract(provider, url, header, prefix, extra):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == url
        assert request.headers[header] == prefix + "test-secret"
        for name, value in extra.items():
            assert request.headers[name] == value
        return httpx.Response(200, json={"status": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = provider_config.probe_provider(provider, "test-secret", client=client)
        assert result["ok"] is True
        assert not client.is_closed
    assert len(requests) == 1
    assert "test-secret" not in json.dumps(result)


def test_probe_does_not_follow_redirect_with_injected_client():
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(302, headers={"location": "https://elsewhere.test/"})
            if len(requests) == 1
            else httpx.Response(200)
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        result = provider_config.probe_provider("datalab", "secret", client=client)
    assert len(requests) == 1
    assert not result["ok"]
    assert result["status"] == 302


def test_unknown_provider_does_not_send_key():
    def handler(request):
        pytest.fail("Unknown provider must not make a request")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = provider_config.probe_provider(
            "not-a-provider", "secret", client=client
        )
    assert not result["reachable"] and not result["ok"]


def test_catalogs_reflect_supported_key_scopes(tmp_path):
    with TestClient(
        create_app(tmp_path / "ws", router=ModelRouter(use_env_keys=False))
    ) as client:
        pid = client.post("/api/projects", json={"name": "Keys"}).json()["id"]
        org = client.get("/api/org/provider-catalog")
        assert org.status_code == 200, org.text
        assert [p["id"] for p in org.json()["providers"]] == [
            "anthropic",
            "openai",
            "gemini",
            "openrouter",
            "exa",
            "tavily",
        ]
        project = client.get(f"/api/projects/{pid}/provider-keys")
        assert project.status_code == 200, project.text
        assert [p["id"] for p in project.json()["providers"]] == [
            "anthropic",
            "openai",
            "gemini",
            "openrouter",
            "opendocrouter",
            "datalab",
        ]
        for row in project.json()["providers"]:
            assert row["policy_fields"] == (
                ["spend_cap_usd"] if row["kind"] == "llm" else []
            )


def test_selector_preserves_environment_key_hint(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from frisket.server.services.selector_choices_setup import SelectorSetupService
    from frisket.server.services.selector_choices_capabilities import (
        SelectorCapabilities,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-1234")
    service = SelectorSetupService(SimpleNamespace(root=tmp_path), edition="solo")
    setup = service.api_key(
        project=SimpleNamespace(provider_key_catalog_rows=lambda: {}),
        provider="openai",
        router=None,
        credential_source="local",
        capabilities=SelectorCapabilities(),
    )
    environment = next(
        scope for scope in setup["scopes"] if scope["scope"] == "environment"
    )
    assert environment["configured"] is True
    assert environment["hint"] == "...1234"
    assert "test-secret" not in json.dumps(setup)
