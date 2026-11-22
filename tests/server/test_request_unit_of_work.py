"""A request is one unit of work: what it leaves uncommitted is rolled back.

A project write that succeeds and is followed by an exception before any
commit leaves the per-thread connection inside its implicit transaction.
When the request ends that transaction is rolled back, so a later request's
commit cannot publish it and its write lock does not block other writers.
"""

from __future__ import annotations
from contextlib import closing

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

from frisket.engine.jobs.queue import SqliteJobQueue
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store import Project
from frisket.engine.store.transactions import write_transaction
from frisket.server.routes.models_gateway import ModelsGatewayAPIRoute
from frisket.server.thread_worker import await_thread_worker
from tests.helpers import make_client


def _write_op(project: Project, label: str) -> None:
    project.db.execute(
        "INSERT INTO changes (kind, label, spec, undo_info, barrier) "
        "VALUES ('unit_of_work_probe', ?, '{}', '{}', 0)",
        (label,),
    )


def _write_lock_is_free(db_path: Path) -> bool:
    peer = sqlite3.connect(db_path, isolation_level=None, timeout=0)
    try:
        peer.execute("BEGIN IMMEDIATE")
        peer.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        peer.close()


def _committed_labels(db_path: Path) -> list[str]:
    with closing(sqlite3.connect(db_path)) as peer:
        return [
            row[0]
            for row in peer.execute(
                "SELECT label FROM changes WHERE kind='unit_of_work_probe' ORDER BY id"
            )
        ]


@pytest.fixture
def probe_client(tmp_path: Path) -> Iterator[tuple[TestClient, str, Path]]:
    """A client whose requests share one event loop and its threadpool, as a
    served app's do; a loop per request would retire every worker thread,
    and its connection, after each call."""
    with make_client(tmp_path) as client:
        yield _with_probe_routes(client)


def _with_probe_routes(client: TestClient) -> tuple[TestClient, str, Path]:
    created = client.post("/api/projects", json={"name": "Unit of work"})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    workspace = client.app.state.workspace
    app = client.app

    @app.post("/probe/{pid}/write-then-fail")
    def write_then_fail(pid: str) -> dict[str, str]:
        _write_op(workspace.get(pid), "failed sync")
        raise HTTPException(409, "refused after a write")

    @app.post("/probe/{pid}/async-write-then-fail")
    async def async_write_then_fail(pid: str) -> dict[str, str]:
        await asyncio.sleep(0)
        _write_op(workspace.get(pid), "failed async")
        raise HTTPException(409, "refused after a write")

    @app.post("/probe/{pid}/write-without-commit")
    def write_without_commit(pid: str) -> dict[str, str]:
        _write_op(workspace.get(pid), "never committed")
        return {"status": "ok"}

    @app.post("/probe/{pid}/owned-write")
    def owned_write(pid: str) -> dict[str, str]:
        project = workspace.get(pid)
        with write_transaction(project.db):
            _write_op(project, "first owned")
            # A nested checkout inside the live transaction is the same work.
            _write_op(workspace.get(pid), "nested checkout")
        return {"status": "ok"}

    @app.post("/probe/{pid}/handed-off-write-then-fail")
    async def handed_off_write_then_fail(pid: str) -> dict[str, str]:
        def work() -> None:
            _write_op(workspace.get(pid), "failed hand-off")
            raise HTTPException(409, "refused after a write")

        return await await_thread_worker(work)

    @app.post("/probe/{pid}/handed-off-owned-write")
    async def handed_off_owned_write(pid: str) -> dict[str, str]:
        def work() -> dict[str, str]:
            project = workspace.get(pid)
            with write_transaction(project.db):
                _write_op(project, "handed-off owned")
            return {"status": "ok"}

        return await await_thread_worker(work)

    gateway = APIRouter(route_class=ModelsGatewayAPIRoute)

    @gateway.post("/probe/{pid}/gateway-write-then-fail")
    def gateway_write_then_fail(pid: str) -> dict[str, str]:
        _write_op(workspace.get(pid), "failed gateway")
        raise HTTPException(409, "refused after a write")

    app.include_router(gateway)
    return client, project_id, workspace.get(project_id).db_path


def test_sync_request_that_fails_after_a_write_leaves_nothing_behind(probe_client):
    client, pid, db_path = probe_client

    failed = client.post(f"/probe/{pid}/write-then-fail")

    assert failed.status_code == 409
    assert failed.json()["detail"] == "refused after a write"
    assert _committed_labels(db_path) == []
    following = client.post(f"/probe/{pid}/owned-write")
    assert following.status_code == 200, following.text
    assert _committed_labels(db_path) == ["first owned", "nested checkout"]


def test_async_request_that_fails_after_a_write_leaves_nothing_behind(probe_client):
    client, pid, db_path = probe_client

    failed = client.post(f"/probe/{pid}/async-write-then-fail")

    assert failed.status_code == 409
    assert _committed_labels(db_path) == []
    following = client.post(f"/probe/{pid}/owned-write")
    assert following.status_code == 200, following.text
    assert _committed_labels(db_path) == ["first owned", "nested checkout"]


def test_a_request_that_never_commits_its_write_does_not_publish_it(probe_client):
    client, pid, db_path = probe_client

    assert client.post(f"/probe/{pid}/write-without-commit").status_code == 200
    assert client.post(f"/probe/{pid}/owned-write").status_code == 200

    assert _committed_labels(db_path) == ["first owned", "nested checkout"]


def test_handed_off_work_that_fails_after_a_write_leaves_nothing_behind(
    probe_client,
):
    client, pid, db_path = probe_client

    failed = client.post(f"/probe/{pid}/handed-off-write-then-fail")

    assert failed.status_code == 409
    assert _committed_labels(db_path) == []
    assert _write_lock_is_free(db_path)
    for _ in range(4):
        following = client.post(f"/probe/{pid}/handed-off-owned-write")
        assert following.status_code == 200, following.text
    assert _committed_labels(db_path) == ["handed-off owned"] * 4


def test_a_route_class_built_on_the_unit_of_work_route_ends_its_unit(probe_client):
    client, pid, db_path = probe_client

    failed = client.post(f"/probe/{pid}/gateway-write-then-fail")

    assert failed.status_code == 409
    assert _committed_labels(db_path) == []
    assert _write_lock_is_free(db_path)


def test_a_job_that_fails_after_a_write_leaves_nothing_behind(tmp_path):
    bundle = tmp_path / "job.frisket"
    Project.create(bundle, name="job").close()
    queue = SqliteJobQueue(tmp_path / "queue.db")
    registry = HandlerRegistry()

    def write_then_fail(payload: dict, _context: object) -> None:
        with closing(Project(payload["bundle"])) as project:
            _write_op(project, "failed job")
            raise RuntimeError("job failed after a write")

    registry.register("unit_of_work_probe", write_then_fail)
    job_id = queue.enqueue(
        "unit_of_work_probe", {"bundle": str(bundle)}, max_attempts=1
    )

    assert Worker(queue, registry, worker_id="unit-of-work").run_once()

    job = queue.get(job_id)
    assert job is not None and job.status == "failed"
    assert _committed_labels(bundle / "project.db") == []
    with closing(Project(bundle)) as project:
        with write_transaction(project.db):
            _write_op(project, "next job")
    assert _committed_labels(bundle / "project.db") == ["next job"]
