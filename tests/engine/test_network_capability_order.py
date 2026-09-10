"""Consent facts must not depend on catalog set iteration order."""

import pytest

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
