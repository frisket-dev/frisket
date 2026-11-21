"""Effective network-policy resolution:
``project mode -> organization default -> on``. The local edition never writes an
organization_default, so a bare project resolves from its own mode alone."""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from frisket.engine.store import Project


def test_unconfigured_default_is_on(project) -> None:
    assert project.network_policy() == {"mode": "inherit", "organization_default": None}
    assert project.effective_network_policy() == "on"


def test_project_mode_wins(project) -> None:
    project.set_network_policy(mode="off")
    assert project.effective_network_policy() == "off"
    project.set_network_policy(mode="on")
    assert project.effective_network_policy() == "on"


def test_inherit_defers_to_organization_default(project) -> None:
    project.set_network_policy(mode="inherit", organization_default="off")
    assert project.effective_network_policy() == "off"
    project.set_network_policy(organization_default="on")
    assert project.effective_network_policy() == "on"


def test_project_on_overrides_organization_off(project) -> None:
    project.set_network_policy(mode="on", organization_default="off")
    assert project.effective_network_policy() == "on"


def test_project_off_overrides_organization_on(project) -> None:
    project.set_network_policy(mode="off", organization_default="on")
    assert project.effective_network_policy() == "off"


def test_local_edition_resolves_from_project_alone(project) -> None:
    # No organization row anywhere: organization_default stays None and inherit means on.
    project.set_network_policy(mode="inherit")
    assert project.network_policy()["organization_default"] is None
    assert project.effective_network_policy() == "on"


def test_invalid_values_are_refused(project) -> None:
    with pytest.raises(ValueError):
        project.set_network_policy(mode="maybe")
    with pytest.raises(ValueError):
        project.set_network_policy(
            organization_default="inherit"
        )  # organization default is 2-state
    # Stored state unchanged after the refusals.
    assert project.effective_network_policy() == "on"


def test_policy_survives_reopen(tmp_path) -> None:
    p = Project.create(tmp_path / "p.frisket")
    p.set_network_policy(mode="off")
    p.close()
    with closing(Project(tmp_path / "p.frisket")) as reopened:
        assert reopened.effective_network_policy() == "off"


def test_corrupt_meta_self_heals_to_default(project) -> None:
    project.db.execute(
        "INSERT INTO meta (key, value) VALUES ('network_policy', 'not-json') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    project.db.commit()
    assert project.network_policy()["mode"] == "inherit"
    assert project.effective_network_policy() == "on"


def test_legacy_org_default_reads_and_rewrites_canonically(project) -> None:
    project.db.execute(
        "INSERT INTO meta (key, value) VALUES ('network_policy', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps({"mode": "inherit", "org_default": "off"}),),
    )
    project.db.commit()
    assert project.network_policy() == {
        "mode": "inherit",
        "organization_default": "off",
    }
    assert project.effective_network_policy() == "off"

    project.set_network_policy(mode="inherit")
    raw = json.loads(project.get_meta("network_policy"))
    assert raw == {"mode": "inherit", "organization_default": "off"}
    assert "org_default" not in raw
