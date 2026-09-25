from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMError, ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.jobs import model_pull_store
from frisket.engine.store import Project
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.services.selector_choices_setup import SelectorSetupService
from frisket.server.workspace import Workspace


def _app(
    root: Path,
    *,
    router: ModelRouter | None = None,
    capabilities_for: Callable[..., SelectorCapabilities] | None = None,
    execution_router_factory: Callable[[], ModelRouter] | None = None,
    models_gateway_status_for: Callable[..., Any] | None = None,
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
        models_gateway_status_for=models_gateway_status_for,
    )
    return TestClient(app), workspace, project_id


def _query(subject: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "frisket.selector_choices_query.v1",
        "subject": subject,
    }


def _choices(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [choice for group in payload["groups"] for choice in group["choices"]]


def test_solo_docling_offers_managed_runtime_setup_without_gateway_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_LOCAL_MODELS_URL", "http://127.0.0.1:42117")
    monkeypatch.setenv("FRISKET_LOCAL_MODELS_TOKEN", "private-token")
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: False)
    client, workspace, project_id = _app(
        tmp_path / "workspace",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
            manage_model_downloads=True,
        ),
    )

    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.to_markdown",
                "field": "engine",
                "params": {"engine": "docling"},
            }
        ),
    )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["resolved_target"]["target_id"] == "local-models"
    assert selected["status"] == "needs_setup"
    assert selected["can_run"] is False
    assert selected["setup"] == {
        "kind": "engine_setup",
        "setup_ref": "engine-setup:docling.local@1",
        "scope": "workspace",
        "can_mutate": True,
        "can_start": True,
        "blocked_by_operation": None,
    }

    pull, _created = model_pull_store.create_or_get_active(
        workspace.queue.engine,
        workspace_root=str(workspace.root),
        model_ref="engine-setup:docling.local@1",
    )
    # The install marker may become valid before the supervised server passes
    # its authenticated readiness check. The active durable operation remains
    # selector authority during that window.
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: True)
    during_readiness = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.to_markdown",
                "field": "engine",
                "params": {"engine": "docling"},
            }
        ),
    )
    working = next(
        choice for choice in _choices(during_readiness.json()) if choice["is_current"]
    )
    assert working["status"] == "working"
    assert working["can_run"] is False
    assert working["active_operation"]["id"] == pull.id
    assert working["setup"]["blocked_by_operation"]["id"] == pull.id


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
        json=_query({"kind": "project_ask", "model": None}),
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
        json=_query({"kind": "project_ask", "model": "openai/gpt-5.6-terra"}),
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
        json=_query({"kind": "project_ask", "model": "ollama/@lab/acme/nested-model"}),
    )
    assert response.status_code == 200, response.text
    assert calls == [workspace.root]
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["authored_selection"]["model"] == "ollama/@lab/acme/nested-model"
    assert selected["can_run"] is True


def test_action_and_project_ask_use_their_distinct_effective_routers(
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
    project_ask = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "project_ask", "model": "openai/gpt-5.6-terra"}),
    )
    assert action.status_code == project_ask.status_code == 200
    action_current = next(row for row in _choices(action.json()) if row["is_current"])
    project_ask_current = next(row for row in _choices(project_ask.json()) if row["is_current"])
    assert action_current["can_run"] is True
    assert project_ask_current["can_run"] is False
    assert project_ask_current["setup"]["kind"] == "api_key"


@pytest.mark.parametrize(
    "subject",
    [
        {"kind": "project_ask", "model": "ollama/@org/owner/nested-model"},
        {
            "kind": "action",
            "action_id": "map.ask",
            "field": "model",
            "params": {"model": "ollama/@org/owner/nested-model"},
        },
        {
            "kind": "action",
            "action_id": "map.translate",
            "field": "engine",
            "params": {"engine": "llm", "model": "ollama/@org/owner/nested-model"},
        },
    ],
)
def test_catalog_discovers_only_effective_authenticated_local_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: dict[str, Any]
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
        json=_query(subject),
    )
    assert response.status_code == 200, response.text
    assert seen == [(endpoint.origin, endpoint.inference_token, True)]
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["authored_selection"]["model"] == "ollama/@org/owner/nested-model"
    assert "inference-secret" not in response.text


