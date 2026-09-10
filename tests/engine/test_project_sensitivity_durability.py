"""``sensitive`` must survive losing the manifest.

The manifest is an externalized summary; ``project.db`` is the durable record,
and the disaster-recovery path restores the db and rebuilds the manifest from
it. A project created sensitive therefore has to carry the flag in the db, or
recovery silently downgrades it -- and ``sensitive`` is what switches telemetry
off entirely, so the downgrade is a fail-open on the one setting a journalist
sets because of who their sources are.

``set_project_sensitivity`` writes both stores. These tests hold ``create`` to
the same contract.
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


@pytest.mark.parametrize("sensitive", [True, False])
def test_create_records_sensitivity_in_the_db_not_only_the_manifest(
    tmp_path, sensitive: bool
) -> None:
    bundle = tmp_path / "bundle"
    Project.create(bundle, "Notes", sensitive=sensitive)

    # Recorded either way: an absent key would let ``False`` pass by accident
    # while ``True`` is being lost.
    assert _db_meta(bundle)["sensitive"] == ("true" if sensitive else "false")


def test_sensitivity_survives_a_restore_that_rebuilds_the_manifest(
    tmp_path,
) -> None:
    bundle = tmp_path / "bundle"
    Project.create(bundle, "Leak source notes", sensitive=True)

    restored = _restore_from_db_alone(bundle)

    # ``project_metadata`` falls back to meta, so it is the one reader that
    # cannot fail here. The rebuilt MANIFEST is what the readers that never
    # open sqlite get, so assert that too or this proves nothing about them.
    assert restored.project_metadata()["sensitive"] is True
    assert json.loads((bundle / "manifest.json").read_text())["sensitive"] is True


def test_a_rebuilt_manifest_answers_the_reader_that_never_opens_sqlite(
    tmp_path,
) -> None:
    """``Workspace.list()`` builds the whole project listing out of manifests
    and deliberately never opens a project db, so it has no meta fallback to
    save it. In a hosted deployment, its answer is written straight back into
    the control plane's ``projects.sensitive`` (a downstream composition's
    project reconciliation), so a rebuilt manifest missing the key turns a
    lost manifest into a durably wrong record of which projects are
    protected."""
    root = tmp_path / "data"
    root.mkdir()
    bundle = root / "leak-notes.frisket"
    Project.create(bundle, "Leak source notes", sensitive=True).close()

    _restore_from_db_alone(bundle).close()

    assert [(row["id"], row["sensitive"]) for row in Workspace(root).list()] == [
        ("leak-notes", True)
    ]


def test_a_restored_sensitive_project_still_emits_no_telemetry(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "bundle.frisket"
    Project.create(bundle, "Leak source notes", sensitive=True)
    destination = _Destination()

    _restore_from_db_alone(bundle).close()
    assert _emit(root, "bundle", destination) == 204
    assert destination.sent == []


def test_the_gate_is_sensitivity_and_not_something_else_refusing(tmp_path) -> None:
    """Positive control: with these settings a non-sensitive project emits.

    Without it the suppression assertions above would pass against an emitter
    that refuses everything for an unrelated reason.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "bundle.frisket"
    Project.create(bundle, "Ordinary notes", sensitive=False)
    destination = _Destination()

    _restore_from_db_alone(bundle).close()
    assert _emit(root, "bundle", destination) == 204
    assert len(destination.sent) == 1


def test_the_setter_and_create_agree_on_the_stored_spelling(tmp_path) -> None:
    """Two write sites, one spelling -- the reader compares against "true"."""
    created = tmp_path / "created"
    Project.create(created, "Created sensitive", sensitive=True)

    toggled = tmp_path / "toggled"
    Project.create(toggled, "Toggled sensitive").set_project_sensitivity(True)

    assert _db_meta(created)["sensitive"] == _db_meta(toggled)["sensitive"]
