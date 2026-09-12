from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.jobs import model_pull_store
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.workspace import Workspace


def _app(
    root: Path,
    *,
    router: ModelRouter | None = None,
    capabilities_for: Callable[..., SelectorCapabilities] | None = None,
    execution_router_factory: Callable[[], ModelRouter] | None = None,
) -> tuple[TestClient, Workspace, str]:
    workspace = Workspace(
        root,
        router=router,
        enable_local_model_pull=False,
        execution_router_factory=execution_router_factory,
    )
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


def test_sparse_transcription_options_refuse_on_active_target_without_retargeting(
    tmp_path: Path,
) -> None:
    client, _workspace, project_id = _app(
        tmp_path / "workspace",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.transcribe",
                "field": "engine",
                "params": {"engine": "parakeet-tdt", "diarize": True},
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert current["resolved_target"]["target_id"] == "local-onnx"
    assert current["status"] == "unavailable"
    assert current["blocker"]["code"] == "engine_options_unsupported"
    assert current["can_run"] is False
    assert current["setup"] is None


@pytest.mark.parametrize("field", ["source", "not_a_field"])
def test_non_selector_field_is_rejected_at_registered_route(
    tmp_path: Path, field: str
) -> None:
    client, _workspace, project_id = _app(tmp_path / field)
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {"kind": "action", "action_id": "map.ask", "field": field, "params": {}}
        ),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_selector_field"


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


def test_action_and_copilot_use_their_distinct_effective_routers(
    tmp_path: Path,
) -> None:
    ordinary = ModelRouter(keys={"anthropic": "ordinary-key"}, use_env_keys=False)
    execution = ModelRouter(keys={"openai": "execution-key"}, use_env_keys=False)
    client, _workspace, project_id = _app(
        tmp_path / "routers",
        router=ordinary,
        execution_router_factory=lambda: execution,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_run_actions=True
        ),
    )
    action = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "map.ask",
                "field": "model",
                "params": {"model": "openai/gpt-5.6-terra"},
            }
        ),
    )
    copilot = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "copilot", "model": "openai/gpt-5.6-terra"}),
    )
    assert action.status_code == copilot.status_code == 200
    action_current = next(row for row in _choices(action.json()) if row["is_current"])
    copilot_current = next(row for row in _choices(copilot.json()) if row["is_current"])
    assert action_current["can_run"] is True
    assert copilot_current["can_run"] is False
    assert copilot_current["setup"]["kind"] == "api_key"


def test_catalog_discovers_only_effective_authenticated_local_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = LocalModelEndpointConfig(
        endpoint_id="org",
        display_name="Organization server",
        origin="https://models.example.test",
        source="local_file",
        inference_token="inference-secret",
        edge_auth=True,
    )
    router = ModelRouter(use_env_keys=False, local_endpoints=(endpoint,))
    seen: list[tuple[str, str | None, bool]] = []

    def reachable(
        origin: str, *, token: str | None = None, edge_auth: bool = False
    ) -> dict[str, Any]:
        seen.append((origin, token, edge_auth))
        return {
            "reachable": True,
            "protocol": "openai_compatible",
            "models": ["owner/nested-model"],
            "auth_status": "ok",
        }

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", reachable)
    client, _workspace, project_id = _app(
        tmp_path / "effective-endpoints", router=router
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "copilot", "model": "ollama/@org/owner/nested-model"}),
    )
    assert response.status_code == 200, response.text
    assert seen == [(endpoint.origin, endpoint.inference_token, True)]
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["authored_selection"]["model"] == "ollama/@org/owner/nested-model"
    assert "inference-secret" not in response.text


def test_busy_setup_embeds_actual_operation_without_removing_download_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "frisket.server.services.selector_choices.parakeet_runtime_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "frisket.server.services.selector_choices.parakeet_setup_ready",
        lambda: False,
    )
    client, workspace, project_id = _app(
        tmp_path / "busy",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True, manage_model_downloads=True
        ),
    )
    operation, _ = model_pull_store.create_or_get_active(
        workspace.queue.engine,
        workspace_root=str(workspace.root),
        model_ref="spacy:en_core_web_sm@3.8.0",
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.transcribe",
                "field": "engine",
                "params": {"engine": "parakeet-tdt"},
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["status"] == "needs_setup"
    assert current["setup"]["can_mutate"] is True
    assert current["setup"]["can_start"] is False
    assert current["setup"]["blocked_by_operation"]["id"] == operation.id
    assert current["setup"]["blocked_by_operation"]["status"] == "pending"
    assert current["active_operation"] is None
    ready = client.post(
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
    assert ready.status_code == 200
    assert (
        next(row for row in _choices(ready.json()) if row["is_current"])["can_run"]
        is True
    )