def test_fixed_engine_selector_does_not_discover_local_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = LocalModelEndpointConfig(
        endpoint_id="offline",
        display_name="Offline model server",
        origin="https://models.example.test",
        source="local_file",
    )
    router = ModelRouter(use_env_keys=False, local_endpoints=(endpoint,))
    probes: list[str] = []

    def unreachable(origin: str, **_kwargs: Any) -> dict[str, Any]:
        probes.append(origin)
        return {"reachable": False, "models": [], "protocol": "unknown"}

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", unreachable)
    client, _workspace, project_id = _app(tmp_path / "fixed-engine", router=router)
    for diarize in (False, True):
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json=_query(
                {
                    "kind": "action",
                    "action_id": "media.transcribe",
                    "field": "engine",
                    "params": {"engine": "parakeet-tdt", "diarize": diarize},
                }
            ),
        )
        assert response.status_code == 200, response.text
        assert _choices(response.json())
    assert probes == []


def test_model_selector_does_not_probe_unrelated_translation_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unrelated_probe() -> bool:
        raise AssertionError("An unrelated translation runtime was probed")

    monkeypatch.setattr(
        "frisket.ops.integrations.opus_mt.runtime_available", unrelated_probe
    )
    monkeypatch.setattr(
        "frisket.ops.integrations.hy_mt2.runtime_available", unrelated_probe
    )
    client, _workspace, project_id = _app(tmp_path / "single-action")
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {"kind": "action", "action_id": "map.ask", "field": "model", "params": {}}
        ),
    )
    assert response.status_code == 200, response.text
    assert _choices(response.json())


@pytest.mark.parametrize(
    "subject",
    [
        {"kind": "action", "action_id": "map.ask", "field": "model", "params": {}},
        {
            "kind": "action",
            "action_id": "media.transcribe",
            "field": "engine",
            "params": {"engine": "parakeet-tdt", "diarize": True},
        },
        {
            "kind": "action",
            "action_id": "map.translate",
            "field": "engine",
            "params": {
                "engine": "opus_mt",
                "source_language": "en",
                "target_language": "es",
            },
        },
    ],
)
def test_single_action_projection_preserves_full_catalog_selector_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: dict[str, Any]
) -> None:
    from frisket.server.action_catalog_hints import (
        project_action_catalog_payload_with_launcher_hints,
    )

    client, _workspace, project_id = _app(tmp_path / "catalog-parity")
    route = f"/api/projects/{project_id}/selector-choices"
    narrow = client.post(route, json=_query(subject))

    def full_catalog(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("action_kinds", None)
        return project_action_catalog_payload_with_launcher_hints(*args, **kwargs)

    monkeypatch.setattr(
        "frisket.server.services.selector_choices.project_action_catalog_payload_with_launcher_hints",
        full_catalog,
    )
    full = client.post(route, json=_query(subject))
    assert narrow.status_code == full.status_code == 200, (narrow.text, full.text)
    assert narrow.json() == full.json()


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


def test_passive_gateway_status_projects_owner_scope_and_mutation_policy(
    tmp_path: Path,
) -> None:
    seen: list[bool] = []

    def status(can_mutate: bool) -> dict[str, Any]:
        seen.append(can_mutate)
        return {
            "configured": True,
            "source": "stored",
            "authority": "organization",
            "can_mutate": False,
            "token_hint": "…hint",
        }

    client, _workspace, project_id = _app(
        tmp_path / "gateway",
        models_gateway_status_for=status,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            configure_models_gateway=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "map.ner",
                "field": "engine",
                "params": {"engine": "gliner"},
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert seen == [True]
    scopes = {row["scope"]: row for row in current["setup"]["scopes"]}
    assert current["setup"]["kind"] == "models_gateway"
    assert scopes["organization"]["configured"] is True
    assert scopes["organization"]["can_mutate"] is False
    assert scopes["organization"]["hint"] == "…hint"
    assert scopes["environment"]["can_mutate"] is False


def test_provider_bound_custom_embedding_id_is_preserved_and_placeholder_cannot_run(
    tmp_path: Path,
) -> None:
    router = ModelRouter(keys={"openrouter": "configured-key"}, use_env_keys=False)
    client, _workspace, project_id = _app(
        tmp_path / "custom-embedding",
        router=router,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    custom = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "embedding",
                "provider": "openrouter",
                "model": "owner/nested/embedding",
                "modality": "text",
            }
        ),
    )
    assert custom.status_code == 200, custom.text
    current = next(row for row in _choices(custom.json()) if row["is_current"])
    assert current["authored_selection"] == {
        "kind": "embedding",
        "provider": "openrouter",
        "model": "owner/nested/embedding",
    }
    assert current["can_run"] is True
    placeholder = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query({"kind": "embedding", "modality": "text"}),
    )
    assert placeholder.status_code == 200
    row = next(
        row
        for row in _choices(placeholder.json())
        if row["authored_selection"]
        == {"kind": "embedding", "provider": "openrouter", "model": ""}
    )
    assert row["can_run"] is False
    assert row["blocker"]["code"] == "custom_model_required"


