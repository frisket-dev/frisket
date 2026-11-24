"""With no provider key, a remote model's default route is Frisket Cloud and
it reads as needing setup. Under a restricted Allowed services setting that
route is refused too, so the choice carries the code the web hides it by."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.app import create_app
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities


def _remote_model_choices(tmp_path, setting: str) -> list[dict[str, Any]]:
    client = TestClient(
        create_app(
            tmp_path / "ws",
            router=ModelRouter(
                model_call_policy=LOCAL_PURCHASE_POLICY, use_env_keys=False
            ),
            selector_capabilities_for=lambda _request, _pid: SelectorCapabilities(),
        )
    )
    pid = client.post(
        "/api/projects", json={"name": "Keyless", "allowed_services": setting}
    ).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/selector-choices",
        json={
            "schema_version": "frisket.selector_choices_query.v1",
            "subject": {
                "kind": "action",
                "action_id": "map.classify",
                "field": "engine",
                "params": {},
            },
        },
    )
    assert response.status_code == 200, response.text
    return [
        choice
        for group in response.json()["groups"]
        if group["group_id"] in {"provider:openai", "provider:anthropic"}
        for choice in group["choices"]
        if choice["authored_selection"].get("engine") == "llm"
    ]


def test_keyless_remote_models_need_cloud_setup_under_all_services(tmp_path):
    choices = _remote_model_choices(tmp_path, "all_services")

    assert choices
    assert {choice["blocker"]["code"] for choice in choices} == {
        "frisket_cloud_required"
    }


@pytest.mark.parametrize("setting", ["this_machine_only", "approved_services"])
def test_keyless_remote_models_are_refused_under_restricted_settings(tmp_path, setting):
    choices = _remote_model_choices(tmp_path, setting)

    assert choices
    assert {choice["status"] for choice in choices} == {"unavailable"}
    assert {choice["blocker"]["code"] for choice in choices} == {"service_not_allowed"}
    assert all(choice["setup"] is None for choice in choices)
