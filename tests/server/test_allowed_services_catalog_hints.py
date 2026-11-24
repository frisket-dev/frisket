"""The project action catalog marks remote engines Allowed services refuses
with a stable code, so the web hides them instead of showing them disabled."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "ws",
            router=ModelRouter(
                model_call_policy=LOCAL_PURCHASE_POLICY, use_env_keys=False
            ),
        )
    )


def _remote_engines(client: TestClient, pid: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/projects/{pid}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    return [
        engine
        for action in response.json()["actions"]
        for engine in (action.get("ui_hints") or {}).get("engines") or []
        if engine.get("tier") == "remote" and engine.get("id") != "llm"
    ]


@pytest.mark.parametrize("setting", ["this_machine_only", "approved_services"])
def test_restricted_project_codes_its_refused_remote_engines(tmp_path, setting):
    client = _client(tmp_path)
    pid = client.post(
        "/api/projects", json={"name": "Restricted", "allowed_services": setting}
    ).json()["id"]

    engines = _remote_engines(client, pid)

    assert engines
    assert all(engine["available"] is False for engine in engines)
    assert {engine.get("error_code") for engine in engines} == {"service_not_allowed"}


def test_all_services_leaves_remote_engines_uncoded(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Open"}).json()["id"]

    engines = _remote_engines(client, pid)

    assert engines
    assert all("error_code" not in engine for engine in engines)
