"""Project-location authority for the notification job family.

The sibling handlers (``source.poll``, ``watch.evaluate``, ``enclosure.download``,
``embedding.index_refresh``, ``project.run``/``action.run``) each derive WHERE a
project lives from the CLAIMED queue identity and fail closed when an injected
opener is present without one. This file pins the same invariant for the
notification handlers (``notification.deliver`` / ``notification.digest``).

Crucially, the registration here is the *unguarded* composition: the handlers
are registered directly with an injected ``project_opener`` but WITHOUT the
strict ``guarded_handler`` wrapper that ``register_production_handlers`` only
installs under ``require_storage_identity=True`` on a hosted queue. That wrapper
is exactly what the per-org ``Workspace`` composition does NOT install
(``frisket.server.workspace.Workspace.__init__`` calls
``register_production_handlers`` with the default ``require_storage_identity=
False``). So this test drives the handler body directly, the way that
composition does, and proves the handler itself — not an outer guard — enforces
the seam.

Attacker: a team-edition member with a valid session who
controls the job's request-body fields (``workspace_root`` / ``project_id`` /
``storage_org_id``) but not server code, the engine tables, or the trusted
queue columns from which the worker reconstructs the claimed
``ProjectStorageKey``. Before the fix, the notification ``_handle`` wrappers
built the bundle path from those payload fields, so a payload-set
``workspace_root`` under the attacker's own declared org opened sqlite at an
attacker-chosen directory keyed to the trusted slug — a slug-collision
cross-org read/write. This test is RED before the fix (no-claim opens instead
of raising; a claimed key does not override a hostile payload) and GREEN after.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.jobs import (
    JobHandlerContext,
    SqliteJobQueue,
    register_notification_digest_handlers,
    register_notification_handlers,
)
from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project


class _DirectOpenBypass(AssertionError):
    """A direct Project(...) open on an opener-bearing path is a seam bypass."""


def _booby_trap(monkeypatch: pytest.MonkeyPatch, seen: list[Path]) -> None:
    def forbid(path: Any, *_: Any, **__: Any) -> Any:
        seen.append(Path(path))
        raise _DirectOpenBypass(f"notification handler bypassed the opener: {path}")

    for name in (
        "frisket.engine.jobs.notifications_delivery",
        "frisket.engine.jobs.notifications_digest",
    ):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "Project", forbid)


def _digest_window_payload() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "cadence": "daily",
        "window_key": "2026-07-17",
        "window_start_at": (now - timedelta(days=1)).isoformat(),
        "window_end_at": now.isoformat(),
    }


def test_notification_handlers_derive_where_from_claimed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The global, org-unscoped hosted root: <data_dir>/projects. A claim's org
    # id is its per-org subdirectory here, so the trusted bundle for org 23 /
    # slug "trusted" is projects/23/trusted.frisket — NOT anything the payload
    # names.
    global_root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=23, project_slug="trusted")
    trusted_bundle = global_root / "23" / "trusted.frisket"
    Project.create(trusted_bundle, name="Trusted").close()

    # A hostile bundle the attacker would love the handler to open: a bundle
    # they control, at a slug that collides with a victim's, under their own
    # declared org. It must NEVER be opened on the claimed path.
    hostile_root = tmp_path / "attacker-root"
    hostile_bundle = hostile_root / "trusted.frisket"
    Project.create(hostile_bundle, name="Attacker owned").close()

    calls: list[tuple[ProjectStorageKey, Path]] = []

    def counting_opener(storage_key: ProjectStorageKey, path: Path) -> Project:
        calls.append((storage_key, Path(path)))
        return Project(path)

    direct_opens: list[Path] = []
    _booby_trap(monkeypatch, direct_opens)

    queue = SqliteJobQueue(tmp_path / "queue.db", hosted=True)
    registry = HandlerRegistry()
    try:
        register_notification_handlers(
            registry,
            workspace_root=global_root,
            queue=queue,
            project_opener=counting_opener,
        )
        register_notification_digest_handlers(
            registry,
            workspace_root=global_root,
            queue=queue,
            project_opener=counting_opener,
        )

        hostile_common = {
            "project_id": "trusted",  # slug collides with the victim's
            "storage_org_id": 999,  # attacker's own declared org
            "workspace_root": str(hostile_root),  # attacker-controlled directory
        }
        deliver = registry.get("notification.deliver")
        digest = registry.get("notification.digest")
        assert deliver is not None and digest is not None

        # --- With a CLAIMED key: WHERE comes ONLY from the claim. The hostile
        # payload workspace_root / project_id / storage_org_id are ignored.
        for kind, handler, extra in (
            ("deliver", deliver, {"request_id": 999}),
            (
                "digest",
                digest,
                {"route_id": 999, **_digest_window_payload()},
            ),
        ):
            calls.clear()
            direct_opens.clear()
            payload = {
                CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
                **hostile_common,
                **extra,
            }
            try:
                handler(payload, JobHandlerContext.without_job_row())
            except _DirectOpenBypass:
                raise
            except Exception:
                # Business data is deliberately absent (no such request/route);
                # the OPEN — its key and path — is the whole proof.
                pass
            assert direct_opens == [], f"{kind}: bypassed the injected opener"
            assert calls, f"{kind}: never opened through the injected opener"
            assert all(call == (key, trusted_bundle) for call in calls), (
                f"{kind}: opened an untrusted key/path instead of the claimed "
                f"identity: {calls}"
            )

        # --- With NO claimed key: an opener-bearing handler must FAIL CLOSED,
        # opening nothing, rather than fall back to the payload-derived path.
        for kind, handler, extra in (
            ("deliver", deliver, {"request_id": 999}),
            (
                "digest",
                digest,
                {"route_id": 999, **_digest_window_payload()},
            ),
        ):
            calls.clear()
            direct_opens.clear()
            payload = {**hostile_common, **extra}
            with pytest.raises(ValueError):
                handler(payload, JobHandlerContext.without_job_row())
            assert calls == [], f"{kind}: opened without a claimed storage identity"
            assert direct_opens == [], f"{kind}: fell back to a payload-derived path"
    finally:
        queue.close()
