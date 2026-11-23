"""The stored network on/off setting folds into Allowed services."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.engine.store import Project
from frisket.server.app import create_app
from frisket.project_service_access import (
    SelectedRoute,
    check_route,
    project_service_access,
)
from frisket.server.provider_config import create_local_endpoint
from frisket.service_access import (
    AllowedServices,
    ServiceNotAllowed,
    register_workspace_edition,
)
from frisket.team.bootstrap import init_identity_schema
from frisket.upgrades.project import upgrade_project
from frisket.engine.store.project_meta import AllowedServicesConfirmationRequired

ALLOWED: tuple[AllowedServices | None, ...] = (
    None,
    "this_machine_only",
    "approved_services",
    "all_services",
)


def _meta(bundle: Path) -> dict[str, str]:
    with sqlite3.connect(bundle / "project.db") as db:
        return dict(db.execute("SELECT key, value FROM meta"))


def _legacy_bundle(
    root: Path,
    *,
    network: dict[str, str | None],
    allowed: AllowedServices | None,
    manifest_state: str,
) -> Path:
    """A project as the previous release stored it: a network_policy meta key
    beside (or without) an Allowed services value, and a manifest that is
    current, stale, or lost."""
    bundle = root / "legacy.frisket"
    Project.create(bundle, "Legacy").close()
    with sqlite3.connect(bundle / "project.db") as db:
        db.execute("DELETE FROM meta WHERE key='allowed_services'")
        if allowed is not None:
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('allowed_services', ?)",
                (allowed,),
            )
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('network_policy', ?)",
            (json.dumps(network),),
        )
    manifest_path = bundle / "manifest.json"
    if manifest_state == "missing":
        manifest_path.unlink()
    else:
        manifest = json.loads(manifest_path.read_text())
        manifest["network"] = network
        manifest["allowed_services"] = "all_services"
        manifest_path.write_text(json.dumps(manifest))
    return bundle


def _expected(network_off: bool, allowed: AllowedServices | None) -> AllowedServices:
    if network_off and allowed in (None, "all_services"):
        return "approved_services"
    return allowed or "all_services"


NETWORK_STATES = {
    "off": ({"mode": "off", "organization_default": None}, True),
    "on": ({"mode": "on", "organization_default": None}, False),
    "inherit_off": ({"mode": "inherit", "organization_default": "off"}, True),
    "inherit_on": ({"mode": "inherit", "organization_default": None}, False),
    "on_over_org_off": ({"mode": "on", "organization_default": "off"}, False),
}


@pytest.mark.parametrize("manifest_state", ["stale", "missing"])
@pytest.mark.parametrize("allowed", ALLOWED)
@pytest.mark.parametrize("network_state", sorted(NETWORK_STATES))
def test_upgrade_folds_the_network_setting(
    tmp_path: Path,
    network_state: str,
    allowed: AllowedServices | None,
    manifest_state: str,
) -> None:
    network, network_off = NETWORK_STATES[network_state]
    bundle = _legacy_bundle(
        tmp_path, network=network, allowed=allowed, manifest_state=manifest_state
    )

    project = Project(bundle)
    upgrade_project(project)
    upgrade_project(project)

    expected = _expected(network_off, allowed)
    meta = _meta(bundle)
    assert "network_policy" not in meta
    assert meta.get("allowed_services", "all_services") == expected
    assert project.allowed_services() == expected
    project._write_manifest()
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert "network" not in manifest
    assert manifest["allowed_services"] == expected
    project.close()


def test_a_stricter_allowed_services_survives_network_off(tmp_path: Path) -> None:
    bundle = _legacy_bundle(
        tmp_path,
        network={"mode": "off", "organization_default": None},
        allowed="this_machine_only",
        manifest_state="stale",
    )
    project = Project(bundle)
    upgrade_project(project)
    assert project.allowed_services() == "this_machine_only"
    project.close()


@pytest.mark.parametrize(
    "network",
    [
        {"mode": "off", "organization_default": None},
        {"mode": "inherit", "organization_default": "off"},
    ],
)
def test_cloud_network_off_project_stores_this_machine_only(
    tmp_path: Path, network: dict[str, str | None]
) -> None:
    """On Cloud, Approved services would allow Frisket's own processing,
    which network off refused, so the project stores This machine only:
    nothing loosens, and the owner chooses an offered option to go further."""
    bundle = _legacy_bundle(
        tmp_path, network=network, allowed=None, manifest_state="stale"
    )
    register_workspace_edition(tmp_path, "cloud")
    try:
        project = Project(bundle)
        upgrade_project(project)
        upgrade_project(project)
        assert project.allowed_services() == "this_machine_only"
        assert "network_policy" not in _meta(bundle)
        access = project_service_access(project)
        with pytest.raises(ServiceNotAllowed):
            check_route(
                project, access, SelectedRoute("frisket-cloud", "frisket_shared")
            )
        with pytest.raises(AllowedServicesConfirmationRequired):
            project.set_allowed_services(
                "approved_services", confirm_allowed_services=None
            )
        project.set_allowed_services(
            "approved_services", confirm_allowed_services="approved_services"
        )
        check_route(
            project,
            project_service_access(project),
            SelectedRoute("frisket-cloud", "frisket_shared"),
        )
        assert project.history()[-1]["kind"] == "allowed_services"
        project.close()
    finally:
        register_workspace_edition(tmp_path, "solo")


def test_cloud_still_refuses_choosing_this_machine_only(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "cloud", edition="cloud"))
    pid = client.post("/api/projects", json={"name": "Cloud"}).json()["id"]
    refused = client.patch(
        f"/api/projects/{pid}/allowed-services",
        json={"allowed_services": "this_machine_only"},
    )
    assert refused.status_code == 400, refused.text
    register_workspace_edition(tmp_path / "cloud", "solo")


# Each destination class with what network off allowed in the previous
# release: in-process work and the operator's own network (loopback or any
# configured model server) were allowed; provider APIs, web services and
# Frisket Cloud routes were refused.
LEGACY_OFF_ALLOWED = {
    "in_process": True,
    "loopback_model_server": True,
    "configured_remote_model_server": True,
    "provider_api": False,
    "web_service": False,
    "frisket_cloud_route": False,
}


def _new_decision(project: Project, destination: str) -> bool:
    access = project_service_access(project)
    try:
        if destination == "in_process":
            check_route(project, access, SelectedRoute("local", "none"))
        elif destination == "loopback_model_server":
            access.check_url("http://127.0.0.1:11434")
        elif destination == "configured_remote_model_server":
            access.check_url("http://ollama.lan.example.test:11434")
        elif destination == "provider_api":
            access.check_url("https://api.openai.com/v1")
        elif destination == "web_service":
            access.check_remote_service("web search")
        else:
            check_route(
                project, access, SelectedRoute("frisket-cloud", "frisket_shared")
            )
    except ServiceNotAllowed:
        return False
    return True


@pytest.mark.parametrize("allowed", ALLOWED)
def test_the_fold_never_allows_more_than_network_off_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: AllowedServices | None
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    create_local_endpoint(
        tmp_path, name="Ollama", url="http://ollama.lan.example.test:11434"
    )
    bundle = _legacy_bundle(
        tmp_path,
        network={"mode": "off", "organization_default": None},
        allowed=allowed,
        manifest_state="stale",
    )
    project = Project(bundle)
    upgrade_project(project)
    for destination, legacy in LEGACY_OFF_ALLOWED.items():
        assert not (_new_decision(project, destination) and not legacy), destination
    project.close()


@pytest.mark.parametrize(
    ("network_default", "expected"),
    [("off", "approved_services"), ("on", "all_services"), (None, None)],
)
def test_control_plane_carries_the_organization_default_over(
    tmp_path: Path, network_default: str | None, expected: str | None
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'v10.db'}", future=True)
    init_identity_schema(engine)
    with engine.begin() as cx:
        cx.exec_driver_sql(
            "ALTER TABLE organizations DROP COLUMN allowed_services_default"
        )
        cx.exec_driver_sql(
            "ALTER TABLE organizations ADD COLUMN network_default VARCHAR"
        )
        cx.exec_driver_sql(
            "ALTER TABLE projects ADD COLUMN network VARCHAR NOT NULL DEFAULT 'inherit'"
        )
        cx.exec_driver_sql("UPDATE schema_marker SET schema_version = 10 WHERE id = 1")
        cx.exec_driver_sql(
            "INSERT INTO organizations (id, name, network_default) VALUES (1, 'Desk', ?)",
            (network_default,),
        )

    init_identity_schema(engine)

    with engine.connect() as cx:
        assert (
            cx.exec_driver_sql(
                "SELECT schema_version FROM schema_marker WHERE id = 1"
            ).scalar_one()
            == 11
        )
        assert (
            cx.exec_driver_sql(
                "SELECT allowed_services_default FROM organizations WHERE id = 1"
            ).scalar_one()
            == expected
        )
    columns = {c["name"] for c in sa.inspect(engine).get_columns("organizations")}
    assert "network_default" not in columns
    project_columns = {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    assert "network" not in project_columns
    engine.dispose()
