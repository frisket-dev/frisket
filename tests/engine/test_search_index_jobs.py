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
from frisket.search_storage import RECLAIM_PENDING_KEY
from frisket.server.workspace import Workspace
from frisket.server.services.project_search import ProjectSearchService


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


def test_readonly_open_does_not_enqueue_but_first_search_does(tmp_path, monkeypatch):
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
    assert (
        workspace.queue.list_project_jobs("docs", kind=search_index.SEARCH_INDEX_KIND)
        == []
    )
    result = ProjectSearchService(workspace).search(
        "docs", q="needle", limit=10, mode="keyword", rerank="off"
    )
    assert result["indexing"] is True
    [job] = workspace.queue.list_project_jobs(
        "docs", kind=search_index.SEARCH_INDEX_KIND
    )
    assert job.status == "queued"
    project.close()
    workspace.queue.close()


def test_worker_retries_reclaim_without_blocking_search(tmp_path, monkeypatch):
    _project(tmp_path)
    with closing(Project(tmp_path / "docs.frisket")) as project:
        from frisket.search import drain_index, _sidecar

        drain_index(project)
        db = _sidecar(project)
        try:
            db.execute(
                "INSERT INTO fts_state(key,value) VALUES (?,?)",
                (RECLAIM_PENDING_KEY, "test"),
            )
            db.commit()
        finally:
            db.close()
        assert search_index.index_work_marker(project) == "reclaim"
        assert len(search_project(project, "needle", rerank="off")) == 9

    attempts = []
    reclaim = search_index.reclaim_search_storage

    def deferred_once(path):
        attempts.append(path)
        return False if len(attempts) == 1 else reclaim(path)

    monkeypatch.setattr(search_index, "reclaim_search_storage", deferred_once)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry, workspace_root=tmp_path, queue=queue
        )
        with closing(Project(tmp_path / "docs.frisket")) as project:
            job_id = search_index.enqueue_search_index(
                project, queue, {"project_id": "docs"}
            )
        assert job_id is not None
        worker = Worker(queue, registry, retry_base_seconds=0)

        assert worker.run_once()
        assert queue.get(job_id).status == "queued"
        with closing(Project(tmp_path / "docs.frisket")) as project:
            assert search_index.index_work_marker(project) == "reclaim"
            assert len(search_project(project, "needle", rerank="off")) == 9

        assert worker.run_once()
        assert queue.get(job_id).status == "done"
        assert len(attempts) == 2
        with closing(Project(tmp_path / "docs.frisket")) as project:
            assert search_index.index_work_marker(project) is None


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


def test_watch_edit_after_index_quantum_continues_without_false_run(
    tmp_path, monkeypatch
):
    _project(tmp_path)
    with closing(Project(tmp_path / "docs.frisket")) as project:
        watch_id = project.add_watch(
            "needle",
            scope="project",
            query={"kind": "fts", "q": "needle", "limit": 20},
        )
    original = search_index.process_index_batches
    injected = False

    def edit_after_complete(project, **kwargs):
        nonlocal injected
        result = original(project, **kwargs)
        if result[1] and not injected:
            injected = True
            sheet = int(project.sheets()[0]["id"])
            column = int(project.columns(sheet)[0]["id"])
            project.add_rows(sheet, [{"body": "needle late"}], {"body": column})
        return result

    monkeypatch.setattr(search_index, "process_index_batches", edit_after_complete)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry, workspace_root=tmp_path, queue=queue
        )
        register_watch_evaluate_handler(registry, workspace_root=tmp_path, queue=queue)
        first = queue.enqueue(
            WATCH_EVALUATE_KIND,
            {"project_id": "docs", "watch_id": watch_id},
        )
        worker = Worker(queue, registry)
        assert worker.run_once()
        assert queue.get(first).result["status"] == "indexing"
        with closing(Project(tmp_path / "docs.frisket")) as project:
            assert project.watch_runs_total(watch_id) == 0
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM notification_items"
                ).fetchone()[0]
                == 0
            )
        for _ in range(20):
            with closing(Project(tmp_path / "docs.frisket")) as project:
                if project.watch_runs_total(watch_id):
                    break
            assert worker.run_once()
        with closing(Project(tmp_path / "docs.frisket")) as project:
            run = project.watch_latest_run(watch_id)
            assert run["status"] == "ok"
            assert run["matched_rows"] == 10
            assert project.watch_runs_total(watch_id) == 1
