"""Index work survives request/queue failures and yields between bounded claims."""

from contextlib import closing

import pytest

from frisket.engine.jobs import (
    HandlerRegistry,
    JobHandlerContext,
    SqliteJobQueue,
    Worker,
)
from frisket.engine.jobs import search_index
from frisket.engine.jobs.watches import (
    WATCH_EVALUATE_KIND,
    register_watch_evaluate_handler,
)
from frisket.engine.store import Project
from frisket.search import search_project
from frisket.server.workspace import Workspace


def _project(root):
    with closing(Project.create(root / "docs.frisket")) as project:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet, [{"body": f"needle {i}"} for i in range(9)], {"body": column}
        )


def test_worker_bounds_claims_and_recovers_deleted_sidecar(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(search_index, "INDEX_BATCH_SIZE", 2)
    monkeypatch.setattr(search_index, "BATCHES_PER_CLAIM", 1)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry, workspace_root=tmp_path, queue=queue
        )
        other = []
        registry.register("other", lambda *_: other.append(True))
        with closing(Project(tmp_path / "docs.frisket")) as project:
            payload = {"project_id": "docs"}
            first = search_index.enqueue_search_index(project, queue, payload)
            assert search_index.enqueue_search_index(project, queue, payload) == first
        queue.enqueue("other", {})
        worker = Worker(queue, registry)
        assert worker.run_once()
        assert queue.get(first).result["processed"] <= 2
        assert worker.run_once()
        assert other == [True]
        for _ in range(100):
            if not worker.run_once():
                break
        else:
            pytest.fail("maintenance never settled")
        with closing(Project(tmp_path / "docs.frisket")) as project:
            assert len(search_project(project, "needle", rerank="off")) == 9
            assert search_index.enqueue_search_index(project, queue, payload) is None
            (project.path / "project.search.db").unlink()
            assert (
                search_index.enqueue_search_index(project, queue, payload) is not None
            )
        for _ in range(100):
            if not worker.run_once():
                break
        with closing(Project(tmp_path / "docs.frisket")) as project:
            assert len(search_project(project, "needle", rerank="off")) == 9


def test_reopen_or_search_retries_failed_enqueue(tmp_path, monkeypatch):
    _project(tmp_path)
    workspace = Workspace(tmp_path)
    enqueue = workspace.queue.enqueue
    monkeypatch.setattr(
        workspace.queue,
        "enqueue",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("unavailable")),
    )
    project = workspace.get("docs")
    monkeypatch.setattr(workspace.queue, "enqueue", enqueue)
    job_id = project._frisket_schedule_search_index()
    assert workspace.queue.get(job_id).kind == search_index.SEARCH_INDEX_KIND
    project.close()
    workspace.queue.close()


def test_hosted_search_requires_claimed_storage_before_open(tmp_path):
    def forbidden(*_args):
        raise AssertionError("untrusted project was opened")

    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry, workspace_root=tmp_path, queue=queue, project_opener=forbidden
        )
        with pytest.raises(ValueError, match="claimed storage identity"):
            registry.get(search_index.SEARCH_INDEX_KIND)(
                {"project_id": "docs", "workspace_root": "/wrong"},
                JobHandlerContext.without_job_row(),
            )


def test_queued_fts_watch_drains_dirty_index_before_evaluation(tmp_path):
    _project(tmp_path)
    with closing(Project(tmp_path / "docs.frisket")) as project:
        watch_id = project.add_watch(
            "needle",
            scope="project",
            query={"kind": "fts", "q": "needle", "limit": 20},
        )
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        register_search_index_handler = search_index.register_search_index_handler
        register_search_index_handler(registry, workspace_root=tmp_path, queue=queue)
        register_watch_evaluate_handler(registry, workspace_root=tmp_path, queue=queue)
        job_id = queue.enqueue(
            WATCH_EVALUATE_KIND,
            {
                "project_id": "docs",
                "watch_id": watch_id,
                "workspace_root": str(tmp_path),
            },
        )

        worker = Worker(queue, registry)
        assert worker.run_once()
        assert queue.get(job_id).status == "done"
        with closing(Project(tmp_path / "docs.frisket")) as project:
            run = project.watch_latest_run(watch_id)
            assert run is not None
            assert run["status"] == "ok"
            assert run["matched_rows"] == 9
