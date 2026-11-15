"""Maintenance discovers published projects before assigning hosted identities."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from frisket.engine.jobs.queue import SqliteJobQueue
from frisket.engine.store import Project
from frisket.project_identity import ProjectStorageKey
from frisket.project_opener import project_bundle_paths


SCANNERS = [
    ("sources", "enqueue_due_source_polls"),
    ("embeddings", "enqueue_due_scheduled_refreshes"),
    ("watches", "enqueue_due_watch_evaluations"),
    ("notifications_digest", "enqueue_due_notification_digests"),
    ("notifications_delivery", "reconcile_notification_delivery_requests"),
]


def _bundles(root: Path) -> tuple[Path, Path]:
    for name in ("valid", "My report", ".upload-staging"):
        project = Project.create(root / f"{name}.frisket", name=name)
        project.close()
    for name, marker in (("no-db", "manifest.json"), ("no-manifest", "project.db")):
        path = root / f"{name}.frisket"
        path.mkdir()
        (path / marker).touch()
    (root / "not-a-directory.frisket").touch()
    return root / "valid.frisket", root / "My report.frisket"


@pytest.mark.parametrize(("module_name", "function_name"), SCANNERS)
@pytest.mark.parametrize("scoped", [False, True])
def test_maintenance_skips_staging_and_incomplete_bundles(
    tmp_path, monkeypatch, module_name, function_name, scoped
):
    root = tmp_path / "workspace"
    valid, local_name = _bundles(root)
    module = importlib.import_module(f"frisket.engine.jobs.{module_name}")
    calls = []

    def direct_open(path):
        calls.append((None, path))
        return Project(path)

    def scoped_open(key, path):
        calls.append((key, path))
        return Project(path)

    monkeypatch.setattr(module, "Project", direct_open)
    queue = SqliteJobQueue(tmp_path / "queue.db", hosted=scoped)
    try:
        getattr(module, function_name)(
            queue=queue,
            workspace_root=root,
            storage_org_id=23 if scoped else None,
            project_opener=scoped_open if scoped else None,
        )
    finally:
        queue.close()
    assert calls == (
        [(ProjectStorageKey(23, "valid"), valid)]
        if scoped
        else [(None, local_name), (None, valid)]
    )


def test_global_hosted_recovery_skips_staging_without_losing_valid_project(tmp_path):
    from frisket.engine.jobs.notifications_delivery import (
        reconcile_notification_delivery_requests,
    )

    root = tmp_path / "projects"
    valid, _ = _bundles(root / "23")
    _bundles(root / "not-an-org")
    calls = []

    def opener(key, path):
        calls.append((key, path))
        return Project(path)

    queue = SqliteJobQueue(tmp_path / "queue.db", hosted=True)
    try:
        reconcile_notification_delivery_requests(
            queue, workspace_root=root, project_opener=opener
        )
    finally:
        queue.close()
    assert calls == [(ProjectStorageKey(23, "valid"), valid)]


@pytest.mark.parametrize(
    "project_id", [".upload-staging", "My report", "CON", "../other"]
)
def test_explicit_invalid_scoped_identity_is_not_silently_skipped(tmp_path, project_id):
    with pytest.raises(ValueError):
        project_bundle_paths(tmp_path, project_id=project_id, require_slug=True)
    from frisket.engine.jobs.notifications_delivery import _recovery_targets

    with pytest.raises(ValueError):
        _recovery_targets(
            tmp_path,
            project_id=project_id,
            storage_org_id=None,
            project_opener=lambda key, path: None,
        )


@pytest.mark.parametrize(("module_name", "function_name"), SCANNERS[:-1])
def test_discovery_does_not_swallow_valid_project_processing_failure(
    tmp_path, module_name, function_name
):
    _bundles(tmp_path / "workspace")
    module = importlib.import_module(f"frisket.engine.jobs.{module_name}")

    def failing_opener(key, path):
        assert key == ProjectStorageKey(23, "valid")
        raise RuntimeError("valid project unavailable")

    queue = SqliteJobQueue(tmp_path / "queue.db", hosted=True)
    try:
        with pytest.raises(RuntimeError, match="valid project unavailable"):
            getattr(module, function_name)(
                queue=queue,
                workspace_root=tmp_path / "workspace",
                storage_org_id=23,
                project_opener=failing_opener,
            )
    finally:
        queue.close()
