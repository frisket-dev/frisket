from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.workspace import Workspace


def _app(
    root: Path,
    *,
    router: ModelRouter | None = None,
    capabilities_for: Callable[..., SelectorCapabilities] | None = None,
) -> tuple[TestClient, Workspace, str]:
    workspace = Workspace(root, router=router, enable_local_model_pull=False)
    project_id = workspace.create("Selector", project_id="selector")["id"]
    app = FastAPI()
    register_route_error_handler(app)
    register_selector_choices_routes(
        app,
        workspace=workspace,
        capabilities_for=(
            capabilities_for or (lambda _request, _pid: SelectorCapabilities())
        ),
    )
    return TestClient(app), workspace, project_id


def _query(subject: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "frisket.selector_choices_query.v1",
        "subject": subject,
    }


def _choices(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [choice for group in payload["groups"] for choice in group["choices"]]


def test_viewer_can_load_an_incomplete_action_draft_without_mutation_authority(
    tmp_path: Path,
) -> None:
    client, _workspace, project_id = _app(tmp_path / "workspace")

    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "map.classify",
                "field": "engine",
                "params": {"engine": "local_semantic"},
            }
        ),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.selector_choices.v1"
    assert payload["project_id"] == project_id
    assert payload["subject"] == {
        "kind": "action",
        "action_id": "map.classify",
        "field": "engine",
    }
    assert "params" not in payload["subject"]
    current = next(choice for choice in _choices(payload) if choice["is_current"])
    assert current["authored_selection"] == {
        "kind": "engine_model",
        "engine": "local_semantic",
        "model": None,
    }
    assert current["status"] == "ready"
    assert current["can_author"] is False
    assert current["can_run"] is False


@pytest.mark.parametrize("override", ["target", "target_id", "execution_target"])
def test_action_subject_rejects_authored_target_overrides(
    tmp_path: Path, override: str
) -> None:
    client, _workspace, project_id = _app(tmp_path / override)
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.ocr",
                "field": "engine",
                "params": {override: "models-gateway"},
            }
        ),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "authored_target_override"


def test_unknown_saved_model_is_returned_as_a_blocked_orphan(tmp_path: Path) -> None:
    client, _workspace, project_id = _app(
        tmp_path / "orphan",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
        ),
    )
    model = "retired-provider/org/nested/model"
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "map.ask",
                "field": "model",
                "params": {"model": model},
            }
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["current_choice_id"] == payload["orphaned_current"]["choice_id"]
    assert payload["orphaned_current"]["authored_selection"] == {
        "kind": "model",
        "model": model,
    }
    assert payload["orphaned_current"]["blocker"]["code"] == "unknown_saved_choice"
    assert payload["orphaned_current"]["can_author"] is False


def test_authoritative_empty_embedding_catalog_stays_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "frisket.server.services.selector_choices.embedding_provider_catalog_payload",
        lambda **_kwargs: {
            "schema_version": "frisket.embedding_provider_catalog.v1",
            "modality": "audio",
            "source_column_type": "audio",
            "providers": [],
        },
    )
    client, _workspace, project_id = _app(tmp_path / "empty")
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "embedding",
                "provider": None,
                "model": None,
                "modality": "audio",
                "source_column_type": "audio",
            }
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["groups"] == []
    assert response.json()["default_choice_id"] is None


def test_provider_setup_carries_backend_owned_scopes_and_never_key_material(
    tmp_path: Path,
) -> None:
    client, _workspace, project_id = _app(
        tmp_path / "provider",
        router=ModelRouter(keys={}, use_env_keys=False),
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
            configure_project_credentials=True,
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "copilot", "model": None}),
    )
    assert response.status_code == 200, response.text
    anthropic = next(
        choice
        for choice in _choices(response.json())
        if choice["authored_selection"]
        == {"kind": "model", "model": "anthropic/claude-haiku-4-5"}
    )
    assert anthropic["status"] == "needs_setup"
    assert anthropic["setup"]["kind"] == "api_key"
    scopes = {row["scope"]: row for row in anthropic["setup"]["scopes"]}
    assert scopes["environment"]["environment_names"] == ["ANTHROPIC_API_KEY"]
    assert scopes["project"]["settings_location"] == "project_ai_providers"
    assert scopes["project"]["can_mutate"] is True
    assert scopes["organization"]["can_mutate"] is False
    rendered = json.dumps(response.json())
    assert "secret" not in rendered


