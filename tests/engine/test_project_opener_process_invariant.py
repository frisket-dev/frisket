"""The invariant a downstream composition is buying: with an opener injected,
EVERY project open in the process routes through it.

This is deliberately NOT a set of per-site unit tests. It injects ONE counting
opener into a single ``Workspace`` and then drives representative,
architecturally distinct open paths in that same process:

* an HTTP route open (``Workspace.get`` via a real request),
* a worker job handler open (a claimed ``source.poll`` job), and
* a background scheduler-scan open (``enqueue_due_source_polls``).

Every ``frisket.store.Project`` construction across the modules that open
projects is booby-trapped to fail the test loudly. The proof is mechanical:
the counter advances on every path and the booby-trap never fires, so no code
path bypassed the seam.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import SqliteJobQueue
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
from frisket.project_identity import ProjectStorageKey
from frisket.server import workspace as workspace_module
from frisket.server.app import create_app
from frisket.engine.store import Project

# Every module that constructs a Project on a project-open path. If a new open
# site is added without threading the seam, its module's Project must be added
# here AND the site fixed — otherwise this booby-trap catches it.
_OPEN_SITE_MODULES = (
    "frisket.server.workspace",
    "frisket.engine.jobs.runs",
    "frisket.engine.jobs.enclosures",
    "frisket.engine.jobs.sources",
    "frisket.engine.jobs.embeddings",
    "frisket.engine.jobs.watches",
    "frisket.engine.jobs.notifications_delivery",
    "frisket.engine.jobs.notifications_digest",
    "frisket.engine.executor.action_jobs",
    "frisket.engine.executor.queue_terminalization",
)

_STORAGE_ORG_ID = 42


class _DirectOpenBypass(AssertionError):
    """A direct Project(...) open on an opener-bearing path is a seam bypass."""


@pytest.fixture
def _booby_trapped_direct_opens(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []

    def forbid(path: Any, *_: Any, **__: Any) -> Any:
        seen.append(Path(path))
        raise _DirectOpenBypass(f"direct Project open bypassed the seam: {path}")

    for name in _OPEN_SITE_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "Project"):
            monkeypatch.setattr(module, "Project", forbid)
    return seen


def test_one_injected_opener_serves_every_open_path_in_the_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _booby_trapped_direct_opens: list[Path],
) -> None:
    root = tmp_path / str(_STORAGE_ORG_ID)
    bundle = root / "trusted.frisket"
    created = Project.create(bundle, name="Trusted")
    created.close()
    key = ProjectStorageKey(storage_org_id=_STORAGE_ORG_ID, project_slug="trusted")

    calls: list[tuple[ProjectStorageKey, Path]] = []

    def counting_opener(storage_key: ProjectStorageKey, path: Path) -> Project:
        calls.append((storage_key, Path(path)))
        # The single real open in the whole test: constructed via the store
        # package directly so the booby-trapped module attributes never fire.
        return Project(path)

    queue = SqliteJobQueue(tmp_path / "queue.db", hosted=True)
    app = create_app(
        root,
        queue=queue,
        queue_payload_extra={
            "org_id": _STORAGE_ORG_ID,
            "storage_org_id": _STORAGE_ORG_ID,
        },
        project_opener=counting_opener,
        enable_provider_config=False,
    )
    ws: workspace_module.Workspace = app.state.workspace
    registry = ws.registry
    assert ws.project_opener is counting_opener

    try:
        # (1) HTTP ROUTE OPEN — a real request that resolves a project.
        client = TestClient(app)
        before = len(calls)
        response = client.get(f"/api/projects/{key.project_slug}/sheets")
        assert response.status_code == 200, response.text
        assert len(calls) > before, "route open did not go through the opener"
        assert calls[-1] == (key, bundle)

        # (2) WORKER JOB HANDLER OPEN — a claimed source.poll job. The payload
        # carries an ATTACKER project_id/workspace_root; only the claimed key
        # may be honored.
        calls.clear()
        handler = registry.get("source.poll")
        assert handler is not None
        try:
            handler(
                {
                    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
                    "project_id": "payload-attacker",
                    "storage_org_id": 999,
                    "workspace_root": str(tmp_path / "payload-root"),
                    "source_id": -1,
                },
                JobHandlerContext.without_job_row(),
            )
        except _DirectOpenBypass:
            raise
        except Exception:
            pass  # minimal business data; the OPEN is the proof
        assert calls, "worker job open did not go through the opener"
        assert all(call == (key, bundle) for call in calls)

        # (3) BACKGROUND SCHEDULER-SCAN OPEN — enqueue_due_source_polls, keyed
        # by the root's declared storage org, never the directory name.
        from frisket.engine.jobs import enqueue_due_source_polls

        calls.clear()
        enqueue_due_source_polls(
            workspace_root=root,
            queue=queue,
            storage_org_id=_STORAGE_ORG_ID,
            project_opener=counting_opener,
        )
        assert calls, "scheduler scan did not go through the opener"
        assert all(call == (key, bundle) for call in calls)
    finally:
        for project in list(ws._projects.values()):
            project.close()
        queue.close()

    # The booby-trap never fired on any path: zero direct opens.
    assert _booby_trapped_direct_opens == []
