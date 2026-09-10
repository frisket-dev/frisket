from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from frisket.engine.jobs import QUEUE_DB_NAME, open_queue
from frisket.engine.jobs.queue import PROJECT_RUN_KIND

_ISO = "2020-01-01T00:00:00.000000+00:00"


def _require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _insert_legacy_project_row(db_path: Path, *, storage_org_id: object) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO jobs (kind, payload, project_id, storage_org_id, status,"
            " attempts, max_attempts, available_at, created_at)"
            " VALUES (?, ?, ?, ?, 'queued', 0, 3, ?, ?)",
            (
                PROJECT_RUN_KIND,
                '{"project_id": "legacy"}',
                "legacy",
                storage_org_id,
                _ISO,
                _ISO,
            ),
        )
        con.commit()
    finally:
        con.close()


def test_declared_hosted_posture_enforces_storage_identity_on_a_sqlite_engine(
    tmp_path,
):
    _require(
        "hosted" in inspect.signature(open_queue).parameters,
        "open_queue must accept a declared `hosted` posture parameter so hosted "
        "tenancy semantics are testable on a SQLite engine without a Postgres "
        "container; hosted queue posture must be explicit",
    )

    hosted_dir = tmp_path / "hosted"
    hosted_dir.mkdir()
    hosted_queue = open_queue(workspace=hosted_dir, hosted=True)
    try:
        # Fail-closed ProjectStorageKey at enqueue: a project job with no storage
        # identity is rejected on a SQLite engine BECAUSE posture is declared
        # hosted, not because the dialect is Postgres.
        with pytest.raises(ValueError):
            hosted_queue.enqueue(PROJECT_RUN_KIND, {"project_id": "proj"})

        # A NULL-storage-identity project row is unclaimable under hosted posture.
        _insert_legacy_project_row(hosted_dir / QUEUE_DB_NAME, storage_org_id=None)
        assert hosted_queue.claim("hosted-worker") is None, (
            "a NULL-storage-identity project row must be unclaimable under "
            "declared hosted posture (fail closed), even on a SQLite engine"
        )

        # A well-formed identity enqueues, claims, and carries a storage key.
        hosted_queue.enqueue(
            PROJECT_RUN_KIND,
            {"project_id": "realslug", "storage_org_id": 42},
        )
        claimed = hosted_queue.claim("hosted-worker")
        assert claimed is not None and claimed.project_id == "realslug", (
            "a well-formed hosted project job must be claimable on a SQLite "
            "engine under declared hosted posture"
        )
        assert claimed.project_storage_key is not None, (
            "the claimed hosted row must carry a trusted ProjectStorageKey for "
            "claimed-row routing"
        )
    finally:
        hosted_queue.close()

    # Defaults preserve today's behavior: the local one-command path cannot
    # acquire hosted posture, so a project job without storage identity still
    # enqueues and claims.
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    local_queue = open_queue(workspace=local_dir)
    try:
        local_queue.enqueue(PROJECT_RUN_KIND, {"project_id": "proj"})
        claimed_local = local_queue.claim("local-worker")
        assert claimed_local is not None and claimed_local.project_id == "proj", (
            "the local (non-hosted) default must not fail-close a project job "
            "that lacks storage identity"
        )
    finally:
        local_queue.close()


def test_hosted_terminalization_routes_by_declared_root_not_ambient_env(
    tmp_path, monkeypatch
):
    """The terminalization hook resolves hosted projects under the declared
    registration root, never an ambient ``FRISKET_DATA_DIR``; a claim for
    another organization's scoped root fails closed."""
    from frisket.contracts.action import Receipt
    from frisket.engine.executor.queue_terminalization import (
        register_queue_terminalization,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    # A decoy data dir: were the env read still alive, cancel would route here.
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "decoy"))

    projects_root = tmp_path / "projects"
    project_root = projects_root / "42"
    project_root.mkdir(parents=True)
    project = Project.create(project_root / "slug.frisket", name="slug")
    try:
        ReceiptStore(project).insert_queued(
            Receipt(
                receipt_id="r-hosted",
                project_id="slug",
                action_id="a-1",
                action_kind="test.hosted",
                idempotency_key="test.hosted@sha256:x",
                params_hash="sha256:x",
                status="queued",
            )
        )
    finally:
        project.close()

    queue = open_queue(workspace=tmp_path / "queue", hosted=True)
    try:
        register_queue_terminalization(queue, workspace_root=projects_root)
        jid = queue.enqueue(
            ACTION_RUN_KIND,
            {
                "project_id": "slug",
                "storage_org_id": 42,
                "v1_receipt_id": "r-hosted",
                "action_kind": "test.hosted",
            },
        )
        assert queue.cancel(jid) is True
    finally:
        queue.close()

    reopened = Project(project_root / "slug.frisket")
    try:
        stored = ReceiptStore(reopened).find_by_id("r-hosted")
        assert stored is not None and stored.parsed().status == "cancelled", (
            "the hook must terminalize the receipt under the declared "
            "registration root, never an ambient FRISKET_DATA_DIR"
        )
    finally:
        reopened.close()
    assert not (tmp_path / "decoy").exists()


def test_hosted_terminalization_fails_closed_for_foreign_org_scoped_root(
    tmp_path,
):
    """An org-scoped registration root refuses a claim for any OTHER org: the
    row flips but no cross-tenant project write happens."""
    from frisket.contracts.action import Receipt
    from frisket.engine.executor.queue_terminalization import (
        register_queue_terminalization,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    org7_root = tmp_path / "projects" / "7"
    org7_root.mkdir(parents=True)
    project = Project.create(org7_root / "slug.frisket", name="slug")
    try:
        ReceiptStore(project).insert_queued(
            Receipt(
                receipt_id="r-foreign",
                project_id="slug",
                action_id="a-1",
                action_kind="test.hosted",
                idempotency_key="test.hosted@sha256:y",
                params_hash="sha256:y",
                status="queued",
            )
        )
    finally:
        project.close()

    queue = open_queue(workspace=tmp_path / "queue", hosted=True)
    try:
        # Registration declares the root as org 7's storage directory; the
        # job's claim names org 42 — resolution must fail closed.
        register_queue_terminalization(
            queue, workspace_root=org7_root, workspace_root_storage_org_id=7
        )
        jid = queue.enqueue(
            ACTION_RUN_KIND,
            {
                "project_id": "slug",
                "storage_org_id": 42,
                "v1_receipt_id": "r-foreign",
                "action_kind": "test.hosted",
            },
        )
        assert queue.cancel(jid) is True
    finally:
        queue.close()

    reopened = Project(org7_root / "slug.frisket")
    try:
        stored = ReceiptStore(reopened).find_by_id("r-foreign")
        assert stored is not None and stored.parsed().status == "queued", (
            "a mismatched-org claim must never terminalize into another "
            "tenant's project directory"
        )
    finally:
        reopened.close()
