from __future__ import annotations

from types import SimpleNamespace

import pytest

from frisket.ai.llm.model_catalog import MODEL_ENTRIES

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.models.gateway_config import ModelsGatewayConnection
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
from frisket.project_settings import (
    ProjectSettings,
    read_project_settings,
    write_project_settings,
)
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.project_qa import register_project_qa_routes
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services.project_qa import ProjectQAService
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
    router: ModelRouter | None = None,
    execution_composition_factory=None,
    models_gateway_connection_resolver=None,
    capabilities: SelectorCapabilities | None = None,
):
    if router is not None and local_keys is not None:
        raise ValueError("pass router or local_keys, not both")
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
        store.save_capabilities(
            CloudCapabilitiesResponse(account_id=41, routes=routes),
            expected_user_id=7,
        )

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
            router
            if router is not None
            else ModelRouter(keys=local_keys, use_env_keys=False)
            if local_keys is not None
            else None
        ),
        execution_composition_factory=execution_composition_factory,
        models_gateway_connection_resolver=models_gateway_connection_resolver,
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
        capabilities_for=lambda _request, _pid: (
            capabilities
            or SelectorCapabilities(
                may_author_actions=True,
                may_run_actions=True,
                configure_workspace_credentials=True,
                configure_project_credentials=True,
                configure_organization_credentials=True,
                manage_model_downloads=True,
                configure_models_gateway=True,
            )
        ),
    )
    return TestClient(app), workspace, store, project_id


def _llm_route() -> CloudCapabilityRoute:
    return CloudCapabilityRoute(
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


def _choices(payload):
    return [choice for group in payload["groups"] for choice in group["choices"]]


@pytest.mark.parametrize("subject_kind", ["action", "project_ask"])
def test_route_choice_does_not_move_default_model_badge(tmp_path, subject_kind):
    first = _llm_route()
    second = first.model_copy(update={"engine": "openai/gpt-5"})
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[first, second], local_keys={}
    )
    subject = (
        {"kind": "action", "action_id": "map.extract", "field": "model", "params": {}}
        if subject_kind == "action"
        else {"kind": "project_ask"}
    )
    query = {"schema_version": "frisket.selector_choices_query.v1", "subject": subject}
    with client:
        before = client.post(f"/api/projects/{project_id}/selector-choices", json=query)
        assert before.status_code == 200, before.text
        default = next(
            choice for choice in _choices(before.json()) if choice["is_default"]
        )
        model = default["authored_selection"]["model"]
        switched = client.patch(
            f"/api/projects/{project_id}/model-routes",
            json={"model_id": model, "route": "own"},
        )
        assert switched.status_code == 200, switched.text
        after = client.post(f"/api/projects/{project_id}/selector-choices", json=query)
    assert after.status_code == 200, after.text
    assert after.json()["default_choice_id"] == before.json()["default_choice_id"]
    current = next(
        choice
        for choice in _choices(after.json())
        if choice["choice_id"] == default["choice_id"]
    )
    assert current["status"] == "needs_setup"
    assert current["can_run"] is False


def test_offered_openrouter_cloud_model_switches_both_directions(tmp_path):
    model = "openrouter/qwen/qwen3-vl-8b-instruct"
    route = _llm_route().model_copy(update={"engine": model, "provider": "openrouter"})
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[route], local_keys={}
    )
    query = {
        "schema_version": "frisket.selector_choices_query.v1",
        "subject": {"kind": "project_ask", "model": model},
    }
    with client:
        for selected in (None, "own", "cloud"):
            if selected is not None:
                changed = client.patch(
                    f"/api/projects/{project_id}/model-routes",
                    json={"model_id": model, "route": selected},
                )
                assert changed.status_code == 200, changed.text
            response = client.post(
                f"/api/projects/{project_id}/selector-choices", json=query
            )
            assert response.status_code == 200, response.text
            current = next(
                choice for choice in _choices(response.json()) if choice["is_current"]
            )
            selection = current["route_selection"]
            assert selection["selected"] == (selected or "cloud")
            cloud = next(
                option for option in selection["options"] if option["id"] == "cloud"
            )
            assert cloud["status"] == "ready"
            assert cloud["disabled_reason"] is None
            assert current["can_run"] is (selected != "own")


