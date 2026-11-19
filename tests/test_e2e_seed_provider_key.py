"""The browser suite's placeholder provider key reaches only its own workspaces."""

from __future__ import annotations

import pytest

from scripts.e2e.seed_provider_key import (
    E2E_ACTIVE_MARKER,
    PLACEHOLDER_KEY,
    NotAnE2EWorkspace,
    seed_placeholder_provider_key,
)


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("FRISKET_SECRETS_MASTER_KEY", "e2e-seed-test-master-key")


def test_a_workspace_without_the_harness_marker_is_refused(tmp_path, make_project):
    project = make_project("costgate")
    with pytest.raises(NotAnE2EWorkspace, match=E2E_ACTIVE_MARKER):
        seed_placeholder_provider_key(tmp_path, "costgate", "anthropic")
    assert project.provider_model_keys() == {}


def test_a_harness_workspace_gets_the_placeholder_project_key(tmp_path, make_project):
    project = make_project("costgate")
    (tmp_path / E2E_ACTIVE_MARKER).write_text("{}\n", encoding="utf-8")
    seed_placeholder_provider_key(tmp_path, "costgate", "anthropic")
    assert project.provider_model_keys() == {"anthropic": PLACEHOLDER_KEY}
