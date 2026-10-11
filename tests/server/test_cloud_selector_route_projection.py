from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.cloud_connection import CloudConnectionStore
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudLLMOptionSupport,
    CloudOCROptionSupport,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudUnitPrice,
    CloudUser,
)
from frisket.execution.provider import open_execution_composition
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.workspace import Workspace


def _cloud_selector_app(
    tmp_path,
    *,
    routes: list[CloudCapabilityRoute],
    available_micro: int = 4_500_000,
    expires_at: str = "2099-01-01T00:00:00Z",
    selected_account: bool = True,
    connected: bool = True,
    preference: str = "cloud_first",
    local_keys: dict[str, str] | None = None,
    execution_composition_factory=None,
):
    account = CloudAccount(
        id=41,
        name="Investigations",
        kind="organization",
        balance_micro=5_000_000,
        available_micro=available_micro,
        manage_billing=True,
    )
    store = CloudConnectionStore(tmp_path / "profile")
    session = CloudSessionResponse(
        user=CloudUser(id=7, email="reporter@example.test", display_name="Reporter"),
        expires_at=expires_at,
        accounts=[account],
    )
    if connected:
        store.save_connection("installation-token", session)

    class Client:
        origin = "https://cloud.example.test"

        def capabilities(
            self, token: str, account_id: int
        ) -> CloudCapabilitiesResponse:
            assert token == "installation-token"
            assert account_id == 41
            return CloudCapabilitiesResponse(account_id=41, routes=routes)

    workspace = Workspace(
        tmp_path / "workspace",
        cloud_connection_store=store,
        cloud_connection_client=Client(),
        router=(
            ModelRouter(keys=local_keys, use_env_keys=False)
            if local_keys is not None
            else None
        ),
        execution_composition_factory=execution_composition_factory,
        enable_local_model_pull=False,
    )
    project_id = workspace.create("Cloud selector", project_id="cloud-selector")["id"]
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(
            cloud_user_id=7 if selected_account else None,
            cloud_account_id=41 if selected_account else None,
            cloud_execution_preference=preference,
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
    return TestClient(app), workspace, store, project_id


def _choices(payload):
    return [choice for group in payload["groups"] for choice in group["choices"]]


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


def test_project_ask_projects_the_composed_cloud_model_route(
    tmp_path, monkeypatch
) -> None:
    route = CloudCapabilityRoute(
        capability="llm.complete",
        engine="openai/gpt-5-mini",
        provider="openai",
        lane="frisket_credits",
        route_revision="llm-route-1",
        ready=True,
        blocker=None,
        price=CloudTokenPrice(
            kind="tokens",
            input_micro_per_million_tokens=100_000,
            output_micro_per_million_tokens=400_000,
        ),
        options=CloudLLMOptionSupport(
            kind="llm",
            structured_output=True,
            tools=True,
            vision=True,
            max_output_tokens=16_384,
        ),
    )
    client, _workspace, store, project_id = _cloud_selector_app(
        tmp_path, routes=[route]
    )
    original_load = store.load
    profile_loads = 0

    def counted_load():
        nonlocal profile_loads
        profile_loads += 1
        return original_load()

    monkeypatch.setattr(store, "load", counted_load)

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "ready"
    assert selected["can_run"] is True
    assert selected["resolved_target"]["target_id"].startswith("frisket-cloud:41:")
    assert selected["execution_route"] == {
        "kind": "cloud_credits",
        "label": "Frisket Cloud",
        "billed_to": "Investigations Cloud credits",
        "sends_data_to": "Frisket Cloud",
        "account_id": 41,
        "balance_label": "$4.50 balance",
        "balance_url": "https://cloud.example.test/settings/billing?account_id=41",
        "settings_url": "/p/cloud-selector/settings/project/ai-providers",
        "has_alternative_route": True,
    }
    assert profile_loads == 1


def test_exhausted_cloud_credit_route_stays_selected_and_offers_credits(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "frisket.ops.ocr_engines.rapidocr_available", lambda: (True, None)
    )
    route = CloudCapabilityRoute(
        capability="ocr",
        engine="rapidocr",
        provider="frisket",
        lane="frisket_credits",
        route_revision="ocr-route-1",
        ready=False,
        blocker="Add credits to continue.",
        price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2_500),
        options=CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
    )
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[route], available_micro=0
    )

    with client:
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
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["resolved_target"]["target_id"].startswith("frisket-cloud:41:")
    assert selected["status"] == "needs_setup"
    assert selected["can_run"] is False
    assert selected["setup"]["state"] == "out_of_credits"
    assert selected["setup"]["add_credits_url"] == (
        "https://cloud.example.test/settings/billing?account_id=41"
    )