@pytest.mark.parametrize("cloud_offered", [False, True])
def test_default_recommendation_respects_own_key_spend_cap(
    tmp_path, monkeypatch, cloud_offered
):
    from frisket.engine.store import Project

    monkeypatch.setattr(
        Project,
        "provider_spend_state",
        lambda _self, _provider: SimpleNamespace(over_cap=True, cap_enforceable=True),
    )
    model = "openai/gpt-5-mini"
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[_llm_route()] if cloud_offered else [],
        router=ModelRouter(
            keys={"openai": "capped-key"},
            key_sources={"openai": "project_key"},
            use_env_keys=False,
        ),
    )
    settings = read_project_settings(workspace.get(project_id))
    write_project_settings(
        workspace.get(project_id),
        settings.model_copy(update={"model_execution_routes": {model: "own"}}),
    )
    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "map.extract",
                    "field": "model",
                    "params": {"model": model},
                },
            },
        )
    assert response.status_code == 200, response.text
    current = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert current["can_run"] is False
    own = next(
        option
        for option in current["route_selection"]["options"]
        if option["id"] == "own"
    )
    assert own["status"] == "unavailable"
    assert own["disabled_reason"]
    assert current["is_default"] is cloud_offered


def test_project_model_route_patch_persists_one_canonical_choice(tmp_path) -> None:
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[_llm_route()],
        local_keys={"openai": "configured-locally"},
    )

    with client:
        response = client.patch(
            f"/api/projects/{project_id}/model-routes",
            json={"model_id": "openai/gpt-5-mini", "route": "own"},
        )
        projected = client.post(
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
    assert response.json() == {
        "model_id": "openai/gpt-5-mini",
        "route": "own",
    }
    settings = read_project_settings(workspace.get(project_id))
    assert settings.model_execution_routes["openai/gpt-5-mini"] == "own"
    selected = next(
        choice for choice in _choices(projected.json()) if choice["is_current"]
    )
    assert selected["route_selection"] == {
        "model_id": "openai/gpt-5-mini",
        "selected": "own",
        "can_mutate": True,
        "options": [
            {
                "id": "cloud",
                "label": "Frisket Cloud",
                "status": "ready",
                "disabled_reason": None,
            },
            {
                "id": "own",
                "label": "Your OpenAI key",
                "status": "ready",
                "disabled_reason": None,
            },
        ],
    }
    assert selected["execution_route"]["credential_label"] == "Workspace key"
    assert selected["configuration_actions"] == []


def test_project_model_route_patch_requires_authoring_capability(tmp_path) -> None:
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[_llm_route()],
        capabilities=SelectorCapabilities(may_run_actions=True),
    )

    with client:
        response = client.patch(
            f"/api/projects/{project_id}/model-routes",
            json={"model_id": "openai/gpt-5-mini", "route": "own"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("local_keys", "own_status"),
    [
        ({}, "needs_setup"),
        ({"openai": "configured-locally"}, "ready"),
    ],
)
def test_selected_missing_cloud_route_never_borrows_own_details(
    tmp_path, local_keys, own_status
) -> None:
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        local_keys=local_keys,
    )
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            model_execution_routes={"openai/gpt-5-mini": "cloud"},
        ),
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
    assert selected["route_selection"]["selected"] == "cloud"
    assert selected["route_selection"]["options"][1]["status"] == own_status
    assert selected["resolved_target"] is None
    assert selected["processing_destination"] == {
        "kind": "unknown",
        "label": "Frisket Cloud",
    }
    assert selected["execution_route"] is None
    assert selected["facts"] == []
    assert selected["status"] == "unavailable"
    assert selected["can_author"] is False
    assert selected["blocker"] == {
        "code": "no_live_target",
        "message": "This model is not currently available through Frisket Cloud.",
        "field": None,
    }
    assert "OpenAI" not in selected["blocker"]["message"]
    assert selected["setup"] is None


def test_nonlocal_own_model_shows_disabled_unsupported_cloud_route(tmp_path) -> None:
    model_id = f"openrouter/{MODEL_ENTRIES['openrouter'][0]['id']}"
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        local_keys={"openrouter": "configured-locally"},
    )
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            model_execution_routes={model_id: "own"},
        ),
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "project_ask", "model": model_id},
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["route_selection"] == {
        "model_id": model_id,
        "selected": "own",
        "can_mutate": True,
        "options": [
            {
                "id": "cloud",
                "label": "Frisket Cloud",
                "status": "unavailable",
                "disabled_reason": ("OpenRouter isn't offered through Frisket Cloud."),
            },
            {
                "id": "own",
                "label": "Your OpenRouter key",
                "status": "ready",
                "disabled_reason": None,
            },
        ],
    }


