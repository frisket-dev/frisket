from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import frisket.server.services.action_runs as action_runs_module
import frisket.server.services.project_copilot as project_copilot_module
from frisket.ai.llm import ChaosConfig, ModelRouter, ResponseCache
from frisket.engine.store import Project
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.server.app import create_app
from frisket.server.mcp.backends import LocalBackend
from frisket.server.services.project_copilot import ProjectCopilotService


@dataclass(frozen=True)
class _BoundPolicy:
    admission: str | None = None

    def bound(self, admission: str) -> _BoundPolicy:
        return _BoundPolicy(admission)

    def prepare(self, request: Any, credential_source: str) -> Any:
        del credential_source
        return request

    def before_live(self, request: Any, credential_source: str) -> None:
        del request, credential_source

    def after_live(self, request: Any, response: Any, credential_source: str) -> None:
        del request, response, credential_source


class _RefuseUnboundPolicy(_BoundPolicy):
    def prepare(self, request: Any, credential_source: str) -> Any:
        del request, credential_source
        raise AssertionError("an unbound execution policy became executable")

    def before_live(self, request: Any, credential_source: str) -> None:
        del request, credential_source
        raise AssertionError("an unbound execution policy became executable")

    def after_live(self, request: Any, response: Any, credential_source: str) -> None:
        del request, response, credential_source
        raise AssertionError("an unbound execution policy became executable")


def _classify_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify local-government news.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "infrastructure"],
                    "description": "Primary reporting beat.",
                }
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": "non-action-router-sentinel@sha256:stable",
    }


