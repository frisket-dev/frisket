"""Model servers: where a server runs decides whether its models are free.

A server at a loopback origin is this machine: its models are local and free.
Any other server may forward to a paid provider, so its models are not local
and have an unknown price until the person enters one. Hand-entered models
are stored with the server and offered like listed ones.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from frisket import local_provider_config
from frisket.server import provider_config
from helpers import make_client

REMOTE_ORIGIN = "http://192.168.1.20:8000"
LOOPBACK_ORIGIN = "http://127.0.0.1:11434"


def _probe(origin: str, **_kwargs: Any) -> dict[str, Any]:
    if origin == LOOPBACK_ORIGIN:
        return {
            "reachable": True,
            "status": 200,
            "models": ["qwen3:8b"],
            "detail": None,
            "protocol": "ollama_native",
            "auth_status": "ok",
        }
    if origin == REMOTE_ORIGIN:
        return {
            "reachable": True,
            "status": 200,
            "models": ["listed-model"],
            "detail": None,
            "protocol": "openai_compatible",
            "auth_status": "ok",
        }
    # A thin proxy: answers, but lists no models.
    return {
        "reachable": True,
        "status": 404,
        "models": [],
        "detail": None,
        "protocol": "unknown",
        "auth_status": "unknown",
    }


def _add_server(client, name: str, origin: str) -> dict[str, Any]:
    created = client.post(
        "/api/providers/local-endpoints",
        json={"display_name": name, "origin": origin},
    )
    assert created.status_code == 200, created.text
    return next(
        row
        for row in created.json()["providers"]
        if row["kind"] == "local_http" and row["origin"] == origin
    )


def _server(catalog: dict[str, Any], endpoint_id: str) -> dict[str, Any]:
    return next(
        row
        for row in catalog["providers"]
        if row["kind"] == "local_http" and row["endpoint_id"] == endpoint_id
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "ollama_reachable", _probe)
    return make_client(tmp_path)


def test_only_a_loopback_server_is_local_and_free(client) -> None:
    loopback = _add_server(client, "Desk Ollama", LOOPBACK_ORIGIN)
    remote = _add_server(client, "LAN vLLM", REMOTE_ORIGIN)

    catalog = client.get("/api/providers").json()
    loopback = _server(catalog, loopback["endpoint_id"])
    remote = _server(catalog, remote["endpoint_id"])

    assert loopback["local"] is True
    assert [
        (model["label"], model["local"], model["price"]) for model in loopback["models"]
    ] == [("qwen3:8b", True, {"input": 0.0, "output": 0.0})]
    assert remote["local"] is False
    assert [
        (model["label"], model["local"], model["price"]) for model in remote["models"]
    ] == [("listed-model", False, None)]


def test_hand_entered_models_are_stored_offered_and_removed_with_the_server(
    client,
) -> None:
    proxy = _add_server(client, "Thin proxy", "http://proxy.example.test:4000")
    assert proxy["models"] == []

    patched = client.patch(
        f"/api/providers/local-endpoints/{proxy['endpoint_id']}",
        json={
            "models": [
                {"name": "gpt-4o-mini", "entered": True, "price": None},
                {
                    "name": "claude-sonnet",
                    "entered": True,
                    "price": {"input": 3, "output": 15},
                },
            ]
        },
    )
    assert patched.status_code == 200, patched.text
    server = _server(patched.json(), proxy["endpoint_id"])
    assert [
        (model["id"], model["entered"], model["local"], model["price"])
        for model in server["models"]
    ] == [
        (f"ollama/@{proxy['endpoint_id']}/gpt-4o-mini", True, False, None),
        (
            f"ollama/@{proxy['endpoint_id']}/claude-sonnet",
            True,
            False,
            {"input": 3.0, "output": 15.0},
        ),
    ]
    assert server["configured_models"] == [
        {"name": "gpt-4o-mini", "entered": True, "price": None},
        {
            "name": "claude-sonnet",
            "entered": True,
            "price": {"input": 3.0, "output": 15.0},
        },
    ]

    # Pickers read the project's selector choices; an entered model is a
    # choice exactly like a listed one.
    project_id = client.post("/api/projects", json={"name": "Picker"}).json()["id"]
    choices = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json={
            "schema_version": "frisket.selector_choices_query.v1",
            "subject": {"kind": "project_ask", "model": None},
        },
    )
    assert choices.status_code == 200, choices.text
    offered = {
        choice["authored_selection"]["model"]: choice
        for group in choices.json()["groups"]
        for choice in group["choices"]
        if choice["authored_selection"].get("kind") == "model"
    }
    entered_id = f"ollama/@{proxy['endpoint_id']}/gpt-4o-mini"
    assert entered_id in offered
    assert offered[entered_id]["status"] == "ready"

    deleted = client.delete(f"/api/providers/local-endpoints/{proxy['endpoint_id']}")
    assert deleted.status_code == 200, deleted.text
    stored = local_provider_config.read_config_file(
        client.app.state.workspace.root
    ).get(local_provider_config.LOCAL_MODEL_ENDPOINTS_KEY)
    assert stored is None


def test_a_loopback_server_takes_no_price(client) -> None:
    loopback = _add_server(client, "Desk Ollama", LOOPBACK_ORIGIN)
    refused = client.patch(
        f"/api/providers/local-endpoints/{loopback['endpoint_id']}",
        json={
            "models": [
                {
                    "name": "qwen3:8b",
                    "entered": False,
                    "price": {"input": 1, "output": 1},
                }
            ]
        },
    )
    assert refused.status_code == 400
    assert "free" in refused.text


def test_servers_stored_before_models_existed_keep_working(tmp_path) -> None:
    root = tmp_path / "ws"
    path = local_provider_config.local_secrets_path(root)
    path.parent.mkdir(parents=True)
    old_row = {
        "id": "local-0123456789ab",
        "name": "Desk Ollama",
        "url": LOOPBACK_ORIGIN,
        "inference_token": None,
        "provisioning_token": None,
        "edge_auth": False,
        "pull_enabled": True,
    }
    path.write_text(
        json.dumps(
            {local_provider_config.LOCAL_MODEL_ENDPOINTS_KEY: json.dumps([old_row])}
        )
    )

    (endpoint,), _notes = local_provider_config.resolve_local_endpoints(root, {})
    assert (endpoint.endpoint_id, endpoint.origin, endpoint.pull_enabled) == (
        "local-0123456789ab",
        LOOPBACK_ORIGIN,
        True,
    )
    assert endpoint.models == ()

    provider_config.patch_local_endpoint(
        root, endpoint.endpoint_id, {"display_name": "Desk"}
    )
    (rewritten,) = json.loads(
        local_provider_config.read_config_file(root)[
            local_provider_config.LOCAL_MODEL_ENDPOINTS_KEY
        ]
    )
    assert rewritten == {**old_row, "name": "Desk", "models": []}