def test_signed_out_catalog_model_offers_cloud_setup(tmp_path) -> None:
    model_id = f"openrouter/{MODEL_ENTRIES['openrouter'][0]['id']}"
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        connected=False,
        selected_account=False,
        local_keys={"openrouter": "configured-locally"},
    )
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(model_execution_routes={model_id: "own"}),
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "project_ask", "model": model_id},
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["route_selection"]["options"][0] == {
        "id": "cloud",
        "label": "Frisket Cloud",
        "status": "needs_setup",
        "disabled_reason": None,
    }


def test_cloud_first_selector_keeps_local_engine_on_this_computer(
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
    workspace.get(project_id).set_network_policy(mode="off")
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
    assert selected["resolved_target"]["target_id"] == "local"
    assert selected["execution_route"]["kind"] == "local_engine"
    assert selected["execution_route"]["account_id"] is None
    assert selected["execution_route"]["billed_to"] is None


def test_network_off_keeps_exact_cloud_llm_choice_and_refuses_selected_route(
    tmp_path,
) -> None:
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[_llm_route()]
    )
    workspace.get(project_id).set_network_policy(mode="off")

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
    assert selected["execution_route"]["kind"] == "cloud_credits"
    assert selected["status"] == "unavailable"
    assert selected["can_run"] is False
    assert selected["blocker"]["code"] == "project_network_off"
    assert selected["setup"] is None
    assert {
        option["id"]: option["status"]
        for option in selected["route_selection"]["options"]
    } == {"cloud": "unavailable", "own": "unavailable"}


def test_network_off_disables_cloud_setup_before_connection(tmp_path) -> None:
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[], connected=False
    )
    workspace.get(project_id).set_network_policy(mode="off")
    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "project_ask", "model": "openai/gpt-5-mini"},
            },
        )
    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    cloud = next(
        option
        for option in selected["route_selection"]["options"]
        if option["id"] == "cloud"
    )
    assert cloud["status"] == "unavailable"
    assert cloud["disabled_reason"] == "This project's network setting is off."


def test_network_off_refuses_exact_cloud_media_but_keeps_gateway_lan(
    tmp_path,
) -> None:
    def gateway_connection():
        return ModelsGatewayConnection(
            origin="https://models.example.test",
            token="fixture-token",
            source="stored",
        )

    cloud_route = CloudCapabilityRoute(
        capability="ocr",
        engine="paddleocr-vl",
        provider="frisket",
        lane="account_key",
        route_revision="ocr-paddle-route-1",
        ready=True,
        blocker=None,
        price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=0),
        options=CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
    )
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[cloud_route],
        models_gateway_connection_resolver=gateway_connection,
    )
    workspace.get(project_id).set_network_policy(mode="off")

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "media.ocr",
                    "field": "engine",
                    "params": {"engine": "paddleocr-vl"},
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["resolved_target"]["target_id"].startswith("frisket-cloud:41:")
    assert selected["execution_route"]["kind"] == "cloud_account_key"
    assert selected["status"] == "unavailable"
    assert selected["can_run"] is False
    assert selected["blocker"]["code"] == "project_network_off"
    assert {
        option["id"]: option["status"]
        for option in selected["route_selection"]["options"]
    } == {"cloud": "unavailable", "own": "ready"}

    lan_client, lan_workspace, _store, lan_project_id = _cloud_selector_app(
        tmp_path / "lan",
        routes=[cloud_route],
        preference="local_first",
        models_gateway_connection_resolver=gateway_connection,
    )
    lan_workspace.get(lan_project_id).set_network_policy(mode="off")
    with lan_client:
        lan_response = lan_client.post(
            f"/api/projects/{lan_project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "media.ocr",
                    "field": "engine",
                    "params": {"engine": "paddleocr-vl"},
                },
            },
        )

    assert lan_response.status_code == 200, lan_response.text
    lan_selected = next(
        choice for choice in _choices(lan_response.json()) if choice["is_current"]
    )
    # Choosing a route must not move the model into a different catalog group.
    for payload in (response.json(), lan_response.json()):
        group = next(
            group
            for group in payload["groups"]
            if any(choice["is_current"] for choice in group["choices"])
        )
        assert (group["group_id"], group["label"]) == (
            "provider:frisket",
            "Frisket models",
        )
    assert lan_selected["resolved_target"] == {
        "target_id": "models-gateway",
        "operator": "self",
        "egress_class": "operator_lan",
    }
    assert lan_selected["status"] == "ready"
    assert lan_selected["can_run"] is True
    assert {
        option["id"]: option["status"]
        for option in lan_selected["route_selection"]["options"]
    } == {"cloud": "unavailable", "own": "ready"}