def test_zero_balance_does_not_block_account_key_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "frisket.ops.ocr_engines.rapidocr_available", lambda: (True, None)
    )
    route = CloudCapabilityRoute(
        capability="ocr",
        engine="rapidocr",
        provider="frisket",
        lane="account_key",
        route_revision="ocr-route-1",
        ready=True,
        blocker=None,
        price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=0),
        options=CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
    )
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[route], available_micro=0
    )

    with client:
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
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "ready"
    assert selected["setup"] is None
    assert selected["execution_route"]["kind"] == "cloud_account_key"


def test_connected_installation_without_project_account_requests_account_once(
    tmp_path,
) -> None:
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[], selected_account=False
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "needs_setup"
    assert selected["setup"]["kind"] == "cloud"
    assert selected["setup"]["state"] == "account_required"
    assert selected["setup"]["local_key"]["kind"] == "api_key"


def test_disconnected_installation_offers_cloud_connect_with_local_key_choice(
    tmp_path,
) -> None:
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[], connected=False
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "needs_setup"
    assert selected["setup"]["kind"] == "cloud"
    assert selected["setup"]["state"] == "connect"
    assert selected["setup"]["local_key"]["kind"] == "api_key"


def test_local_first_ready_model_is_not_blocked_by_cloud_route(tmp_path) -> None:
    route = CloudCapabilityRoute(
        capability="llm.complete",
        engine="openai/gpt-5-mini",
        provider="openai",
        lane="frisket_credits",
        route_revision="llm-route-1",
        ready=False,
        blocker="Cloud route unavailable.",
        price=CloudTokenPrice(
            kind="tokens",
            input_micro_per_million_tokens=100_000,
            output_micro_per_million_tokens=400_000,
        ),
        options=CloudLLMOptionSupport(
            kind="llm",
            structured_output=True,
            tools=True,
            vision=True,
            max_output_tokens=16_384,
        ),
    )
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[route],
        preference="local_first",
        local_keys={"openai": "local-openai-key"},
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "ready"
    assert selected["setup"] is None
    assert selected["resolved_target"]["target_id"] == "remote-api:openai"
    assert selected["execution_route"]["kind"] == "local_key"
    assert "local-openai-key" not in response.text


def test_explicit_composition_factory_remains_the_selector_authority(tmp_path) -> None:
    route = CloudCapabilityRoute(
        capability="llm.complete",
        engine="openai/gpt-5-mini",
        provider="openai",
        lane="frisket_credits",
        route_revision="llm-route-1",
        ready=True,
        blocker=None,
        price=CloudTokenPrice(
            kind="tokens",
            input_micro_per_million_tokens=100_000,
            output_micro_per_million_tokens=400_000,
        ),
        options=CloudLLMOptionSupport(
            kind="llm",
            structured_output=True,
            tools=True,
            vision=True,
            max_output_tokens=16_384,
        ),
    )
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[route],
        local_keys={"openai": "composition-owned-key"},
        execution_composition_factory=open_execution_composition,
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "ready"
    assert selected["resolved_target"]["target_id"] == "remote-api:openai"
    assert selected["execution_route"]["kind"] == "local_key"
    assert "composition-owned-key" not in response.text


def test_expired_connection_projects_reconnect_for_known_cloud_route(
    tmp_path,
) -> None:
    route = CloudCapabilityRoute(
        capability="llm.complete",
        engine="openai/gpt-5-mini",
        provider="openai",
        lane="frisket_credits",
        route_revision="llm-route-1",
        ready=True,
        blocker=None,
        price=CloudTokenPrice(
            kind="tokens",
            input_micro_per_million_tokens=100_000,
            output_micro_per_million_tokens=400_000,
        ),
        options=CloudLLMOptionSupport(
            kind="llm",
            structured_output=True,
            tools=True,
            vision=True,
            max_output_tokens=16_384,
        ),
    )
    client, _workspace, store, project_id = _cloud_selector_app(
        tmp_path, routes=[route], expires_at="2020-01-01T00:00:00Z"
    )
    store.save_capabilities(
        CloudCapabilitiesResponse(account_id=41, routes=[route]),
        expected_user_id=7,
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["resolved_target"]["target_id"].startswith("frisket-cloud:41:")
    assert selected["status"] == "needs_setup"
    assert selected["setup"]["state"] == "reconnect"