@pytest.mark.parametrize("installed", [False, True])
def test_opus_canonical_language_list_and_display_target_use_runtime_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    monkeypatch.setattr(
        "frisket.ops.integrations.opus_mt.runtime_available", lambda: True
    )
    monkeypatch.setattr(
        "frisket.ops.integrations.opus_mt.installed_pairs",
        lambda: ["en-es"] if installed else [],
    )
    client, _workspace, project_id = _app(
        tmp_path / "opus",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "map.translate",
                "field": "engine",
                "params": {
                    "engine": "opus_mt",
                    "language": ["en"],
                    "target_language": "Spanish",
                },
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["can_run"] is True
    assert current["status"] == "ready"
    assert (current["setup"] is None) is installed
    if not installed:
        assert current["setup"]["kind"] == "first_use_download"


def test_network_off_preserves_policy_refusal_for_keyless_hosted_engine(
    tmp_path: Path,
) -> None:
    client, workspace, project_id = _app(
        tmp_path / "off",
        router=ModelRouter(use_env_keys=False),
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
            configure_project_credentials=True,
        ),
    )
    workspace.get(project_id).set_network_policy(mode="off")
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.transcribe",
                "field": "engine",
                "params": {"engine": "openai/whisper-1"},
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["status"] == "unavailable"
    assert current["can_author"] is False
    assert current["can_run"] is False
    assert current["setup"] is None
    assert "network" in current["blocker"]["message"].lower()


def test_ambient_embedding_key_does_not_unlock_an_unconfigured_effective_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-not-selected")
    router = ModelRouter(use_env_keys=False)
    assert router.providers() == []
    with pytest.raises(LLMError, match="no embedding backend"):
        asyncio.run(
            router.embed_batch(["example"], model="openai/text-embedding-3-small")
        )
    client, _workspace, project_id = _app(
        tmp_path / "embedding-router",
        router=router,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "embedding",
                "provider": "openai",
                "model": "text-embedding-3-small",
                "modality": "text",
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["can_run"] is False
    assert current["status"] == "needs_setup"
    assert current["blocker"]["code"] == "provider_key_required"


@pytest.mark.parametrize("provider", ["fastembed", "local"])
def test_supported_custom_fastembed_current_uses_owner_registry_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setattr(
        "frisket.ai.embeddings.capabilities._detect_local_text", lambda _env: True
    )
    monkeypatch.setattr(
        "frisket.ai.embeddings.capabilities._fastembed_registry",
        lambda: {"owner/custom-local": {"dim": 614, "size_in_GB": 0.42}},
    )
    client, _workspace, project_id = _app(
        tmp_path / "custom-local",
        router=ModelRouter(use_env_keys=False),
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "embedding",
                "provider": provider,
                "model": "owner/custom-local",
                "modality": "text",
            }
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["orphaned_current"] is None
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["authored_selection"] == {
        "kind": "embedding",
        "provider": provider,
        "model": "owner/custom-local",
    }
    assert current["can_run"] is True
    assert {"kind": "list", "label": "Dimensions", "values": ["614"]} in current[
        "facts"
    ]


def test_ocr_geometry_knob_is_reported_as_selector_dependency(tmp_path: Path) -> None:
    client, _workspace, project_id = _app(tmp_path / "geometry")
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.ocr",
                "field": "engine",
                "params": {},
            }
        ),
    )
    assert response.status_code == 200, response.text
    assert "searchable_pdf" in response.json()["depends_on"]