def test_cloud_only_project_ask_default_matches_the_persisted_turn(tmp_path) -> None:
    model = "gemini/gemini-3.5-flash-lite"
    route = _llm_route().model_copy(update={"engine": model, "provider": "gemini"})
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[route]
    )
    observed_turn_models: list[str | None] = []

    async def run_turn(_project, _router, turn, _store):
        observed_turn_models.append(turn["model"])
        return {}

    project_qa = ProjectQAService(workspace, runner=run_turn)
    register_project_qa_routes(client.app, service=project_qa)
    client.app.router.add_event_handler("shutdown", project_qa.shutdown)

    with client:
        choices_response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "project_ask"},
            },
        )
        assert choices_response.status_code == 200, choices_response.text
        default = next(
            choice
            for choice in _choices(choices_response.json())
            if choice["is_default"]
        )
        assert default["authored_selection"] == {"kind": "model", "model": model}
        assert default["execution_route"]["kind"] == "cloud_credits"

        threads = f"/api/projects/{project_id}/qa/threads"
        thread_response = client.post(threads, json={"scope": {"kind": "project"}})
        assert thread_response.status_code == 200, thread_response.text
        turn_response = client.post(
            f"{threads}/{thread_response.json()['id']}/turns",
            json={
                "request_id": "cloud-only-default",
                "question": "What changed?",
                "scope": {"kind": "project"},
            },
        )
        assert turn_response.status_code == 200, turn_response.text
        assert turn_response.json()["model"] == model

        explicit_response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "openai/gpt-5-mini",
                },
            },
        )
        assert explicit_response.status_code == 200, explicit_response.text
        explicit = next(
            choice
            for choice in _choices(explicit_response.json())
            if choice["is_current"]
        )
        assert explicit["authored_selection"] == {
            "kind": "model",
            "model": "openai/gpt-5-mini",
        }

    assert observed_turn_models == [model]


@pytest.mark.parametrize(
    "subject, selected_flag",
    [
        ({"kind": "project_ask", "model": "openai/gpt-5-mini"}, "is_current"),
        (
            {
                "kind": "action",
                "action_id": "map.extract",
                "field": "engine",
                "params": {"engine": "llm"},
            },
            "is_default",
        ),
    ],
)
@pytest.mark.parametrize("preference", ["local_first", "cloud_first"])
def test_selector_projects_the_composed_cloud_model_route(
    tmp_path, monkeypatch, subject, selected_flag, preference
) -> None:
    route = _llm_route()
    client, _workspace, store, project_id = _cloud_selector_app(
        tmp_path, routes=[route], preference=preference
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
                "subject": subject,
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice[selected_flag]
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
        "balance_url": "https://cloud.example.test/funding-accounts?account_id=41",
        "settings_url": "/p/cloud-selector/settings/project/ai-providers",
        "has_alternative_route": True,
    }
    assert selected["setup"] is None
    assert selected["configuration_actions"] == []
    assert selected["facts"] == [
        {
            "kind": "rate",
            "label": "Input price",
            "amount": 0.1,
            "currency": "USD",
            "unit": "million input tokens",
            "source_url": None,
            "updated": None,
        },
        {
            "kind": "rate",
            "label": "Output price",
            "amount": 0.4,
            "currency": "USD",
            "unit": "million output tokens",
            "source_url": None,
            "updated": None,
        },
    ]
    assert [option["id"] for option in selected["route_selection"]["options"]] == [
        "cloud",
        "own",
    ]
    assert response.json()["schema_version"] == "frisket.selector_choices.v3"
    assert response.json()["cloud_setup"] is None
    assert profile_loads == 1


