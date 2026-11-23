"""A project's usage-data override must survive losing the manifest.

The manifest is an externalized summary; ``project.db`` is the durable record,
and the disaster-recovery path restores the db and rebuilds the manifest from
it. A project that shares no usage data therefore has to carry the override in
the db, or recovery silently resumes telemetry its owner switched off.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store import Project
from frisket.server.app import create_app
from frisket.server.workspace import Workspace
from frisket.upgrades.project import upgrade_project


def _db_meta(bundle: Path) -> dict[str, str]:
    with sqlite3.connect(bundle / "project.db") as db:
        return dict(db.execute("SELECT key, value FROM meta"))


def _restore_from_db_alone(bundle: Path) -> Project:
    """The DR path: project.db survives, the manifest is rebuilt from it."""
    (bundle / "manifest.json").unlink()
    return Project(bundle)


class _Destination:
    app_id = "test-app"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _emit(root: Path, project_id: str, destination: _Destination) -> int:
    client = TestClient(create_app(root, product_telemetry_destination=destination))
    return client.post(
        f"/api/projects/{project_id}/telemetry/events",
        json={"monthly_id": "a" * 64, "type": "Project.opened", "properties": {}},
    ).status_code


@pytest.mark.parametrize("value", ["inherit", "on", "off"])
def test_create_records_the_override_in_the_db_not_only_the_manifest(
    tmp_path, value
) -> None:
    bundle = tmp_path / "bundle"
    Project.create(bundle, "Notes", usage_data_sharing=value)

    assert _db_meta(bundle)["usage_data_sharing"] == value
    assert "sensitive" not in _db_meta(bundle)


def test_the_override_survives_a_restore_that_rebuilds_the_manifest(
    tmp_path,
) -> None:
    bundle = tmp_path / "bundle"
    Project.create(bundle, "Leak source notes", usage_data_sharing="off")

    restored = _restore_from_db_alone(bundle)

    assert restored.project_metadata()["usage_data_sharing"] == "off"
    assert restored.project_metadata()["sensitive"] is True
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["usage_data_sharing"] == "off"
    assert manifest["sensitive"] is True


def test_a_rebuilt_manifest_answers_the_reader_that_never_opens_sqlite(
    tmp_path,
) -> None:
    """``Workspace.list()`` builds the listing out of manifests and never
    opens a project db; hosted, its ``sensitive`` answer is written back into
    the control plane's ``projects.sensitive``."""
    root = tmp_path / "data"
    root.mkdir()
    bundle = root / "leak-notes.frisket"
    Project.create(bundle, "Leak source notes", usage_data_sharing="off").close()

    _restore_from_db_alone(bundle).close()

    assert [(row["id"], row["sensitive"]) for row in Workspace(root).list()] == [
        ("leak-notes", True)
    ]


def test_a_restored_project_that_shares_nothing_emits_no_telemetry(
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "bundle.frisket"
    Project.create(bundle, "Leak source notes", usage_data_sharing="off")
    destination = _Destination()

    _restore_from_db_alone(bundle).close()
    assert _emit(root, "bundle", destination) == 204
    assert destination.sent == []


@pytest.mark.parametrize("value", ["inherit", "on"])
def test_the_gate_is_the_override_and_not_something_else_refusing(
    tmp_path, value
) -> None:
    """Positive control: a project that inherits or shares emits."""
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "bundle.frisket"
    Project.create(bundle, "Ordinary notes", usage_data_sharing=value)
    destination = _Destination()

    _restore_from_db_alone(bundle).close()
    assert _emit(root, "bundle", destination) == 204
    assert len(destination.sent) == 1


def test_restricted_allowed_services_do_not_suppress_telemetry(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    Project.create(
        root / "bundle.frisket", "Restricted", allowed_services="this_machine_only"
    ).close()
    destination = _Destination()

    assert _emit(root, "bundle", destination) == 204
    assert len(destination.sent) == 1


def test_the_override_follows_the_workspace_only_when_inherited(tmp_path) -> None:
    project = Project.create(tmp_path / "bundle", "Notes")
    assert project.shares_usage_data(workspace_shares=True) is True
    assert project.shares_usage_data(workspace_shares=False) is False
    project.set_usage_data_sharing("on")
    assert project.shares_usage_data(workspace_shares=False) is True
    project.set_usage_data_sharing("off")
    assert project.shares_usage_data(workspace_shares=True) is False


@pytest.mark.parametrize(
    ("stored_sensitive", "expected"),
    [("true", "off"), ("false", "inherit"), (None, "inherit")],
)
def test_upgrade_turns_the_sensitive_flag_into_the_override(
    tmp_path, stored_sensitive, expected
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "old.frisket"
    Project.create(bundle, "Old project").close()
    with sqlite3.connect(bundle / "project.db") as db:
        db.execute(
            "DELETE FROM meta WHERE key IN ('usage_data_sharing', 'allowed_services')"
        )
        if stored_sensitive is not None:
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('sensitive', ?)",
                (stored_sensitive,),
            )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for key in ("usage_data_sharing", "allowed_services"):
        manifest.pop(key)
    manifest["sensitive"] = stored_sensitive == "true"
    manifest_path.write_text(json.dumps(manifest))

    project = Project(bundle)
    upgrade_project(project)

    assert _db_meta(bundle)["usage_data_sharing"] == expected
    assert "sensitive" not in _db_meta(bundle)
    assert project.allowed_services() == "all_services"
    assert project.project_metadata()["sensitive"] is (expected == "off")
    project.close()
    listed = Workspace(root).list()
    assert [(row["sensitive"], row["allowed_services"]) for row in listed] == [
        (expected == "off", "all_services")
    ]
    destination = _Destination()
    assert _emit(root, "old", destination) == 204
    assert len(destination.sent) == (0 if expected == "off" else 1)
