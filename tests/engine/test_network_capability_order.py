"""Consent facts must not depend on catalog set iteration order."""

import pytest
from types import SimpleNamespace

from frisket.authoring import action_metadata
from frisket.engine.runner.network_policy import _catalog_external_capability


@pytest.mark.parametrize(
    "capabilities",
    [
        ["external:web_search", "model:complete", "external:http_fetch"],
        ["external:http_fetch", "external:web_search", "model:complete"],
    ],
)
def test_remote_consent_tag_is_stable(monkeypatch, capabilities):
    monkeypatch.setattr(
        action_metadata, "declared_action_capabilities", lambda _: capabilities
    )
    assert _catalog_external_capability("research.answer") == "external:http_fetch"


def test_capability_checks_do_not_build_catalog_schemas(monkeypatch):
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.runner.network_policy import remote_capability_for_spec

    # Registry declarations remain live; generating UI/JSON schemas must not
    # become part of a per-row network/paid-effect check.
    assert ACTION_REGISTRY.get("map.ask") is not None

    def forbidden_catalog(_self):
        pytest.fail("execution capability lookup built presentation schemas")

    monkeypatch.setattr(RegisteredAction, "catalog_entry", forbidden_catalog)
    recipe = SimpleNamespace(is_llm=lambda _spec: True)
    assert remote_capability_for_spec(recipe, {"action_kind": "map.api_call"}) == (
        "external:api_call"
    )
    spec = {"action_kind": "map.ask", "model": "openai/gpt-5"}
    assert remote_capability_for_spec(recipe, spec) == "provider:openai"
    spec["model"] = "ollama/local-model"
    assert remote_capability_for_spec(recipe, spec) is None
    assert "unsafe:local_code" in action_metadata.declared_action_capabilities(
        "map.python"
    )
    assert action_metadata.declared_action_capabilities("unknown.action") == frozenset()
