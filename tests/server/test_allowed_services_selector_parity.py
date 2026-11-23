"""The selector shows a model as ready exactly when Run's gate allows it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import validation
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.local_provider_config import resolve_local_endpoints
from frisket.server.provider_config import create_local_endpoint
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.workspace import Workspace
from frisket.service_access import (
    AllowedServices,
    ApprovedEndpoint,
    ApprovedEndpoints,
    write_approved_endpoints,
)

LOOPBACK_ORIGIN = "http://127.0.0.1:11434"
REMOTE_ORIGIN = "http://ollama.lan.example.test:11434"


def _selector(
    root: Path, setting: AllowedServices, approved: tuple[str, ...]
) -> tuple[TestClient, Workspace, ModelRouter, dict[str, str]]:
    """A workspace whose model servers are configured the way settings save
    them; adding the remote server is what approves it."""
    servers = {
        origin: create_local_endpoint(root, name=name, url=origin)
        for name, origin in (("Here", LOOPBACK_ORIGIN), ("Ollama", REMOTE_ORIGIN))
    }
    endpoints, _notes = resolve_local_endpoints(root)
    router = ModelRouter(
        keys={"openai": "sk-test-not-real"},
        model_call_policy=LOCAL_PURCHASE_POLICY,
        use_env_keys=False,
        local_endpoints=endpoints,
    )
    workspace = Workspace(root, router=router, enable_local_model_pull=False)
    write_approved_endpoints(
        root,
        ApprovedEndpoints(endpoints=tuple(ApprovedEndpoint(host=h) for h in approved)),
    )
    workspace.create("Selector", project_id="selector", allowed_services=setting)
    app = FastAPI()
    register_route_error_handler(app)
    register_selector_choices_routes(
        app,
        workspace=workspace,
        capabilities_for=lambda *_: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    models = {
        origin: f"ollama/@{server.endpoint_id}/llama3"
        for origin, server in servers.items()
    }
    return TestClient(app), workspace, router, models


def _gate_allows(workspace: Workspace, router: ModelRouter, model: str) -> bool:
    project = workspace.get("selector")
    sheet = project.add_sheet(f"data-{len(project.sheets())}")
    cols = {"statement": project.add_column(sheet, "statement")}
    project.add_rows(sheet, [{"statement": "Hello"}], cols)
    request = ActionRequest.model_validate(
        {
            "action_id": "map.classify",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["statement"],
                "engine": "llm",
                "model": model,
                "fields": [
                    {"name": "relevance", "type": "score", "description": "0-10"}
                ],
            },
            "idempotency_key": f"parity-{model}",
        }
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )
    try:
        validation.assert_allowed_services(
            project, plan.program, plan.spec_dict(), router=router, selected_route=None
        )
    except validation.ServiceRefused:
        return False
    return True


@pytest.mark.parametrize(
    ("setting", "approved"),
    [
        ("this_machine_only", ()),
        ("approved_services", ()),
        ("approved_services", ("api.openai.com",)),
        ("all_services", ()),
    ],
)
def test_model_selector_agrees_with_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: AllowedServices,
    approved: tuple[str, ...],
) -> None:
    def reachable(_origin: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "reachable": True,
            "protocol": "openai_compatible",
            "models": ["llama3"],
            "auth_status": "ok",
        }

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", reachable)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    client, workspace, router, models = _selector(tmp_path / "ws", setting, approved)
    response = client.post(
        "/api/projects/selector/selector-choices",
        json={
            "schema_version": "frisket.selector_choices_query.v1",
            "subject": {
                "kind": "action",
                "action_id": "map.ask",
                "field": "model",
                "params": {},
            },
        },
    )
    assert response.status_code == 200, response.text
    choices = {
        choice["authored_selection"].get("model"): choice
        for group in response.json()["groups"]
        for choice in group["choices"]
    }
    openai_model = next(m for m in choices if m and m.startswith("openai/"))
    for model in (models[LOOPBACK_ORIGIN], models[REMOTE_ORIGIN], openai_model):
        gate = _gate_allows(workspace, router, model)
        shown_ready = choices[model]["status"] == "ready"
        assert shown_ready is gate, (model, choices[model]["blocker"])
        if not gate:
            assert choices[model]["blocker"]["code"] == "service_not_allowed"
