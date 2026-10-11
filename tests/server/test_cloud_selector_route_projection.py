from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.cloud_connection import CloudConnectionStore
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudOCROptionSupport,
    CloudSessionResponse,
    CloudUnitPrice,
    CloudUser,
)
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.workspace import Workspace


def test_cloud_first_selector_projects_the_account_billed_route(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "frisket.ops.ocr_engines.rapidocr_available", lambda: (True, None)
    )
    account = CloudAccount(
        id=41,
        name="Investigations",
        kind="organization",
        balance_micro=5_000_000,
        available_micro=4_500_000,
        manage_billing=True,
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(
                id=7, email="reporter@example.test", display_name="Reporter"
            ),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[account],
        ),
    )

    class Client:
        def capabilities(
            self, token: str, account_id: int
        ) -> CloudCapabilitiesResponse:
            assert token == "installation-token"
            assert account_id == 41
            return CloudCapabilitiesResponse(
                account_id=41,
                routes=[
                    CloudCapabilityRoute(
                        capability="ocr",
                        engine="rapidocr",
                        provider="frisket",
                        lane="frisket_credits",
                        route_revision="ocr-route-1",
                        ready=True,
                        blocker=None,
                        price=CloudUnitPrice(
                            kind="unit", unit="page", micro_per_unit=2_500
                        ),
                        options=CloudOCROptionSupport(
                            kind="ocr",
                            language=True,
                            geometry=True,
                            searchable_pdf=False,
                        ),
                    )
                ],
            )

    workspace = Workspace(
        tmp_path / "workspace",
        cloud_connection_store=store,
        cloud_connection_client=Client(),
        enable_local_model_pull=False,
    )
    project_id = workspace.create("Cloud selector", project_id="cloud-selector")["id"]
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    app = FastAPI()
    register_route_error_handler(app)
    register_selector_choices_routes(
        app,
        workspace=workspace,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_author_actions=True, may_run_actions=True
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "media.ocr",
                    "field": "engine",
                    "params": {"engine": "rapidocr"},
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice
        for group in response.json()["groups"]
        for choice in group["choices"]
        if choice["is_current"]
    )
    assert selected["resolved_target"]["target_id"].startswith("frisket-cloud:41:")
    assert selected["execution_route"]["kind"] == "cloud_credits"
    assert selected["execution_route"]["account_id"] == 41
    assert selected["execution_route"]["billed_to"] == "Investigations Cloud credits"
