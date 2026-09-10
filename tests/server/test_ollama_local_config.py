"""Canonical local-model endpoint configuration and env authority."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.server.workspace import Workspace
from frisket.engine.store import Project


LOOPBACK = "http://127.0.0.1:11434"
LAN = "http://models.internal:11434"
HTTPS_LAN = "https://models.internal"


def _offline(_origin: str, **_kwargs) -> dict:
    return {
        "reachable": False,
        "status": None,
        "models": [],
        "detail": "not running",
        "protocol": "unknown",
        "auth_status": "unknown",
    }


def test_no_env_and_no_file_is_an_honest_empty_collection(tmp_path) -> None:
    endpoints, notes = provider_config.resolve_local_endpoints(tmp_path, {})
    assert endpoints == ()
    assert notes == []


def test_env_url_materializes_one_origin_derived_read_only_endpoint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(provider_config, "ollama_reachable", _offline)
    env = {"OLLAMA_URL": LOOPBACK}
    endpoints, notes = provider_config.resolve_local_endpoints(tmp_path, env)
    assert notes == []
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.endpoint_id.startswith("env-")
    assert endpoint.origin == LOOPBACK
    assert endpoint.source == "env"

    again, _notes = provider_config.resolve_local_endpoints(tmp_path, dict(env))
    changed, _notes = provider_config.resolve_local_endpoints(
        tmp_path, {"OLLAMA_URL": "http://127.0.0.1:1234"}
    )
    assert again[0].endpoint_id == endpoint.endpoint_id
    assert changed[0].endpoint_id != endpoint.endpoint_id

    local = next(
        row
        for row in provider_config.build_provider_catalog(tmp_path, env)["providers"]
        if row["kind"] == "local_http"
    )
    assert local["endpoint_id"] == endpoint.endpoint_id
    assert local["read_only"] is True
    assert local["origin"] == LOOPBACK
    assert local["authority"] == "instance"
    assert local["source"] == "environment"


@pytest.mark.parametrize(
    "env",
    [
        {"FRISKET_LLM_TOKEN": "missing-origin"},
        {"OLLAMA_URL": "http://evil.example/path", "FRISKET_LLM_TOKEN": "x"},
        {"OLLAMA_URL": LAN, "FRISKET_LLM_TOKEN": "plain-http-secret"},
    ],
)
def test_invalid_env_bundle_is_rejected_as_a_unit(tmp_path, env) -> None:
    endpoints, notes = provider_config.resolve_local_endpoints(tmp_path, env)
    assert endpoints == ()
    assert notes


def test_https_env_bundle_preserves_endpoint_scoped_auth_and_pull(tmp_path) -> None:
    endpoints, notes = provider_config.resolve_local_endpoints(
        tmp_path,
        {
            "OLLAMA_URL": HTTPS_LAN,
            "FRISKET_LLM_TOKEN": "inference-secret",
            "FRISKET_LLM_PROVISIONING_TOKEN": "provisioning-secret",
            "FRISKET_LLM_EDGE_AUTH": "1",
            "FRISKET_ENABLE_MODEL_PULL": "1",
        },
    )
    assert notes == []
    endpoint = endpoints[0]
    assert endpoint.inference_token == "inference-secret"
    assert endpoint.provisioning_token == "provisioning-secret"
    assert endpoint.edge_auth is True
    assert endpoint.pull_enabled is True


def test_persisted_endpoint_patch_preserves_omitted_secrets_and_origin(
    tmp_path,
) -> None:
    endpoint = provider_config.create_local_endpoint(
        tmp_path,
        name="Authenticated Ollama",
        url=LOOPBACK,
        inference_token="inference-secret",
        provisioning_token="provisioning-secret",
        edge_auth=True,
        pull_enabled=True,
    )
    renamed = provider_config.patch_local_endpoint(
        tmp_path, endpoint.endpoint_id, {"display_name": "Renamed"}
    )
    assert renamed.origin == endpoint.origin
    assert renamed.inference_token == "inference-secret"
    assert renamed.provisioning_token == "provisioning-secret"
    assert renamed.edge_auth is True
    assert renamed.pull_enabled is True

    cleared = provider_config.patch_local_endpoint(
        tmp_path, endpoint.endpoint_id, {"inference_token": None}
    )
    assert cleared.inference_token is None
    assert cleared.provisioning_token == "provisioning-secret"


def test_create_rejects_env_origin_collision_without_mutating_file(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", LOOPBACK)
    with pytest.raises(ValueError, match="environment local endpoint"):
        provider_config.create_local_endpoint(tmp_path, name="Duplicate", url=LOOPBACK)
    path = provider_config.local_secrets_path(tmp_path)
    assert not path.exists()


def test_env_endpoint_patch_and_delete_are_explicitly_refused(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", LOOPBACK)
    monkeypatch.setattr(provider_config, "ollama_reachable", _offline)
    endpoint, _notes = provider_config.resolve_env_local_endpoint()
    assert endpoint is not None
    client = TestClient(create_app(tmp_path / "ws"))

    patched = client.patch(
        f"/api/providers/local-endpoints/{endpoint.endpoint_id}",
        json={"display_name": "Cannot rename"},
    )
    deleted = client.delete(f"/api/providers/local-endpoints/{endpoint.endpoint_id}")
    assert patched.status_code == 409
    assert deleted.status_code == 409


def test_hand_edited_noncanonical_store_fails_closed(tmp_path) -> None:
    path = provider_config.local_secrets_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ollama_url": LOOPBACK}))
    endpoints, notes = provider_config.resolve_local_endpoints(tmp_path, {})
    assert endpoints == ()
    assert notes == []


def test_workspace_router_threads_exact_persisted_endpoint_bundle(tmp_path) -> None:
    root = tmp_path / "ws"
    endpoint = provider_config.create_local_endpoint(
        root,
        name="Authenticated Ollama",
        url=LOOPBACK,
        inference_token="inference-secret",
        provisioning_token="provisioning-secret",
        edge_auth=True,
    )
    project = Project.create(tmp_path / "project.frisket", name="project")
    try:
        router = Workspace(root).router_for(project)
    finally:
        project.close()
    assert router.local_endpoints == (endpoint,)
    selected, adapter, bare = router.resolve_local_model(
        f"ollama/@{endpoint.endpoint_id}/qwen3:8b"
    )
    assert selected == endpoint
    assert adapter.api_key == "inference-secret"
    assert bare == "qwen3:8b"
