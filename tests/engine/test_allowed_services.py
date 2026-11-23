"""Allowed services: the project setting, its history, and the egress check."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.project_meta import AllowedServicesConfirmationRequired
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.provider_config import (
    create_local_endpoint,
    delete_local_endpoint,
)
from frisket.service_access import (
    ALLOWED_SERVICES_VALUES,
    ApprovedEndpoint,
    ApprovedEndpoints,
    AllowedServices,
    classify_destination,
    write_approved_endpoints,
)

LOOPBACK = LocalModelEndpointConfig(
    endpoint_id="here",
    display_name="This machine",
    origin="http://127.0.0.1:11434",
    source="local_file",
)
# Named like a local server, but on another host.
REMOTE_OLLAMA = LocalModelEndpointConfig(
    endpoint_id="ollama",
    display_name="Ollama",
    origin="http://ollama.lan.example.test:11434",
    source="local_file",
)
NEWSROOM = LocalModelEndpointConfig(
    endpoint_id="newsroom",
    display_name="Newsroom OpenAI-compatible API",
    origin="https://llm.newsroom.example.test",
    source="local_file",
)
MODELS = {
    "this_machine": "ollama/@here/llama3",
    "remote_named_local": "ollama/@ollama/llama3",
    "approved": "ollama/@newsroom/llama3",
    "provider_api": "openai/gpt-5-mini",
}
EXPECTED: dict[AllowedServices, set[str]] = {
    "this_machine_only": {"this_machine"},
    "approved_services": {"this_machine", "approved"},
    "all_services": {"this_machine", "approved", "remote_named_local", "provider_api"},
}


def _router(requests: list[httpx.Request]) -> ModelRouter:
    router = ModelRouter(
        keys={"openai": "sk-test-not-real"},
        use_env_keys=False,
        model_call_policy=LOCAL_PURCHASE_POLICY,
        cache=None,
        cache_mode="off",
        local_endpoints=(LOOPBACK, REMOTE_OLLAMA, NEWSROOM),
    )

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    router._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return router


def _approve_newsroom(project: Project) -> None:
    write_approved_endpoints(
        project.path.parent,
        ApprovedEndpoints(
            endpoints=(
                ApprovedEndpoint(host="llm.newsroom.example.test", label="Newsroom"),
            )
        ),
    )


def _set(project: Project, value: AllowedServices) -> None:
    project.set_allowed_services(value, confirm_allowed_services=value)


def _classify_plan(project: Project, model: str):
    sheet = project.add_sheet("data")
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
            "idempotency_key": f"allowed-services-{model}",
        }
    )
    return build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )


@pytest.mark.parametrize("setting", ALLOWED_SERVICES_VALUES)
@pytest.mark.parametrize("destination", sorted(MODELS))
def test_each_destination_under_each_setting_is_decided_before_any_request(
    project: Project, setting: AllowedServices, destination: str
) -> None:
    _approve_newsroom(project)
    _set(project, setting)
    requests: list[httpx.Request] = []
    runner = MapRunner(
        project, _router(requests), authority=UnroutedOnlyAuthority(project)
    )
    plan = _classify_plan(project, MODELS[destination])

    def gate() -> None:
        validation.assert_network_policy(
            project, plan.program, plan.spec_dict(), router=runner.router
        )

    if destination in EXPECTED[setting]:
        gate()
    else:
        with pytest.raises(validation.ServiceRefused) as refused:
            gate()
        assert refused.value.code == "service_not_allowed"
        assert "Allowed services setting" in str(refused.value)
    assert requests == []


def test_remote_host_named_like_a_local_server_is_not_this_machine() -> None:
    assert classify_destination("http://ollama.lan.example.test:11434", ()) == "other"
    assert classify_destination("http://localhost:11434", ()) == "this_machine"
    assert classify_destination("http://[::1]:8080/v1", ()) == "this_machine"
    assert classify_destination("unix:///run/frisket.sock", ()) == "this_machine"


def test_approval_matches_the_actual_host_and_port_only() -> None:
    approved = (
        ApprovedEndpoint(host="llm.newsroom.example.test"),
        ApprovedEndpoint(host="gpu.example.test", port=8443),
    )
    assert classify_destination("https://llm.newsroom.example.test/v1", approved) == (
        "approved"
    )
    assert classify_destination("https://api.openai.com/v1", approved) == "other"
    assert classify_destination("https://gpu.example.test:8443", approved) == (
        "approved"
    )
    assert classify_destination("https://gpu.example.test", approved) == "other"


def test_approving_a_self_hosted_api_does_not_allow_openai(project: Project) -> None:
    _approve_newsroom(project)
    _set(project, "approved_services")
    requests: list[httpx.Request] = []
    router = _router(requests)
    plan = _classify_plan(project, MODELS["provider_api"])
    with pytest.raises(validation.ServiceRefused) as refused:
        validation.assert_network_policy(
            project, plan.program, plan.spec_dict(), router=router
        )
    assert refused.value.capability == "api.openai.com"
    assert requests == []


def test_external_action_is_refused_at_admission_with_a_stable_code(
    project: Project,
) -> None:
    _set(project, "approved_services")
    sheet = project.add_sheet("data")
    cols = {"statement": project.add_column(sheet, "statement")}
    project.add_rows(sheet, [{"statement": "Hello"}], cols)
    result = run_action_spec(
        project,
        {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"request": {"url": "https://api.example.test/items"}},
            "idempotency_key": "api-allowed-services",
        },
        project_id="api-allowed-services",
    )
    assert result.status == "failed"
    assert result.errors[0].code == "service_not_allowed"
    assert "Approved services" in result.errors[0].message


def test_stricter_change_takes_effect_without_confirmation_or_history(
    project: Project,
) -> None:
    before = len(project.history())
    project.set_allowed_services("this_machine_only", confirm_allowed_services=None)
    assert project.allowed_services() == "this_machine_only"
    assert project.remote_services_refused()
    assert len(project.history()) == before


def test_looser_change_needs_confirmation_and_leaves_history(
    project: Project,
) -> None:
    project.set_allowed_services("this_machine_only", confirm_allowed_services=None)
    with pytest.raises(AllowedServicesConfirmationRequired):
        project.set_allowed_services("all_services", confirm_allowed_services=None)
    with pytest.raises(AllowedServicesConfirmationRequired):
        project.set_allowed_services(
            "all_services", confirm_allowed_services="approved_services"
        )
    assert project.allowed_services() == "this_machine_only"
    project.set_allowed_services(
        "all_services", confirm_allowed_services="all_services"
    )
    assert project.allowed_services() == "all_services"
    entry = project.history()[-1]
    assert entry["kind"] == "allowed_services"
    assert json.loads(entry["spec"]) == {
        "from": "this_machine_only",
        "to": "all_services",
    }
    # The entry changes no data, so Undo passes over it.
    assert entry["change_order"] is None


def test_unreadable_setting_reads_as_this_machine_only(project: Project) -> None:
    project.set_meta("allowed_services", "everything")
    assert project.allowed_services() == "this_machine_only"


def test_manifest_and_metadata_carry_the_setting(project: Project) -> None:
    project.set_allowed_services("approved_services", confirm_allowed_services=None)
    manifest: dict[str, Any] = json.loads((project.path / "manifest.json").read_text())
    assert manifest["allowed_services"] == "approved_services"
    assert project.project_metadata()["allowed_services"] == "approved_services"


def test_a_configured_model_server_is_approved_until_it_is_removed(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    created = create_local_endpoint(
        project.path.parent, name="Ollama", url=REMOTE_OLLAMA.origin
    )
    requests: list[httpx.Request] = []
    router = _router(requests)
    model = f"ollama/@{REMOTE_OLLAMA.endpoint_id}/llama3"
    plan = _classify_plan(project, model)

    def gate() -> None:
        validation.assert_network_policy(
            project, plan.program, plan.spec_dict(), router=router
        )

    _set(project, "approved_services")
    gate()
    _set(project, "this_machine_only")
    with pytest.raises(validation.ServiceRefused):
        gate()
    _set(project, "approved_services")
    assert delete_local_endpoint(project.path.parent, created.endpoint_id)
    with pytest.raises(validation.ServiceRefused):
        gate()
    assert requests == []