def test_source_only_opus_draft_defers_language_pair_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "frisket.ops.integrations.opus_mt.runtime_available", lambda: True
    )
    client, _workspace, project_id = _app(
        tmp_path / "source-only",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "map.translate",
                "field": "engine",
                "params": {"engine": "opus_mt", "language": ["en"]},
            }
        ),
    )
    assert response.status_code == 200, response.text
    current = next(row for row in _choices(response.json()) if row["is_current"])
    assert current["status"] == "ready"
    assert current["can_author"] is True
    assert current["blocker"] is None
    assert current["setup"] is None


def test_model_choices_reuse_provider_setup_credential_and_spend_facts_within_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_reads: Counter[str] = Counter()
    source_reads: Counter[str] = Counter()
    spend_reads: Counter[str] = Counter()
    original_setup = SelectorSetupService.api_key
    original_source = ModelRouter.credential_source_for

    def setup(self: SelectorSetupService, **kwargs: Any) -> dict[str, Any]:
        setup_reads[kwargs["provider"]] += 1
        return original_setup(self, **kwargs)

    def source(self: ModelRouter, provider: str) -> str:
        source_reads[provider] += 1
        return original_source(self, provider)

    def spend(_project: Project, provider: str) -> Any:
        spend_reads[provider] += 1
        return SimpleNamespace(over_cap=spend_reads[provider] > 1, cap_enforceable=True)

    monkeypatch.setattr(SelectorSetupService, "api_key", setup)
    monkeypatch.setattr(ModelRouter, "credential_source_for", source)
    monkeypatch.setattr(Project, "provider_spend_state", spend)
    router = ModelRouter(
        keys={"openai": "selected-project-key"},
        key_sources={"openai": "project_key"},
        use_env_keys=False,
    )
    client, _workspace, project_id = _app(
        tmp_path / "provider-facts",
        router=router,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices", json=_query({"kind": "project_ask"})
    )
    assert response.status_code == 200, response.text
    models = _choices(response.json())
    openai = [
        row
        for row in models
        if row["authored_selection"]["model"].startswith("openai/")
    ]
    assert len(openai) > 1
    assert all(row["can_run"] for row in openai)
    assert setup_reads == Counter({"anthropic": 1, "gemini": 1, "openrouter": 1})
    assert source_reads == Counter(
        {"anthropic": 1, "openai": 1, "gemini": 1, "openrouter": 1}
    )
    assert spend_reads == Counter({"openai": 1})
    refreshed = client.post(
        f"/api/projects/{project_id}/selector-choices", json=_query({"kind": "project_ask"})
    )
    assert refreshed.status_code == 200
    refreshed_openai = [
        row
        for row in _choices(refreshed.json())
        if row["authored_selection"]["model"].startswith("openai/")
    ]
    assert all(not row["can_run"] for row in refreshed_openai)
    assert all(
        row["blocker"]["code"] == "provider_spend_cap_exceeded"
        for row in refreshed_openai
    )
    assert setup_reads == Counter({"anthropic": 2, "gemini": 2, "openrouter": 2})
    assert source_reads == Counter(
        {"anthropic": 2, "openai": 2, "gemini": 2, "openrouter": 2}
    )
    assert spend_reads == Counter({"openai": 2})


