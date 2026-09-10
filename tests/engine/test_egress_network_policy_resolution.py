"""Effective network-policy resolution:
``project mode -> org default -> on``. The local edition never writes an
org_default, so a bare project resolves from its own mode alone."""

from __future__ import annotations

import pytest

from frisket.engine.store import Project


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def test_unconfigured_default_is_on(project) -> None:
    assert project.network_policy() == {"mode": "inherit", "org_default": None}
    assert project.effective_network_policy() == "on"


def test_project_mode_wins(project) -> None:
    project.set_network_policy(mode="off")
    assert project.effective_network_policy() == "off"
    project.set_network_policy(mode="on")
    assert project.effective_network_policy() == "on"


def test_inherit_defers_to_org_default(project) -> None:
    project.set_network_policy(mode="inherit", org_default="off")
    assert project.effective_network_policy() == "off"
    project.set_network_policy(org_default="on")
    assert project.effective_network_policy() == "on"


def test_project_on_overrides_org_off(project) -> None:
    project.set_network_policy(mode="on", org_default="off")
    assert project.effective_network_policy() == "on"


def test_project_off_overrides_org_on(project) -> None:
    project.set_network_policy(mode="off", org_default="on")
    assert project.effective_network_policy() == "off"


def test_local_edition_resolves_from_project_alone(project) -> None:
    # No org row anywhere: org_default stays None and inherit means on.
    project.set_network_policy(mode="inherit")
    assert project.network_policy()["org_default"] is None
    assert project.effective_network_policy() == "on"


def test_invalid_values_are_refused(project) -> None:
    with pytest.raises(ValueError):
        project.set_network_policy(mode="maybe")
    with pytest.raises(ValueError):
        project.set_network_policy(org_default="inherit")  # org default is 2-state
    # Stored state unchanged after the refusals.
    assert project.effective_network_policy() == "on"


def test_policy_survives_reopen(tmp_path) -> None:
    p = Project.create(tmp_path / "p.frisket")
    p.set_network_policy(mode="off")
    p.close()
    reopened = Project(tmp_path / "p.frisket")
    try:
        assert reopened.effective_network_policy() == "off"
    finally:
        reopened.close()


def test_corrupt_meta_self_heals_to_default(project) -> None:
    project.db.execute(
        "INSERT INTO meta (key, value) VALUES ('network_policy', 'not-json') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    project.db.commit()
    assert project.network_policy()["mode"] == "inherit"
    assert project.effective_network_policy() == "on"
