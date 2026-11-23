"""An organization-configured models gateway (Team injects its resolver into
the execution composition) is judged by the origin that composition resolved,
with no FRISKET_MODELS_* environment values."""

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
from frisket.ai.models.gateway_config import ModelsGatewayConnection
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
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

LOOPBACK_GATEWAY = "http://127.0.0.1:9000"
REMOTE_GATEWAY = "https://models.lan.example.test"


@pytest.mark.parametrize(
    ("origin", "setting", "approve", "allowed"),
    [
        (LOOPBACK_GATEWAY, "this_machine_only", False, True),
        (REMOTE_GATEWAY, "approved_services", True, True),
        (REMOTE_GATEWAY, "approved_services", False, False),
        (REMOTE_GATEWAY, "this_machine_only", True, False),
    ],
)
def test_injected_gateway_selector_and_gate_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    setting: AllowedServices,
    approve: bool,
    allowed: bool,
) -> None:
    for name in ("FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    if approve:
        write_approved_endpoints(
            root,
            ApprovedEndpoints(
                endpoints=(ApprovedEndpoint(host="models.lan.example.test"),)
            ),
        )

    def organization_gateway() -> ModelsGatewayConnection:
        return ModelsGatewayConnection(origin=origin, token="org-token", source="stored")

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        use_env_keys=False,
        cache=None,
        cache_mode="off",
    )
    workspace = Workspace(
        root,
        router=router,
        enable_local_model_pull=False,
        models_gateway_connection_resolver=organization_gateway,
    )
    workspace.create("Gateway", project_id="gateway", allowed_services=setting)
    project = workspace.get("gateway")
    sheet = project.add_sheet("data")
    cols = {"doc": project.add_column(sheet, "doc", type="text")}
    project.add_rows(sheet, [{"doc": "<html><body>hi</body></html>"}], cols)

    request = ActionRequest.model_validate(
        {
            "action_id": "media.to_markdown",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "doc", "engine": "docling"},
            "output_names": {
                "markdown": "markdown",
                "ocr_used": "markdown_ocr_used",
            },
            "idempotency_key": "injected-gateway",
        }
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )
    composition = open_execution_composition(
        project,
        router,
        ExecutionCompositionContext.direct(),
        models_gateway_resolver=organization_gateway,
        include_managed_local_models=False,
    )
    runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
    try:
        validation.validate_spec(
            project,
            router,
            runner.run_store,
            plan.spec_dict(),
            program=plan.program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=composition,
        )
        gate_allows = True
    except validation.ServiceRefused:
        gate_allows = False
    assert gate_allows is allowed

    app = FastAPI()
    register_route_error_handler(app)
    register_selector_choices_routes(
        app,
        workspace=workspace,
        capabilities_for=lambda *_: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )
    response = TestClient(app).post(
        "/api/projects/gateway/selector-choices",
        json={
            "schema_version": "frisket.selector_choices_query.v1",
            "subject": {
                "kind": "action",
                "action_id": "media.to_markdown",
                "field": "engine",
                "params": {"engine": "docling"},
            },
        },
    )
    assert response.status_code == 200, response.text
    docling: dict[str, Any] = next(
        choice
        for group in response.json()["groups"]
        for choice in group["choices"]
        if choice["authored_selection"].get("engine") == "docling"
    )
    blocker = docling.get("blocker") or {}
    assert (blocker.get("code") != "service_not_allowed") is allowed, docling