@pytest.mark.parametrize("failure", ["stale_jobless", "linked_failed"])
def test_viewer_setup_read_projects_terminal_operation_without_store_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from datetime import timedelta

    import sqlalchemy as sa

    monkeypatch.setattr(
        "frisket.server.services.selector_choices.parakeet_runtime_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "frisket.server.services.selector_choices.parakeet_setup_ready", lambda: False
    )
    client, workspace, project_id = _app(tmp_path / failure)
    engine = workspace.queue.engine
    row, _ = model_pull_store.create_or_get_active(
        engine,
        workspace_root=str(workspace.root),
        model_ref="spacy:en_core_web_sm@3.8.0",
    )
    table = model_pull_store.model_pulls_table
    if failure == "stale_jobless":
        with engine.begin() as cx:
            cx.execute(
                table.update()
                .where(table.c.id == row.id)
                .values(
                    created_at=row.created_at
                    - timedelta(
                        seconds=model_pull_store.JOBLESS_STALE_THRESHOLD_SECONDS + 1
                    )
                )
            )
    else:
        job_id = workspace.queue.enqueue("model.pull", {"pull_id": row.id})
        model_pull_store.mark_running(engine, row.id, job_id=job_id)
        workspace.queue.claim("test-worker")
        workspace.queue.fail(job_id, "test-worker", "no handler", retry=False)
    with engine.connect() as cx:
        before = cx.execute(sa.select(table)).mappings().all()
    writes: list[str] = []

    def record_writes(_cx, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record_writes)
    try:
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
    finally:
        sa.event.remove(engine, "before_cursor_execute", record_writes)
    assert response.status_code == 200, response.text
    current = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert current["active_operation"] is None
    assert current["setup"]["blocked_by_operation"] is None
    assert current["setup"]["can_mutate"] is False
    assert writes == []
    with engine.connect() as cx:
        assert cx.execute(sa.select(table)).mappings().all() == before
    terminal = model_pull_store.list_recent(engine, str(workspace.root), persist=False)[
        0
    ]
    assert model_pull_store.to_dto(terminal)["status"] == "failed"
    assert terminal.error_code == (
        "enqueue_failed" if failure == "stale_jobless" else "worker_failed"
    )


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_geocode_auto_keeps_authored_default_and_resolves_own_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool, explicit: bool
) -> None:
    from frisket.execution.definitions import NOMINATIM_TARGET_ID, OPENCAGE_TARGET_ID

    if configured:
        monkeypatch.setenv("OPENCAGE_API_KEY", "geocode-test-key")
    else:
        monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    client, _workspace, project_id = _app(
        tmp_path / "geocode",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "enrich.geocode",
                "field": "engine",
                "params": {"engine": "auto"} if explicit else {},
            }
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    selected = next(choice for choice in _choices(payload) if choice["is_default"])
    assert selected["authored_selection"] == {"kind": "engine", "engine": "auto"}
    assert selected["is_current"] is explicit
    assert payload["orphaned_current"] is None
    assert selected["status"] == "ready"
    assert selected["can_run"] is True
    assert selected["resolved_target"]["target_id"] == (
        OPENCAGE_TARGET_ID if configured else NOMINATIM_TARGET_ID
    )
    assert "geocode-test-key" not in response.text


@pytest.mark.parametrize(
    "scoped_key,provider_key,expected_engine,ready",
    [
        (False, True, "nominatim", True),
        (True, True, "opencage", True),
        (True, False, "opencage", False),
    ],
)
def test_geocode_auto_uses_scoped_owner_without_ready_alternative_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scoped_key: bool,
    provider_key: bool,
    expected_engine: str,
    ready: bool,
) -> None:
    from dataclasses import replace

    from frisket.credentials import ResolvedCredential
    from frisket.execution.credential_use import CredentialUseContext
    from frisket.execution.definitions import (
        NOMINATIM_TARGET_ID,
        OPENCAGE_TARGET_ID,
        StaticExecutionTargetProvider,
    )

    monkeypatch.setenv("OPENCAGE_API_KEY", "ambient-must-not-select-auto")
    client, workspace, project_id = _app(
        tmp_path / "scoped",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    project = workspace.get(project_id)
    router = workspace.action_execution_router_for(project)
    composition = workspace.execution_composition_for(
        project, router, workspace.edition_execution_composition_context_for()
    )

    class ScopedResolver:
        def resolve_action_credential(self, _project, name):
            assert name == "OPENCAGE_API_KEY"
            return (
                ResolvedCredential("scoped-geocode-key", "project_key")
                if scoped_key
                else None
            )

    composition = replace(
        composition,
        credential_use_context=CredentialUseContext(
            cost_posture=composition.credential_use_context.cost_posture,
            credential_resolver=ScopedResolver(),
        ),
        provider=StaticExecutionTargetProvider(
            env={"OPENCAGE_API_KEY": "scoped-geocode-key"} if provider_key else {},
            router=router,
        ),
    )
    monkeypatch.setattr(
        workspace, "execution_composition_for", lambda *_args: composition
    )
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "enrich.geocode",
                "field": "engine",
                "params": {"engine": "auto"},
            }
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    selected = next(choice for choice in _choices(payload) if choice["is_current"])
    assert payload["orphaned_current"] is None
    assert selected["authored_selection"] == {"kind": "engine", "engine": "auto"}
    assert selected["resolved_target"]["target_id"] == (
        OPENCAGE_TARGET_ID if expected_engine == "opencage" else NOMINATIM_TARGET_ID
    )
    assert selected["can_run"] is ready
    assert selected["status"] == ("ready" if ready else "unavailable")
    if not ready:
        assert selected["blocker"]["code"] == "no_live_target"
        assert selected["can_author"] is False
    assert "ambient-must-not-select-auto" not in response.text
    assert "scoped-geocode-key" not in response.text