def test_configured_provider_is_runnable_only_with_run_capability(
    tmp_path: Path,
) -> None:
    key = "test-openai-key-must-never-render"
    client, _workspace, project_id = _app(
        tmp_path / "configured",
        router=ModelRouter(keys={"openai": key}, use_env_keys=False),
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "copilot", "model": "openai/gpt-5.6-terra"}),
    )
    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "ready"
    assert selected["can_run"] is True
    assert selected["resolved_target"]["target_id"] == "remote-api:openai"
    assert selected["processing_destination"]["kind"] == "external"
    assert key not in response.text


def test_active_target_alone_controls_engine_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic = {
        "kind": "synthetic.select",
        "input_schema": {
            "type": "object",
            "properties": {"engine": {"type": "string", "default": "dual"}},
        },
        "ui_hints": {
            "semantic_controls": {"engine": "engine"},
            "engines": [
                {
                    "id": "dual",
                    "label": "Dual target",
                    "tier": "sidecar",
                    "available": False,
                    "error": "active target unavailable",
                    "target_id": "active-server",
                    "targets": [
                        {
                            "target_id": "active-server",
                            "available": False,
                            "error": "active target unavailable",
                        },
                        {"target_id": "later-server", "available": True},
                    ],
                }
            ],
        },
    }
    monkeypatch.setattr(
        "frisket.server.services.selector_choices.project_action_catalog_payload_with_launcher_hints",
        lambda *_args, **_kwargs: {
            "schema_version": "frisket.action_catalog.v2",
            "actions": [synthetic],
        },
    )
    client, _workspace, project_id = _app(
        tmp_path / "active",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
            configure_models_gateway=True,
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "synthetic.select",
                "field": "engine",
                "params": {"engine": "dual"},
            }
        ),
    )
    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["resolved_target"]["target_id"] == "active-server"
    assert selected["status"] != "ready"
    assert selected["can_run"] is False


def test_configured_local_endpoint_discovery_is_called_once_and_preserves_model_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def catalog(root: str | Path, **_kwargs: Any) -> dict[str, Any]:
        calls.append(Path(root))
        return {
            "schemaVersion": "frisket.providers.v1",
            "tier": "local",
            "providers": [
                {
                    "endpoint_id": "lab",
                    "label": "Lab server",
                    "kind": "local_http",
                    "read_only": False,
                    "models": [
                        {
                            "id": "ollama/@lab/acme/nested-model",
                            "label": "acme/nested-model",
                            "price": None,
                            "local": True,
                        }
                    ],
                    "reachable": True,
                    "origin": "http://localhost:11434",
                    "authority": "instance",
                    "source": "stored",
                    "detail": None,
                    "protocol": "ollama_native",
                    "auth_status": "ok",
                    "token_configured": False,
                    "provisioning_token_configured": False,
                    "edge_auth": False,
                    "pull_enabled": True,
                }
            ],
        }

    monkeypatch.setattr(
        "frisket.server.services.selector_choices.build_provider_catalog", catalog
    )
    client, workspace, project_id = _app(
        tmp_path / "local-endpoint",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "copilot", "model": "ollama/@lab/acme/nested-model"}),
    )
    assert response.status_code == 200, response.text
    assert calls == [workspace.root]
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["authored_selection"]["model"] == "ollama/@lab/acme/nested-model"
    assert selected["can_run"] is True
