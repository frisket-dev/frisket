"""Project Ask follows the project's Allowed services: its model calls and
its public-web steps are decided before any request is sent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.local_provider_config import resolve_local_endpoints
from frisket.server.provider_config import create_local_endpoint
from frisket.server.route_errors import RouteError, register_route_error_handler
from frisket.server.routes.selector_choices import register_selector_choices_routes
from frisket.server.services import project_qa_runner
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.selector_choices import SelectorCapabilities
from frisket.server.workspace import Workspace
from frisket.service_access import (
    AllowedServices,
    ApprovedEndpoint,
    ApprovedEndpoints,
    write_approved_endpoints,
)

ANTHROPIC_MODEL = "anthropic/claude-haiku-4-5"
LOOPBACK_ORIGIN = "http://127.0.0.1:11434"
REMOTE_ORIGIN = "http://ollama.lan.example.test:11434"


class _Calls:
    def __init__(self, tool_call: dict[str, Any] | None = None) -> None:
        self.models: list[str] = []
        self.tool_call = tool_call

    async def complete(self, request: Any, *_args: Any, **_kwargs: Any) -> LLMResponse:
        self.models.append(request.model)
        first = len(self.models) == 1 and self.tool_call is not None
        return LLMResponse(
            content=None if first else "Answer.",
            data=None,
            model=request.model,
            tokens_in=1,
            tokens_out=1,
            cost=0.0,
            tool_calls=[self.tool_call] if first and self.tool_call else [],
        )


def _workspace(
    root: Path,
    setting: AllowedServices,
    approved: tuple[str, ...],
    *,
    origins: tuple[str, ...] = (LOOPBACK_ORIGIN, REMOTE_ORIGIN),
) -> tuple[Workspace, ModelRouter, dict[str, str], _Calls]:
    root.mkdir(parents=True)
    servers = {
        origin: create_local_endpoint(root, name=f"Server {index}", url=origin)
        for index, origin in enumerate(origins)
    }
    endpoints, _notes = resolve_local_endpoints(root)
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "configured"},
        use_env_keys=False,
        cache_mode="off",
        max_retries=0,
        local_endpoints=endpoints,
    )
    calls = _Calls()
    router._adapters["anthropic"].complete = calls.complete
    for endpoint in endpoints:
        router._adapters["ollama"].adapter_for_endpoint(
            endpoint.endpoint_id
        ).complete = calls.complete
    write_approved_endpoints(
        root,
        ApprovedEndpoints(endpoints=tuple(ApprovedEndpoint(host=h) for h in approved)),
    )
    workspace = Workspace(root, router=router)
    workspace.create("Ask", project_id="ask", allowed_services=setting)
    models = {
        origin: f"ollama/@{server.endpoint_id}/llama3"
        for origin, server in servers.items()
    }
    models["provider"] = ANTHROPIC_MODEL
    return workspace, router, models, calls


def _ask(workspace: Workspace, model: str, *, web: bool = False) -> str:
    async def scenario() -> str:
        service = ProjectQAService(workspace)
        thread = await service.create("ask", AskThreadCreate(scope={"kind": "project"}))
        try:
            turn = await service.submit(
                "ask",
                thread["id"],
                AskTurnRequest(
                    request_id=f"ask-{model}-{web}",
                    question="What is in this project?",
                    scope={"kind": "project"},
                    model=model,
                    web=web,
                ),
            )
        except RouteError as refused:
            await service.shutdown()
            return str(refused.content["code"])
        project = workspace.get("ask")
        await asyncio.wait_for(
            asyncio.shield(service._tasks[(project, turn["id"])]), 10
        )
        await service.shutdown()
        return str(ProjectQAStore(project).get_turn(turn["id"])["status"])

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("setting", "approved", "model_key", "allowed"),
    [
        ("this_machine_only", (), "provider", False),
        ("this_machine_only", (), REMOTE_ORIGIN, False),
        ("this_machine_only", (), LOOPBACK_ORIGIN, True),
        ("approved_services", (), "provider", False),
        ("approved_services", ("api.anthropic.com",), "provider", True),
        ("approved_services", (), REMOTE_ORIGIN, True),
        ("all_services", (), "provider", True),
    ],
)
def test_ask_model_is_decided_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: AllowedServices,
    approved: tuple[str, ...],
    model_key: str,
    allowed: bool,
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    workspace, _router, models, calls = _workspace(tmp_path / "ws", setting, approved)
    status = _ask(workspace, models[model_key])
    if allowed:
        assert calls.models, status
    else:
        assert calls.models == []
        assert status == "service_not_allowed"


@pytest.mark.parametrize(
    ("setting", "searched"),
    [
        ("this_machine_only", False),
        ("approved_services", False),
        ("all_services", True),
    ],
)
def test_ask_web_search_needs_all_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: AllowedServices,
    searched: bool,
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    workspace, _router, models, calls = _workspace(tmp_path / "ws", setting, ())
    calls.tool_call = {"name": "search_web", "args": {"query": "news"}, "id": "web"}
    searches: list[str] = []

    async def recording_search(_service: Any, query: str, **_kwargs: Any) -> list:
        searches.append(query)
        return []

    monkeypatch.setattr(project_qa_runner, "_search_with_purchase", recording_search)
    _ask(workspace, models[LOOPBACK_ORIGIN], web=True)
    assert calls.models, "the loopback model must still be called"
    assert bool(searches) is searched


@pytest.mark.parametrize("setting", ["this_machine_only", "approved_services"])
def test_ask_model_picker_agrees_with_the_ask_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: AllowedServices
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    def reachable(_origin: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "reachable": True,
            "protocol": "openai_compatible",
            "models": ["llama3"],
            "auth_status": "ok",
        }

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", reachable)
    workspace, router, models, _calls = _workspace(tmp_path / "ws", setting, ())
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
        "/api/projects/ask/selector-choices",
        json={
            "schema_version": "frisket.selector_choices_query.v1",
            "subject": {"kind": "project_ask", "model": None},
        },
    )
    assert response.status_code == 200, response.text
    choices = {
        choice["authored_selection"].get("model"): choice
        for group in response.json()["groups"]
        for choice in group["choices"]
    }
    project = workspace.get("ask")
    for model in (models[LOOPBACK_ORIGIN], models[REMOTE_ORIGIN], ANTHROPIC_MODEL):
        try:
            project_qa_runner._assert_ask_model_allowed(project, router, model, None)
            ask_allows = True
        except project_qa_runner.ServiceRefused:
            ask_allows = False
        assert (choices[model]["status"] == "ready") is ask_allows, (
            model,
            choices[model]["blocker"],
        )


def _submit(workspace: Workspace, model: str | None) -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        service = ProjectQAService(workspace)
        try:
            thread = await service.create(
                "ask", AskThreadCreate(scope={"kind": "project"})
            )
            return await service.submit(
                "ask",
                thread["id"],
                AskTurnRequest(
                    request_id=f"default-{model}",
                    question="What is in this project?",
                    scope={"kind": "project"},
                    model=model,
                ),
            )
        finally:
            await service.shutdown()

    return asyncio.run(scenario())


def test_ask_default_model_is_one_the_setting_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    def reachable(_origin: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "reachable": True,
            "protocol": "openai_compatible",
            "models": ["llama3"],
            "auth_status": "ok",
        }

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", reachable)
    workspace, _router, models, _calls = _workspace(
        tmp_path / "ws", "this_machine_only", ()
    )
    turn = _submit(workspace, None)
    assert turn["model"] == models[LOOPBACK_ORIGIN]


@pytest.mark.parametrize("model_key", ["provider", REMOTE_ORIGIN])
def test_ask_refuses_a_disallowed_model_at_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_key: str
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    workspace, _router, models, calls = _workspace(
        tmp_path / "ws", "this_machine_only", ()
    )
    with pytest.raises(RouteError) as refused:
        _submit(workspace, models[model_key])
    assert refused.value.content["code"] == "service_not_allowed"
    assert calls.models == []


def test_ask_refuses_at_submit_when_no_model_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    def reachable(_origin: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "reachable": True,
            "protocol": "openai_compatible",
            "models": ["llama3"],
            "auth_status": "ok",
        }

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", reachable)
    workspace, _router, _models, calls = _workspace(
        tmp_path / "ws", "this_machine_only", (), origins=(REMOTE_ORIGIN,)
    )
    with pytest.raises(RouteError) as refused:
        _submit(workspace, None)
    assert refused.value.content["code"] == "service_not_allowed"
    assert calls.models == []


def test_ask_rechecks_the_model_before_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tightening the setting mid-turn stops the next model call."""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    workspace, router, models, calls = _workspace(tmp_path / "ws", "all_services", ())
    calls.tool_call = {"name": "inspect_sheets", "args": {}, "id": "inspect"}
    first_complete = calls.complete

    async def tightening(request: Any, *args: Any, **kwargs: Any) -> LLMResponse:
        workspace.get("ask").set_allowed_services(
            "this_machine_only", confirm_allowed_services=None
        )
        return await first_complete(request, *args, **kwargs)

    router._adapters["anthropic"].complete = tightening
    status = _ask(workspace, models["provider"])
    assert status == "failed"
    assert calls.models == [models["provider"]]