def test_selected_suspended_cloud_model_uses_human_blocker_copy(tmp_path) -> None:
    route = _llm_route().model_copy(
        update={"ready": False, "blocker": "account_suspended"}
    )
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[route]
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
    assert selected["status"] == "unavailable"
    assert selected["blocker"] == {
        "code": "account_suspended",
        "message": "The selected Frisket Cloud account is suspended.",
        "field": None,
    }


def test_exhausted_cloud_credit_route_stays_selected_and_offers_credits(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "frisket.ops.ocr_engines.rapidocr_available", lambda: (True, None)
    )
    route = CloudCapabilityRoute(
        capability="ocr",
        engine="datalab",
        provider="frisket",
        lane="frisket_credits",
        route_revision="ocr-route-1",
        ready=False,
        blocker="insufficient_credits",
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
                    "params": {"engine": "datalab"},
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
        "https://cloud.example.test/billing-checkout?account_id=41"
    )
    assert selected["configuration_actions"] == []
    assert selected["blocker"] == {
        "code": "insufficient_credits",
        "message": "Add credits in Frisket Cloud to continue.",
        "field": None,
    }
    assert selected["route_selection"]["options"][0] == {
        "id": "cloud",
        "label": "Frisket Cloud",
        "status": "needs_setup",
        "disabled_reason": None,
    }
    assert selected["route_selection"]["options"][1]["id"] == "own"


def test_zero_balance_does_not_block_account_key_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "frisket.ops.ocr_engines.rapidocr_available", lambda: (True, None)
    )
    route = CloudCapabilityRoute(
        capability="ocr",
        engine="datalab",
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
                    "params": {"engine": "datalab"},
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


def test_missing_cloud_account_key_links_exact_selected_account(tmp_path) -> None:
    route = CloudCapabilityRoute(
        capability="ocr",
        engine="datalab",
        provider="datalab",
        lane="account_key",
        route_revision="ocr-route-1",
        ready=False,
        blocker="account_key_required",
        price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=0),
        options=CloudOCROptionSupport(
            kind="ocr", language=True, geometry=True, searchable_pdf=False
        ),
    )
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path, routes=[route]
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
                    "params": {"engine": "datalab"},
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "needs_setup"
    assert selected["blocker"]["code"] == "account_key_required"
    assert selected["setup"] == {
        "kind": "instructions",
        "title": "Set up key in Frisket Cloud",
        "steps": [
            "Add a Datalab key to the selected Frisket Cloud account, then recheck."
        ],
        "url": "https://cloud.example.test/funding-accounts?account_id=41&provider=datalab",
    }
    assert selected["route_selection"]["options"][0]["status"] == "needs_setup"


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
    payload = response.json()
    selected = next(choice for choice in _choices(payload) if choice["is_current"])
    assert selected["status"] == "needs_setup"
    assert selected["setup"]["kind"] == "cloud"
    assert selected["setup"]["state"] == "account_required"
    assert selected["configuration_actions"] == []
    assert payload["cloud_setup"]["kind"] == "cloud"
    assert payload["cloud_setup"]["state"] == "account_required"
    assert "local_key" not in payload["cloud_setup"]


def test_cloud_first_disconnected_installation_does_not_use_configured_local_key(
    tmp_path,
) -> None:
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        connected=False,
        local_keys={"openai": "must-not-be-used"},
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
    assert selected["setup"]["state"] == "reconnect"
    assert selected["execution_route"] is None
    assert "local_key" not in selected["setup"]
    assert selected["configuration_actions"] == []
    assert selected["route_selection"]["options"][1] == {
        "id": "own",
        "label": "Your OpenAI key",
        "status": "ready",
        "disabled_reason": None,
    }
    assert selected["can_run"] is False
    assert "must-not-be-used" not in response.text


