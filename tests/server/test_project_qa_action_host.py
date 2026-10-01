from __future__ import annotations

from typing import Any

import pytest

from frisket.server.services.project_qa_action_host import ProjectAskActionHost


class _Router:
    local_endpoints = {"local": object()}


class _Workspace:
    executor_deps_factory = object()

    def __init__(self) -> None:
        self.project = object()
        self.router = _Router()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def get(self, project_id: str) -> object:
        self.calls.append(("get", (project_id,)))
        return self.project

    def router_for(self, project: object) -> _Router:
        self.calls.append(("router_for", (project,)))
        return self.router

    def edition_execution_composition_context_for(self, context: object) -> str:
        self.calls.append(("edition_context", (context,)))
        return f"edition:{context}"

    def execution_composition_for(
        self, project: object, router: _Router, context: str
    ) -> object:
        self.calls.append(("composition", (project, router, context)))
        return {"context": context}

    def org_provider_keys(self) -> dict[str, str]:
        self.calls.append(("org_provider_keys", ()))
        return {"openai": "configured"}


def test_catalog_uses_the_live_project_composition_and_sidecar_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()
    observed: dict[str, Any] = {}

    def catalog(project: object, **kwargs: Any) -> dict[str, Any]:
        observed["project"] = project
        observed.update(kwargs)
        return {"actions": [{"kind": "plugin.example"}]}

    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host."
        "project_action_catalog_payload_with_launcher_hints",
        catalog,
    )
    host = ProjectAskActionHost(
        workspace,  # type: ignore[arg-type]
        "project-1",
        context_provider=lambda: "admission-1",
        sidecar_capabilities_provider=lambda: {"engines": [{"name": "gliner"}]},
    )

    assert host.catalog() == {"actions": [{"kind": "plugin.example"}]}
    assert observed == {
        "project": workspace.project,
        "sidecar_capabilities": {"engines": [{"name": "gliner"}]},
        "execution_composition": {"context": "edition:admission-1"},
        "org_provider_keys": {"openai": "configured"},
        "has_local_model_endpoint": True,
        "effective_router": workspace.router,
        "connected_account_resolver_configured": True,
    }


def test_quote_and_run_refresh_the_trusted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()
    contexts = iter(["quote-context", "run-context"])
    observed: list[tuple[str, str, dict[str, Any], object]] = []

    def estimate(
        _self: object,
        project_id: str,
        action: dict[str, Any],
        *,
        request_context: object,
    ) -> dict[str, Any]:
        observed.append(("quote", project_id, action, request_context))
        return {"estimate": {"billed_cost": 0}}

    def run_action(
        _self: object, project_id: str, body: dict[str, Any], *, request_context: object
    ) -> dict[str, Any]:
        observed.append(("run", project_id, body, request_context))
        return {"status": "queued"}

    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host.ActionPreviewService.estimate",
        estimate,
    )
    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host.ActionRunService.run_action",
        run_action,
    )
    host = ProjectAskActionHost(
        workspace,  # type: ignore[arg-type]
        "project-1",
        context_provider=lambda: next(contexts),
        sidecar_capabilities_provider=lambda: pytest.fail("catalog was not requested"),
    )

    assert host.quote({"action_id": "map.template"}) == {"estimate": {"billed_cost": 0}}
    assert host.run("project-1", {"action_id": "map.template"}) == {"status": "queued"}
    assert observed == [
        ("quote", "project-1", {"action_id": "map.template"}, "quote-context"),
        ("run", "project-1", {"action_id": "map.template"}, "run-context"),
    ]


def test_run_never_redirects_a_bound_host_to_another_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()
    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host.ActionRunService.run_action",
        lambda *_args, **_kwargs: pytest.fail("must not run another project"),
    )
    host = ProjectAskActionHost(
        workspace,  # type: ignore[arg-type]
        "project-1",
        context_provider=lambda: pytest.fail("must not read context"),
        sidecar_capabilities_provider=lambda: pytest.fail("catalog was not requested"),
    )

    with pytest.raises(ValueError, match="different project"):
        host.run("project-2", {"action_id": "map.template"})