def test_chandra_selector_preserves_owned_restrictive_license(tmp_path: Path) -> None:
    client, _workspace, project_id = _app(tmp_path / "license")
    response = client.post(
        f"/api/projects/{project_id}/selector-choices",
        json=_query(
            {
                "kind": "action",
                "action_id": "media.to_markdown",
                "field": "engine",
                "params": {},
            }
        ),
    )
    assert response.status_code == 200, response.text
    chandra = next(
        choice
        for choice in _choices(response.json())
        if choice["authored_selection"] == {"kind": "engine", "engine": "chandra"}
    )
    assert {
        "kind": "text",
        "label": "Restrictive license",
        "value": "Modified OpenRAIL-M (Datalab): Free under $2M revenue/funding; use must not compete with Datalab products.",
    } in chandra["facts"]


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("model_size", [None, "base", "small"])
def test_whisper_base_selector_requires_durable_download(
    tmp_path, monkeypatch, prepared, model_size
):
    from frisket.ai.models import artifact_manifest

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(
        "frisket.execution.definitions.faster_whisper_runtime_present", lambda: True
    )
    entry = artifact_manifest.whisper_base_artifact()
    assert entry is not None and entry.hf_snapshot is not None
    snap = entry.hf_snapshot
    if prepared:
        snapshot = (
            tmp_path
            / "hub"
            / f"models--{snap.repo_id.replace('/', '--')}"
            / "snapshots"
            / snap.revision
        )
        snapshot.mkdir(parents=True)
        for filename in snap.files:
            target = snapshot / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"prepared-fixture")
    client, workspace, project_id = _app(
        tmp_path / "workspace",
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True, manage_model_downloads=True
        ),
    )
    params = {"engine": "faster_whisper"}
    if model_size is not None:
        params["model_size"] = model_size
    query = _query(
        {
            "kind": "action",
            "action_id": "media.transcribe",
            "field": "engine",
            "params": params,
        }
    )
    response = client.post(f"/api/projects/{project_id}/selector-choices", json=query)
    assert response.status_code == 200, response.text
    current = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    needs_download = not prepared and model_size in (None, "base")
    assert current["status"] == ("needs_setup" if needs_download else "ready")
    assert current["can_run"] is (not needs_download)
    if needs_download:
        assert current["setup"]["kind"] == "artifact_download"
        assert current["setup"]["setup_ref"] == entry.ref
        assert current["setup"]["can_start"] is True
        pull, _ = model_pull_store.create_or_get_active(
            workspace.queue.engine,
            workspace_root=str(workspace.root),
            model_ref=entry.ref,
        )
        busy = client.post(f"/api/projects/{project_id}/selector-choices", json=query)
        choice = next(row for row in _choices(busy.json()) if row["is_current"])
        assert choice["status"] == "working"
        assert choice["active_operation"]["id"] == pull.id
        assert choice["setup"]["can_start"] is False
    else:
        assert current["setup"] is None
