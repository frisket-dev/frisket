"""Adversarial money-path coverage for the project copilot.

The copilot is not a v1 action and does not mint an execution attempt, but it
does make paid provider calls against the project's selected key.  That still
makes two existing project-key promises load-bearing at this direct effect
site: an exhausted cap refuses before egress, and every completed transport is
written to the neutral ``model_calls`` ledger so the cap advances from fact
truth rather than from the HTTP response.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.engine.runner import (
    ProviderSpendCapExceeded,
    ProviderSpendCapUnenforceable,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.project_copilot import (
    ProjectCopilotService,
    ProjectCopilotUpstreamError,
)
from frisket.server.routes.project_research import register_project_copilot_routes
from frisket.team.security.secrets import encrypt_secret, key_hint


VALID_REPLY: dict[str, Any] = {
    "reply": "Here is a plan.",
    "needs_import": False,
    "proposals": [],
}
INVALID_REPLY: dict[str, Any] = {"reply": "missing required fields"}


_ScriptedReply = (
    tuple[dict[str, Any], float | None] | tuple[dict[str, Any], float | None, bool]
)


class _ScriptedAdapter:
    def __init__(self, replies: list[_ScriptedReply]):
        self._replies = list(replies)
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(req)
        scripted = self._replies.pop(0)
        data, cost = scripted[:2]
        cached = bool(scripted[2]) if len(scripted) == 3 else False
        return LLMResponse(
            content=json.dumps(data),
            data=dict(data),
            tokens_in=42,
            tokens_out=17,
            cost=cost,
            model=req.model,
            cached=cached,
        )


class _WorkspaceStub:
    def __init__(self, project: Project, router: ModelRouter):
        self.project = project
        self.router = router

    def get(self, project_id: str) -> Project:
        assert project_id == "project"
        return self.project

    def router_for(self, project: Project) -> ModelRouter:
        assert project is self.project
        return self.router


def _project(tmp_path: Path, *, cap_micro: int | None) -> Project:
    project = Project.create(tmp_path / "copilot-money.frisket", name="Copilot")
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-copilot-test"),
        hint=key_hint("sk-ant-copilot-test"),
        spend_cap_micro=cap_micro,
    )
    return project


def _service(
    project: Project,
    replies: list[_ScriptedReply],
) -> tuple[ProjectCopilotService, _ScriptedAdapter]:
    router = ModelRouter(
        keys={"anthropic": "sk-ant-copilot-test"},
        key_sources={"anthropic": "project_key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    adapter = _ScriptedAdapter(replies)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return ProjectCopilotService(_WorkspaceStub(project, router)), adapter  # type: ignore[arg-type]


def _chat(service: ProjectCopilotService) -> dict[str, Any]:
    return asyncio.run(
        service.chat(
            "project",
            {"messages": [{"role": "user", "content": "help me plan this"}]},
        )
    )


def _model_calls(project: Project) -> list[Any]:
    return list(project.db.execute("SELECT * FROM model_calls ORDER BY created_at, id"))


def test_copilot_refuses_an_exhausted_project_key_before_provider_egress(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, cap_micro=0)
    try:
        service, adapter = _service(project, [(VALID_REPLY, 0.004)])

        with pytest.raises(ProviderSpendCapExceeded):
            _chat(service)

        assert adapter.requests == []
        assert _model_calls(project) == []
    finally:
        project.close()


def test_copilot_http_cap_refusal_is_typed_409_without_egress(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, cap_micro=0)
    try:
        service, adapter = _service(project, [(VALID_REPLY, 0.004)])
        app = FastAPI()
        register_project_copilot_routes(app, service=service)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/projects/project/copilot",
            json={"messages": [{"role": "user", "content": "help"}]},
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "provider_spend_cap_exceeded"
        assert detail["details"]["setting"] == "spend_cap_usd"
        assert "spend cap" in detail["message"]
        assert adapter.requests == []
        assert _model_calls(project) == []
    finally:
        project.close()


def test_copilot_clean_call_persists_neutral_fact_and_advances_project_cap(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, cap_micro=1_000_000)
    try:
        service, adapter = _service(project, [(VALID_REPLY, 0.004)])

        response = _chat(service)

        assert len(adapter.requests) == 1
        calls = _model_calls(project)
        assert len(calls) == 1
        call = calls[0]
        assert call["run_id"] is None
        assert call["attempt_id"] is None
        assert call["capability"] == "llm.complete"
        assert call["engine"] == adapter.requests[0].model
        assert call["provider"] == "anthropic"
        assert call["credential_source"] == "project_key"
        assert call["provider_cost_usd"] == pytest.approx(0.004)
        assert json.loads(call["units"]) == {"tokens_in": 42, "tokens_out": 17}
        durable_fact = json.dumps(dict(call), sort_keys=True)
        assert "sk-ant-copilot-test" not in durable_fact
        assert "help me plan this" not in durable_fact
        assert response["cost_usd"] == pytest.approx(0.004)
        assert sum(float(row["provider_cost_usd"]) for row in calls) == pytest.approx(
            response["cost_usd"]
        )
        spend = project.provider_spend_state("anthropic")
        assert spend is not None
        assert spend.spent_micro == 4_000
        assert spend.unmetered_calls == 0
    finally:
        project.close()


def test_copilot_successful_repair_records_both_paid_transports(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, cap_micro=1_000_000)
    try:
        service, adapter = _service(
            project,
            [(INVALID_REPLY, 0.003), (VALID_REPLY, 0.004)],
        )

        response = _chat(service)

        assert len(adapter.requests) == 2
        calls = _model_calls(project)
        assert len(calls) == 2
        assert sum(float(row["provider_cost_usd"]) for row in calls) == pytest.approx(
            0.007
        )
        assert response["cost_usd"] == pytest.approx(0.007)
        spend = project.provider_spend_state("anthropic")
        assert spend is not None
        assert spend.spent_micro == 7_000
        assert spend.unmetered_calls == 0
    finally:
        project.close()


def test_copilot_unknown_cost_stays_unknown_and_makes_cap_unenforceable(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, cap_micro=1_000_000)
    try:
        service, adapter = _service(
            project,
            [(VALID_REPLY, None), (VALID_REPLY, 0.004)],
        )

        response = _chat(service)

        assert response["cost_usd"] is None
        calls = _model_calls(project)
        assert len(calls) == 1
        assert calls[0]["provider_cost_usd"] is None
        assert calls[0]["cost_source"] == "unknown"
        spend = project.provider_spend_state("anthropic")
        assert spend is not None
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 1

        with pytest.raises(ProviderSpendCapUnenforceable):
            _chat(service)
        assert len(adapter.requests) == 1
    finally:
        project.close()


def test_copilot_cache_only_response_records_history_but_costs_zero(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, cap_micro=1_000_000)
    try:
        service, adapter = _service(project, [(VALID_REPLY, 0.004, True)])

        response = _chat(service)

        assert len(adapter.requests) == 1
        assert response["cost_usd"] == 0.0
        calls = _model_calls(project)
        assert len(calls) == 1
        assert calls[0]["credential_source"] == "cache"
        assert calls[0]["provider_cost_usd"] == 0.0
        assert calls[0]["cost_source"] == "cache_hit"
        spend = project.provider_spend_state("anthropic")
        assert spend is not None
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 0
    finally:
        project.close()


def test_copilot_fact_write_failure_maps_to_safe_upstream_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, cap_micro=1_000_000)
    try:
        service, adapter = _service(project, [(VALID_REPLY, 0.004)])

        def _fail_write(*args: Any, **kwargs: Any) -> float:
            del args, kwargs
            raise RuntimeError("db failure mentioning sk-ant-copilot-test")

        monkeypatch.setattr(
            RunResultStore,
            "write_unscoped_model_calls",
            _fail_write,
        )

        with pytest.raises(ProjectCopilotUpstreamError) as caught:
            _chat(service)

        assert len(adapter.requests) == 1
        assert "usage facts could not be recorded" in caught.value.detail
        assert "retrying may make another provider call" in caught.value.detail
        assert "sk-ant-copilot-test" not in caught.value.detail
        assert _model_calls(project) == []
        spend = project.provider_spend_state("anthropic")
        assert spend is not None
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 0
    finally:
        project.close()


def test_copilot_exhausted_repair_still_records_billed_transports(
    tmp_path: Path,
) -> None:
    """A model can bill twice and still return no usable copilot reply.

    Repeating that 502 must not leave the cap frozen at zero, or an invalid
    provider response becomes an unlimited-spend bypass.
    """

    project = _project(tmp_path, cap_micro=1_000_000)
    try:
        service, adapter = _service(
            project,
            [(INVALID_REPLY, 0.003), (INVALID_REPLY, 0.004)],
        )

        with pytest.raises(ProjectCopilotUpstreamError):
            _chat(service)

        assert len(adapter.requests) == 2
        calls = _model_calls(project)
        assert len(calls) == 2
        assert sum(float(row["provider_cost_usd"]) for row in calls) == pytest.approx(
            0.007
        )
        spend = project.provider_spend_state("anthropic")
        assert spend is not None
        assert spend.spent_micro == 7_000
        assert spend.unmetered_calls == 0
    finally:
        project.close()
