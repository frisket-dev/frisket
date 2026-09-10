from __future__ import annotations

import pytest

from frisket.engine.jobs.runs import _router_for
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimless_test_model_calls


def test_credential_sources_reject_unknown_or_secret_like_values() -> None:
    from frisket.ai.models.metadata import ModelCallMeta

    with pytest.raises(ValueError, match="unsupported credential source"):
        ModelRouter(
            keys={"openai": "value"},
            key_sources={"openai": "real-secret"},
        )
    with pytest.raises(ValueError, match="unsupported credential source"):
        ModelCallMeta.provider_call(
            capability="llm.complete",
            engine="openai/test",
            provider="openai",
            provider_kind="chat_api",
            credential_source="real-secret",
            provider_reported_cost_usd=0.0,
            provider_cost_usd=0.0,
            cost_source="pricing_data",
            duration_ms=None,
        )


def test_durable_fact_store_rejects_unknown_credential_source(tmp_path) -> None:
    project = Project.create(tmp_path / "invalid-fact.frisket", name="invalid-fact")
    try:
        sheet_id = project.add_sheet("Facts")
        column_id = project.add_column(sheet_id, "value")
        row_id = project.add_rows(
            sheet_id,
            [{"value": "input"}],
            {"value": column_id},
        )[0]
        op_id = project.append_op("map", {"recipe": "invalid-fact"})
        run_store = RunResultStore(project)
        run_id = run_store.start_run(op_id, sheet_id, "test.invalid_fact")
        with pytest.raises(ValueError, match="unsupported credential source"):
            write_claimless_test_model_calls(
                project,
                run_id,
                [
                    {
                        "row_id": row_id,
                        "column_id": column_id,
                        "model_calls": [
                            {
                                "fact_version": "frisket.model-call-fact.v1",
                                "capability": "llm.complete",
                                "engine": "openai/test",
                                "credential_source": "real-secret",
                            }
                        ],
                    }
                ],
            )
    finally:
        project.close()


def test_workspace_preserves_mixed_org_and_platform_sources(tmp_path) -> None:
    project = Project.create(tmp_path / "project.frisket", name="project")
    base = ModelRouter(
        keys={"openai": "org-value", "anthropic": "platform-value"},
        key_sources={"openai": "org_byok", "anthropic": "platform_key"},
    )
    try:
        composed = Workspace(tmp_path / "hosted", router=base).router_for(project)
        assert composed.configured_key_sources() == {
            "openai": "org_byok",
            "anthropic": "platform_key",
        }
    finally:
        project.close()


def test_injected_router_without_a_declared_source_refuses(tmp_path) -> None:
    """The worker cannot know whose key an injected router carries.

    It used to guess ``platform_key``, which is a FALSE label in a
    self-hosted deployment: there the injected router holds the operator's
    own env material, and that fabricated token was persisted onto every
    provider fact the run wrote. Correct only because the hosted composition
    happened to be the only injector. The provenance is now required at the
    effect site, so an injector that omits it fails loudly and by name.
    """
    project = Project.create(tmp_path / "undeclared.frisket", name="undeclared")
    try:
        undeclared = ModelRouter(keys={"openai": "operator-env-value"})
        assert not undeclared.has_explicit_key_source("openai")
        with pytest.raises(ValueError, match="must declare the provenance"):
            # A project-key layer forces the composition branch; before the
            # fix this returned a router stamping openai as "platform_key".
            _router_for(project, undeclared, keys={"anthropic": "org-value"})
    finally:
        project.close()


def test_injected_router_with_a_declared_source_keeps_it(tmp_path) -> None:
    """The declared token survives composition unchanged — the fence above
    refuses the undeclared case without rewriting the declared one."""
    project = Project.create(tmp_path / "declared.frisket", name="declared")
    try:
        declared = ModelRouter(
            keys={"openai": "operator-env-value"},
            key_sources={"openai": "local"},
        )
        composed = _router_for(project, declared, keys={"anthropic": "org-value"})
        assert composed.configured_key_sources()["openai"] == "local"
        assert composed.configured_key_sources()["anthropic"] == "org_byok"
    finally:
        project.close()


def test_local_queued_env_key_is_operator_owned(tmp_path, monkeypatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "local-operator-value")
    project = Project.create(tmp_path / "project.frisket", name="project")
    try:
        composed = _router_for(project, None)
        assert composed.configured_key_sources()["openai"] == "local"
    finally:
        project.close()


def test_router_for_composed_branch_carries_local_endpoints(tmp_path) -> None:
    """The org-resolver/hosted reconstruction branch of ``router_for``
    (workspace.py) used to build a
    fresh ``ModelRouter`` from scratch without threading the injected base
    router's ``local_endpoints`` forward -- silently losing any configured
    endpoint identity/origin/token/edge_auth the moment a project- or org-key layer
    forced a reconstruction."""
    project = Project.create(tmp_path / "project.frisket", name="project")
    endpoint = LocalModelEndpointConfig(
        endpoint_id="heavy",
        display_name="Heavy internal",
        origin="https://llm.heavy.internal",
        inference_token="secret-bearer",
        edge_auth=True,
        source="env",
    )
    try:
        base = ModelRouter(local_endpoints=(endpoint,), use_env_keys=False)
        # provider_keys_resolver forces the composed-router reconstruction
        # branch (resolved_org_keys is not None) rather than the fast path
        # that just returns self._router unchanged.
        ws = Workspace(
            tmp_path / "org",
            router=base,
            provider_keys_resolver=lambda: {"openai": "org-value"},
        )
        composed = ws.router_for(project)
        assert composed is not base
        assert composed.local_endpoints == (endpoint,)
        assert composed.local_endpoints[0].endpoint_id == "heavy"
        assert composed.local_endpoints[0].origin == "https://llm.heavy.internal"
        assert composed.local_endpoints[0].inference_token == "secret-bearer"
        assert composed.local_endpoints[0].edge_auth is True
    finally:
        project.close()


def test_diagnostic_router_org_resolver_branch_carries_local_endpoints(
    tmp_path,
) -> None:
    """Same gap, ``Workspace.diagnostic_router``'s org-resolver branch: it
    reconstructs a fresh router from the org keys resolver without checking
    (let alone threading) the injected base router's ``local_endpoints`` at
    all."""
    endpoint = LocalModelEndpointConfig(
        endpoint_id="heavy",
        display_name="Heavy internal",
        origin="https://llm.heavy.internal",
        inference_token="secret-bearer",
        edge_auth=True,
        source="env",
    )
    base = ModelRouter(local_endpoints=(endpoint,), use_env_keys=False)
    ws = Workspace(
        tmp_path / "org-diag",
        router=base,
        provider_keys_resolver=lambda: {"openai": "org-value"},
    )
    diag = ws.diagnostic_router()
    assert diag.local_endpoints == (endpoint,)
    assert diag.local_endpoints[0].endpoint_id == "heavy"
    assert diag.local_endpoints[0].origin == "https://llm.heavy.internal"
    assert diag.local_endpoints[0].inference_token == "secret-bearer"
    assert diag.local_endpoints[0].edge_auth is True