def test_network_off_defaults_ask_and_actions_to_reachable_local_model(
    tmp_path, monkeypatch
) -> None:
    model = "ollama/@desktop/qwen3:8b"
    endpoint = LocalModelEndpointConfig(
        endpoint_id="desktop",
        display_name="Desktop models",
        origin="http://127.0.0.1:11434",
        source="local_file",
    )

    def local_catalog(_root, candidate, _env, *, authority):
        assert candidate == endpoint
        return {
            "endpoint_id": endpoint.endpoint_id,
            "label": endpoint.display_name,
            "kind": "local_http",
            "read_only": False,
            "models": [
                {"id": model, "label": "qwen3:8b", "price": None, "local": True}
            ],
            "reachable": True,
            "origin": endpoint.origin,
            "authority": authority,
            "source": "stored",
            "detail": None,
            "protocol": "ollama_native",
            "auth_status": "ok",
            "token_configured": False,
            "provisioning_token_configured": False,
            "edge_auth": False,
            "pull_enabled": False,
            "installed_models": ["qwen3:8b"],
        }

    monkeypatch.setattr(
        "frisket.server.provider_config.local_endpoint_catalog_entry", local_catalog
    )
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        preference="local_first",
        router=ModelRouter(local_endpoints=(endpoint,), use_env_keys=False),
    )
    workspace.get(project_id).set_network_policy(mode="off")

    async def run_turn(_project, _router, turn, _store):
        return {}

    project_qa = ProjectQAService(workspace, runner=run_turn)
    register_project_qa_routes(client.app, service=project_qa)
    client.app.router.add_event_handler("shutdown", project_qa.shutdown)

    with client:
        for subject in (
            {
                "kind": "action",
                "action_id": "map.extract",
                "field": "engine",
                "params": {"engine": "llm"},
            },
            {"kind": "project_ask"},
        ):
            response = client.post(
                f"/api/projects/{project_id}/selector-choices",
                json={
                    "schema_version": "frisket.selector_choices_query.v1",
                    "subject": subject,
                },
            )
            assert response.status_code == 200, response.text
            default = next(
                choice for choice in _choices(response.json()) if choice["is_default"]
            )
            assert default["authored_selection"]["model"] == model
            assert default["status"] == "ready"
            assert default["can_run"] is True

        threads = f"/api/projects/{project_id}/qa/threads"
        thread = client.post(threads, json={"scope": {"kind": "project"}}).json()
        turn = client.post(
            f"{threads}/{thread['id']}/turns",
            json={
                "request_id": "network-off-local-default",
                "question": "What changed?",
                "scope": {"kind": "project"},
            },
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["model"] == model


def test_omitted_ask_model_refuses_before_admission_when_network_off(tmp_path) -> None:
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        preference="local_first",
        router=ModelRouter(use_env_keys=False),
    )
    workspace.get(project_id).set_network_policy(mode="off")
    project_qa = ProjectQAService(workspace)
    register_project_qa_routes(client.app, service=project_qa)
    client.app.router.add_event_handler("shutdown", project_qa.shutdown)

    with client:
        threads = f"/api/projects/{project_id}/qa/threads"
        thread = client.post(threads, json={"scope": {"kind": "project"}}).json()
        response = client.post(
            f"{threads}/{thread['id']}/turns",
            json={
                "request_id": "network-off-no-default",
                "question": "What changed?",
                "scope": {"kind": "project"},
            },
        )
        assert response.status_code == 409, response.text
        detail = client.get(f"{threads}/{thread['id']}").json()
        assert detail["active_turn"] is None
        assert detail["history"]["events"] == []


def test_omitted_ask_model_uses_selector_eligible_spend_inventory(
    tmp_path, monkeypatch
) -> None:
    def spend(_project, provider):
        return SimpleNamespace(
            over_cap=provider == "anthropic",
            cap_enforceable=True,
        )

    monkeypatch.setattr(
        "frisket.engine.store.Project.provider_spend_state",
        spend,
    )
    router = ModelRouter(
        keys={"anthropic": "capped", "openai": "available"},
        key_sources={"anthropic": "project_key", "openai": "project_key"},
        use_env_keys=False,
    )
    client, workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        preference="local_first",
        router=router,
    )

    async def run_turn(_project, _router, _turn, _store):
        return {}

    project_qa = ProjectQAService(workspace, runner=run_turn)
    register_project_qa_routes(client.app, service=project_qa)
    client.app.router.add_event_handler("shutdown", project_qa.shutdown)

    with client:
        selector = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "project_ask"},
            },
        )
        assert selector.status_code == 200, selector.text
        default = next(
            choice for choice in _choices(selector.json()) if choice["is_default"]
        )
        assert default["authored_selection"]["model"] == "openai/gpt-5.6-terra"

        threads = f"/api/projects/{project_id}/qa/threads"
        thread = client.post(threads, json={"scope": {"kind": "project"}}).json()
        turn = client.post(
            f"{threads}/{thread['id']}/turns",
            json={
                "request_id": "spend-eligible-default",
                "question": "What changed?",
                "scope": {"kind": "project"},
            },
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["model"] == "openai/gpt-5.6-terra"


def test_configuration_actions_require_mutation_authority(tmp_path) -> None:
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[_llm_route()],
        capabilities=SelectorCapabilities(
            may_author_actions=True,
            may_run_actions=True,
        ),
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
    assert selected["configuration_actions"] == []


def test_native_external_action_omits_generic_cloud_setup(tmp_path) -> None:
    client, _workspace, _store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        selected_account=False,
    )

    with client:
        response = client.post(
            f"/api/projects/{project_id}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "enrich.geocode",
                    "field": "engine",
                    "params": {"engine": "auto"},
                },
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["cloud_setup"] is None