def test_non_action_consumers_never_resolve_the_execution_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_router = ModelRouter(
        keys={"anthropic": "ordinary-key"},
        key_sources={"anthropic": "local"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    execution_router = ModelRouter(
        keys={"anthropic": "platform-key"},
        key_sources={"anthropic": "platform_key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
        model_call_policy=_RefuseUnboundPolicy(),
    )
    factory_calls = 0

    def fresh_execution_router() -> ModelRouter:
        nonlocal factory_calls
        factory_calls += 1
        return execution_router

    app = create_app(
        tmp_path / "workspace",
        router=ordinary_router,
        execution_router_factory=fresh_execution_router,
        serve_spa=False,
        enable_provider_config=False,
    )
    workspace = app.state.workspace

    async def fake_copilot_chat(project, router, messages, *, model):
        del project, messages, model
        assert router is ordinary_router
        return {"reply": "ok", "needs_import": False, "proposals": []}

    monkeypatch.setattr(project_copilot_module, "copilot_chat", fake_copilot_chat)

    class CompletedBackfill:
        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {
                "status": "completed",
                "run_id": 1,
                "receipt_id": "receipt-1",
                "outputs": [],
                "errors": [],
            }

    def fake_run_action_spec(*args, router, **kwargs):
        del args, kwargs
        assert router is ordinary_router
        return CompletedBackfill()

    import frisket.engine.executor as executor_module

    monkeypatch.setattr(executor_module, "run_action_spec", fake_run_action_spec)

    try:
        with TestClient(app) as client:
            project_id = client.post(
                "/api/projects", json={"name": "ordinary router consumers"}
            ).json()["id"]
            imported = client.post(
                f"/api/projects/{project_id}/import/csv",
                files={
                    "file": (
                        "stories.csv",
                        b"story\nBridge repairs delayed\n",
                        "text/csv",
                    )
                },
            )
            assert imported.status_code == 200, imported.text
            sheet_id = imported.json()["sheet_id"]

            catalog = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
            assert catalog.status_code == 200, catalog.text
            estimate = client.post(
                f"/api/projects/{project_id}/actions/v1/estimate",
                json={"action": _classify_action(sheet_id)},
            )
            assert estimate.status_code == 200, estimate.text

            result = asyncio.run(
                ProjectCopilotService(workspace).chat(
                    project_id,
                    {"messages": [{"role": "user", "content": "plan"}]},
                )
            )
            assert result["reply"] == "ok"

            backend = object.__new__(LocalBackend)
            backend.ws = workspace
            backend._tasks = set()
            backfill = asyncio.run(backend.backfill_run(project_id, sheet_id, "beat"))
            assert backfill["status"] == "completed"

        project = workspace.get(project_id)
        assert workspace.router_for(project) is ordinary_router
        assert factory_calls == 0
    finally:
        workspace.queue.close()


def test_cached_app_builds_one_fresh_effective_router_per_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "platform-env-key")
    cache = ResponseCache(tmp_path / "shared.cache.db")
    chaos = ChaosConfig(seed=398, enabled=True)
    template = ModelRouter(
        keys={"anthropic": "platform-explicit-key"},
        key_sources={"anthropic": "platform_key"},
        env_key_source="platform_key",
        cache=cache,
        cache_mode="fresh",
        chaos=chaos,
        max_retries=9,
        model_call_policy=_BoundPolicy(),
    )
    bases: list[ModelRouter] = []
    composed: list[ModelRouter] = []

    def fresh_router() -> ModelRouter:
        router = ModelRouter(
            keys={"anthropic": "platform-explicit-key"},
            key_sources={"anthropic": "platform_key"},
            env_key_source="platform_key",
            cache=cache,
            cache_mode="fresh",
            chaos=chaos,
            max_retries=9,
            model_call_policy=_BoundPolicy(),
        )
        bases.append(router)
        return router

    def compose(project, router, context):
        policy = router.model_call_policy
        assert isinstance(policy, _BoundPolicy)
        admission = str(context.edition_snapshot["admission"])
        router.model_call_policy = policy.bound(admission)
        composed.append(router)
        return open_execution_composition(project, router, context)

    app = create_app(
        tmp_path / "workspace",
        router=template,
        execution_router_factory=fresh_router,
        execution_composition_factory=compose,
        serve_spa=False,
        enable_provider_config=False,
    )
    workspace = app.state.workspace
    project = Project.create(tmp_path / "project.frisket", name="fresh routers")
    monkeypatch.setattr(
        project,
        "provider_model_keys",
        lambda: {"gemini": "project-key"},
    )
    direct = ExecutionCompositionContext.direct()

    try:
        first = workspace.action_execution_router_for(project)
        workspace.execution_composition_for(
            project,
            first,
            replace(direct, edition_snapshot={"admission": "first"}),
        )
        second = workspace.action_execution_router_for(project)
        workspace.execution_composition_for(
            project,
            second,
            replace(direct, edition_snapshot={"admission": "second"}),
        )

        assert len(bases) == 2
        assert bases[0] is not bases[1]
        assert first is composed[0] and second is composed[1]
        assert first is not second
        assert first.cache is second.cache is cache
        assert first.cache_mode == second.cache_mode == "fresh"
        assert first.max_retries == second.max_retries == 9
        assert first.chaos is not None and first.chaos.config == chaos
        assert second.chaos is not None and second.chaos.config == chaos
        assert first.chaos is not bases[0].chaos
        assert second.chaos is not bases[1].chaos
        expected_sources = {
            "anthropic": "platform_key",
            "openai": "platform_key",
            "gemini": "project_key",
        }
        assert first.configured_key_sources() == expected_sources
        assert second.configured_key_sources() == expected_sources
        assert first.model_call_policy == _BoundPolicy("first")
        assert second.model_call_policy == _BoundPolicy("second")
        assert bases[0].model_call_policy == _BoundPolicy()
        assert bases[1].model_call_policy == _BoundPolicy()
        assert template.model_call_policy == _BoundPolicy()
    finally:
        project.close()
        workspace.queue.close()
        cache.close()


def test_direct_action_launch_resolves_one_router_for_gate_composition_and_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ResponseCache(tmp_path / "direct.cache.db")
    bases: list[ModelRouter] = []
    composed: list[ModelRouter] = []
    dispatched: list[ModelRouter] = []

    def fresh_router() -> ModelRouter:
        router = ModelRouter(cache=cache, cache_mode="fresh", use_env_keys=False)
        bases.append(router)
        return router

    def compose(project, router, context):
        composed.append(router)
        return open_execution_composition(project, router, context)

    real_run_action_spec = action_runs_module.run_action_spec

    def capture_dispatch(*args, router, **kwargs):
        dispatched.append(router)
        return real_run_action_spec(*args, router=router, **kwargs)

    monkeypatch.setattr(action_runs_module, "run_action_spec", capture_dispatch)
    app = create_app(
        tmp_path / "workspace",
        execution_router_factory=fresh_router,
        execution_composition_factory=compose,
        serve_spa=False,
        enable_provider_config=False,
    )

    try:
        with TestClient(app) as client:
            project_id = client.post(
                "/api/projects", json={"name": "one request router"}
            ).json()["id"]
            response = client.post(
                f"/api/projects/{project_id}/actions/v1/run",
                json={
                    "action_id": "import.rows",
                    "scope": {"kind": "project"},
                    "sheet_name": "rows",
                    "params": {
                        "columns": [{"name": "text", "type": "text"}],
                        "rows": [{"text": "same router"}],
                        "source": {
                            "kind": "inline",
                            "label": "seed",
                            "fingerprint": "sha256:seed",
                        },
                    },
                    "idempotency_key": "one-router@sha256:stable",
                },
            )
        assert response.status_code == 200, response.text
        assert len(bases) == 1
        assert composed == dispatched == bases
    finally:
        app.state.workspace.queue.close()
        cache.close()


def test_model_action_launch_reuses_one_router_across_preflight_and_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    bases: list[ModelRouter] = []
    composed: list[ModelRouter] = []

    def fresh_router() -> ModelRouter:
        router = ModelRouter(
            keys={"anthropic": "platform-key"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        bases.append(router)
        return router

    def compose(project, router, context):
        composed.append(router)
        return open_execution_composition(project, router, context)

    app = create_app(
        tmp_path / "workspace",
        execution_router_factory=fresh_router,
        execution_composition_factory=compose,
        require_explicit_provider_keys=True,
        serve_spa=False,
        enable_provider_config=False,
    )

    try:
        with TestClient(app) as client:
            project_id = client.post(
                "/api/projects", json={"name": "model request router"}
            ).json()["id"]
            imported = client.post(
                f"/api/projects/{project_id}/import/csv",
                files={"file": ("stories.csv", b"story\nBridge delayed\n", "text/csv")},
            )
            assert imported.status_code == 200, imported.text
            action = _classify_action(imported.json()["sheet_id"])
            path = f"/api/projects/{project_id}/actions/v1/run"

            challenge = client.post(path, json=action)
            assert challenge.status_code == 402, challenge.text
            assert len(bases) == 1
            assert composed == [bases[0], bases[0]]

            promise_set_hash = challenge.json()["errors"][0]["details"][
                "promise_set_hash"
            ]
            action["confirmation"] = promise_set_hash
            bases.clear()
            composed.clear()

            launched = client.post(path, json=action)
            assert launched.status_code == 200, launched.text
            assert launched.json()["status"] == "queued"
            assert len(bases) == 1
            assert composed == [bases[0], bases[0]]
    finally:
        app.state.workspace.queue.close()
