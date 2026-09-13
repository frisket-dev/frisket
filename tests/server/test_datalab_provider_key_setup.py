"""Datalab inline setup shares encrypted keys, health validation and dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.credentials import resolve_credential_with_source
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines_hosted import ocr_datalab
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities
from tests.document_conversion_helpers import bound_document_converter


@pytest.fixture
def setup_client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATALAB_API_KEY", raising=False)
    requests = []

    def health(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://www.datalab.to/api/v1/user_health"
        assert "Authorization" not in request.headers
        accepted = request.headers["X-API-Key"] == "datalab-inline-valid"
        return httpx.Response(200 if accepted else 401, json={"status": "ok"})

    transport_client = httpx.Client(transport=httpx.MockTransport(health))
    original_probe = provider_config.probe_provider
    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda provider, key: original_probe(provider, key, client=transport_client),
    )
    client = TestClient(
        create_app(tmp_path / "ws", router=ModelRouter(use_env_keys=False))
    )
    pid = client.post("/api/projects", json={"name": "Datalab setup"}).json()["id"]
    yield client, pid, requests
    transport_client.close()
    client.close()


def _choice(client, pid, action):
    response = client.post(
        f"/api/projects/{pid}/selector-choices",
        json={
            "schema_version": "frisket.selector_choices_query.v1",
            "subject": {
                "kind": "action",
                "action_id": action,
                "field": "engine",
                "params": {},
            },
        },
    )
    assert response.status_code == 200, response.text
    return next(
        choice
        for group in response.json()["groups"]
        for choice in group["choices"]
        if choice["authored_selection"].get("engine") == "datalab"
    )


def _save(client, pid):
    validated = client.post(
        f"/api/projects/{pid}/provider-keys/validate",
        json={"provider": "datalab", "key": "datalab-inline-valid"},
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["ok"] is True
    assert "datalab-inline-valid" not in validated.text
    saved = client.post(
        f"/api/projects/{pid}/provider-keys",
        json={
            "provider": "datalab",
            "key": "datalab-inline-valid",
            "validation_token": validated.json()["validation_token"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert "datalab-inline-valid" not in saved.text
    return client.app.state.workspace.get(pid)


def test_datalab_inline_setup_validate_save_test_and_resolve(setup_client):
    client, pid, requests = setup_client
    for action in ("media.ocr", "media.to_markdown"):
        choice = _choice(client, pid, action)
        assert choice["status"] == "needs_setup"
        assert choice["setup"]["kind"] == "api_key"
        assert choice["setup"]["provider"] == "datalab"
        scopes = {scope["scope"]: scope for scope in choice["setup"]["scopes"]}
        assert scopes["project"]["can_mutate"] is True
        assert scopes["workspace"]["can_mutate"] is False
        assert scopes["organization"]["can_mutate"] is False

    endpoint = f"/api/projects/{pid}/provider-keys"
    direct = client.post(endpoint, json={"provider": "datalab", "key": "unvalidated"})
    assert direct.status_code == 400
    rejected = client.post(
        endpoint + "/validate", json={"provider": "datalab", "key": "rejected"}
    )
    assert rejected.json()["ok"] is False
    assert "validation_token" not in rejected.json()
    assert client.app.state.workspace.get(pid).provider_key_catalog_rows() == {}

    validated = client.post(
        endpoint + "/validate",
        json={"provider": "datalab", "key": "datalab-inline-valid"},
    )
    unsupported_cap = client.post(
        endpoint,
        json={
            "provider": "datalab",
            "key": "datalab-inline-valid",
            "validation_token": validated.json()["validation_token"],
            "spend_cap_usd": 1,
        },
    )
    assert unsupported_cap.status_code == 400
    assert "datalab-inline-valid" not in unsupported_cap.text
    assert client.app.state.workspace.get(pid).provider_key_catalog_rows() == {}

    project = _save(client, pid)
    tested = client.post(endpoint + "/validate", json={"provider": "datalab"})
    assert tested.json()["ok"] is True
    assert "datalab-inline-valid" not in tested.text
    assert requests[-1].headers["X-API-Key"] == "datalab-inline-valid"
    assert project.secret_plaintext("DATALAB_API_KEY") is None
    credential = resolve_credential_with_source(project, "DATALAB_API_KEY")
    assert credential.value == "datalab-inline-valid"
    assert credential.source == "project_key"
    connection = StaticExecutionTargetProvider(secrets=project, env={}).connection(
        "datalab"
    )
    assert connection.token == credential.value
    for action in ("media.ocr", "media.to_markdown"):
        choice = _choice(client, pid, action)
        assert choice["status"] == "ready"
        assert choice["setup"] is None
        assert "datalab-inline-valid" not in json.dumps(choice)
    model_catalog = provider_config.build_provider_catalog(
        client.app.state.workspace.root, env={}
    )
    assert "datalab" not in {row.get("id") for row in model_catalog["providers"]}
    unsupported = client.put(
        "/api/providers/keys/datalab", json={"key": "datalab-inline-valid"}
    )
    assert unsupported.status_code == 400


@pytest.mark.parametrize("body", [{"status": "down"}, {}, None, "html"])
def test_datalab_health_requires_documented_success_body(body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    ) as client:
        result = provider_config.probe_provider(
            "datalab", "secret-value", client=client
        )
    assert result["ok"] is False
    assert result["reachable"] is True
    assert "secret-value" not in json.dumps(result)


def test_datalab_health_redacts_transport_error():
    def fail(request):
        raise httpx.ConnectError("connection failed with secret-value", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = provider_config.probe_provider(
            "datalab", "secret-value", client=client
        )
    assert result["ok"] is False
    assert "secret-value" not in json.dumps(result)


def test_datalab_keeps_legacy_secret_and_environment_precedence(
    setup_client, monkeypatch
):
    client, pid, _requests = setup_client
    project = client.app.state.workspace.get(pid)
    client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "DATALAB_API_KEY", "value": "legacy-key"},
    )
    assert (
        resolve_credential_with_source(project, "DATALAB_API_KEY").value == "legacy-key"
    )
    _save(client, pid)
    monkeypatch.setenv("DATALAB_API_KEY", "environment-key")
    credential = resolve_credential_with_source(project, "DATALAB_API_KEY")
    assert credential.value == "environment-key"
    assert credential.source == "local"
    assert (
        StaticExecutionTargetProvider(secrets=project).connection("datalab").token
        == credential.value
    )


def test_datalab_setup_respects_permissions_and_network(tmp_path, monkeypatch):
    monkeypatch.delenv("DATALAB_API_KEY", raising=False)
    client = TestClient(
        create_app(
            tmp_path / "ws",
            router=ModelRouter(use_env_keys=False),
            selector_capabilities_for=lambda _request, _pid: SelectorCapabilities(),
        )
    )
    pid = client.post("/api/projects", json={"name": "Read only"}).json()["id"]
    choice = _choice(client, pid, "media.ocr")
    assert not any(scope["can_mutate"] for scope in choice["setup"]["scopes"])
    assert choice["can_run"] is False
    client.app.state.workspace.get(pid).set_network_policy(mode="off")
    choice = _choice(client, pid, "media.ocr")
    assert choice["status"] == "unavailable"
    assert choice["blocker"]["code"] == "project_network_off"
    assert choice["setup"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["ocr", "convert"])
async def test_datalab_dispatch_uses_inline_saved_key(setup_client, tmp_path, engine):
    client, pid, _requests = setup_client
    project = _save(client, pid)
    fixtures = Path(__file__).parents[1] / "fixtures" / "datalab"
    submit = json.loads((fixtures / f"{engine}_submit.json").read_text())
    complete = json.loads((fixtures / f"{engine}_complete.json").read_text())
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "datalab-inline-valid"
        return httpx.Response(
            200, json=submit if request.method == "POST" else complete
        )

    path = tmp_path / ("page.png" if engine == "ocr" else "doc.pdf")
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n bytes" if engine == "ocr" else b"%PDF-1.4 bytes"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = OpContext(project=project, http=http, extras={})
        if engine == "ocr":
            usage = {"cost": 0, "calls": 0}
            result = await ocr_datalab([path], ctx, usage)
            assert result
            assert usage["credential_source"] == "project_key"
        else:
            markdown, meta = await bound_document_converter(
                ctx, engine="datalab"
            )._convert_datalab(path)
            assert markdown
            assert meta["model_calls"][0]["credential_source"] == "project_key"
    assert calls[0].method == "POST"