def test_cloud_first_different_user_does_not_use_configured_local_key(
    tmp_path,
) -> None:
    client, _workspace, store, project_id = _cloud_selector_app(
        tmp_path,
        routes=[],
        local_keys={"openai": "must-not-be-used"},
    )
    store.save_connection(
        "other-user-token",
        CloudSessionResponse(
            user=CloudUser(
                id=8, email="other@example.test", display_name="Other reporter"
            ),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[
                CloudAccount(
                    id=41,
                    name="Other personal",
                    kind="personal",
                    balance_micro=5_000_000,
                    available_micro=5_000_000,
                    manage_billing=True,
                )
            ],
        ),
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

    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["status"] == "needs_setup"
    assert selected["setup"]["state"] == "account_required"
    assert selected["can_run"] is False
    assert "must-not-be-used" not in response.text


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


@pytest.mark.parametrize("preference", ["local_first", "cloud_first"])
def test_action_default_respects_payer_preference_across_providers(
    tmp_path, preference
):
    client, _workspace, _store, pid = _cloud_selector_app(
        tmp_path,
        routes=[_llm_route()],
        preference=preference,
        local_keys={"gemini": "configured-local-key"},
    )
    with client:
        response = client.post(
            f"/api/projects/{pid}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "map.extract",
                    "field": "engine",
                    "params": {"engine": "llm"},
                },
            },
        )
    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_default"]
    )
    assert selected["status"] == "ready"
    expected_provider = "gemini/" if preference == "local_first" else "openai/"
    assert selected["authored_selection"]["model"].startswith(expected_provider)
    assert selected["execution_route"]["kind"] == (
        "local_key" if preference == "local_first" else "cloud_credits"
    )


def test_blocked_cloud_route_keeps_local_api_key_setup_available(tmp_path):
    route = _llm_route().model_copy(
        update={"ready": False, "blocker": "Model unavailable"}
    )
    client, _workspace, _store, pid = _cloud_selector_app(
        tmp_path,
        routes=[route],
        preference="local_first",
        local_keys={},
    )
    with client:
        response = client.post(
            f"/api/projects/{pid}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "project_ask", "model": "openai/gpt-5-mini"},
            },
        )
    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["can_run"] is False
    assert selected["status"] == "unavailable"
    assert selected["setup"] is None
    assert selected["configuration_actions"] == []
    assert selected["route_selection"]["options"][1]["id"] == "own"


def test_unrelated_cloud_model_is_not_reported_as_an_alternative_route(tmp_path):
    route = _llm_route().model_copy(update={"engine": "openai/gpt-5-mini"})
    client, _workspace, _store, pid = _cloud_selector_app(
        tmp_path,
        routes=[route],
        preference="local_first",
        local_keys={"gemini": "configured-key"},
    )

    with client:
        response = client.post(
            f"/api/projects/{pid}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "project_ask",
                    "model": "gemini/gemini-3.5-flash",
                },
            },
        )

    assert response.status_code == 200, response.text
    selected = next(
        choice for choice in _choices(response.json()) if choice["is_current"]
    )
    assert selected["execution_route"]["kind"] == "local_key"
    assert selected["execution_route"]["has_alternative_route"] is False


def test_embedding_projection_never_invents_cloud_setup(tmp_path):
    client, _workspace, _store, pid = _cloud_selector_app(
        tmp_path,
        routes=[_llm_route()],
        selected_account=False,
    )

    with client:
        response = client.post(
            f"/api/projects/{pid}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {"kind": "embedding"},
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cloud_setup"] is None
    assert all(
        choice["execution_route"] is None
        and all(action["kind"] != "cloud" for action in choice["configuration_actions"])
        for choice in _choices(payload)
    )
