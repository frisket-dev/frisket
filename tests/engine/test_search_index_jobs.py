"""Unified retrieval maintenance on the existing project queue."""

from __future__ import annotations

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
from frisket.retrieval.encoding import EncodingError
from frisket.retrieval.store import RETRIEVAL_DB_NAME, RetrievalStore
from frisket.search import search_project
from frisket.server.workspace import Workspace


def _project(root):
    with closing(Project.create(root / "docs.frisket")) as project:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet, [{"body": f"needle {i}"} for i in range(9)], {"body": column}
        )


class _BusyOnceEncoder:
    def __init__(self) -> None:
        self.busy = True

    def available(self) -> bool:
        return True

    def encode(self, inputs, *, purpose, space, cancelled=None):
        if self.busy:
            raise EncodingError("engine_busy", "the shared encoder is occupied")
        return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]


class _InstallableEncoder:
    def __init__(self) -> None:
        self.installed = False

    def available(self) -> bool:
        return self.installed

    def encode(self, inputs, *, purpose, space, cancelled=None):
        assert self.installed
        return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]


def test_unavailable_encoder_stops_continuations_until_setup_enqueues(tmp_path):
    encoder = _InstallableEncoder()
    workspace = Workspace(
        tmp_path / "workspace",
        embedding_runtime=encoder,
        enable_local_model_pull=False,
    )
    project_id = workspace.create("Documents")["id"]
    project = workspace.get(project_id)
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(
            sheet,
            [{"body": f"needle {i}"} for i in range(9)],
            {"body": column},
        )

        project._frisket_schedule_search_index()
        worker = Worker(workspace.queue, workspace.registry)
        assert worker.run_once()
        assert workspace.queue.counts()["queued"] == 0
        with RetrievalStore(project).open() as store:
            statuses = dict(
                store.db.execute(
                    "SELECT status, COUNT(*) FROM preparation "
                    "WHERE space_id IS NOT NULL GROUP BY status"
                ).fetchall()
            )
        assert statuses == {"pending": 8, "unavailable": 1}

        encoder.installed = True
        workspace._embedding_setup_completed()
        assert workspace.queue.counts()["queued"] == 1
        for _ in range(20):
            if not worker.run_once():
                break
        else:
            pytest.fail("semantic preparation never drained after setup")

        with RetrievalStore(project).open() as store:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM preparation WHERE status != 'ready'"
                ).fetchone()[0]
                == 0
            )
    finally:
        project.close()
        workspace.queue.close()


def test_installed_encoder_does_not_continue_policy_disabled_work(tmp_path):
    encoder = _InstallableEncoder()
    encoder.installed = True
    project_path = tmp_path / "docs.frisket"
    with closing(Project.create(project_path)) as project:
        sheet = project.add_sheet("Media")
        column = project.add_column(sheet, "audio", type="audio")
        digest = project.add_blob(b"audio", mime="audio/wav")
        project.add_rows(
            sheet,
            [{"audio": {"blob": digest, "mime": "audio/wav"}}],
            {"audio": column},
        )

    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry,
            workspace_root=tmp_path,
            queue=queue,
            embedding_encoder=encoder,
        )
        with closing(Project(project_path)) as project:
            job_id = search_index.enqueue_search_index(
                project, queue, {"project_id": "docs"}
            )
        assert job_id is not None

        assert Worker(queue, registry).run_once()
        assert queue.counts()["queued"] == 0

    with RetrievalStore(project_path).open() as store:
        disabled = store.db.execute(
            "SELECT status,error_code FROM preparation WHERE channel='audio'"
        ).fetchone()
    assert tuple(disabled) == ("unavailable", "policy_disabled")


def test_engine_busy_retries_job_and_resumes_without_source_edit(tmp_path):
    _project(tmp_path)
    encoder = _BusyOnceEncoder()
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry,
            workspace_root=tmp_path,
            queue=queue,
            embedding_encoder=encoder,
        )
        with closing(Project(tmp_path / "docs.frisket")) as project:
            job_id = search_index.enqueue_search_index(
                project, queue, {"project_id": "docs"}
            )
        assert job_id is not None

        worker = Worker(queue, registry, retry_base_seconds=0)
        assert worker.run_once()
        retry = queue.get(job_id)
        assert retry is not None
        assert retry.status == "queued"
        assert retry.attempts == 1
        with RetrievalStore(tmp_path / "docs.frisket").open() as store:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM preparation WHERE status='preparing'"
                ).fetchone()[0]
                == 1
            )

        encoder.busy = False
        for _ in range(20):
            if not worker.run_once():
                break
        else:
            pytest.fail("retrieval preparation never drained after contention")

        finished = queue.get(job_id)
        assert finished is not None
        assert finished.status == "done"
        assert finished.attempts == 2
        with RetrievalStore(tmp_path / "docs.frisket").open() as store:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM preparation WHERE status!='ready'"
                ).fetchone()[0]
                == 0
            )


def test_worker_yields_between_claims_and_recovers_deleted_sidecar(
    tmp_path, monkeypatch
):
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
            assert (
                search_index.enqueue_search_index(
                    project, queue, payload, include_preparation=False
                )
                is None
            )
            (project.path / RETRIEVAL_DB_NAME).unlink()
            assert (
                search_index.enqueue_search_index(project, queue, payload) is not None
            )
        for _ in range(100):
            if not worker.run_once():
                break
        with closing(Project(tmp_path / "docs.frisket")) as project:
            assert len(search_project(project, "needle", rerank="off")) == 9


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


def test_queued_watch_waits_for_complete_unified_index(tmp_path):
    _project(tmp_path)
    with closing(Project(tmp_path / "docs.frisket")) as project:
        watch_id = project.add_watch(
            "needle",
            scope="project",
            query={"kind": "fts", "q": "needle", "limit": 20},
        )
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        search_index.register_search_index_handler(
            registry, workspace_root=tmp_path, queue=queue
        )
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
            with RetrievalStore(project).open() as store:
                assert (
                    store.db.execute(
                        "SELECT COUNT(*) FROM preparation WHERE status='pending'"
                    ).fetchone()[0]
                    == 9
                )


def test_keyword_watch_index_drain_does_not_wait_for_embedding_encoder(
    tmp_path, monkeypatch
):
    _project(tmp_path)
    with closing(Project(tmp_path / "docs.frisket")) as project:
        watch_id = project.add_watch(
            "needle",
            scope="project",
            query={"kind": "fts", "q": "needle", "limit": 20},
        )
    encoder = object()
    seen = []

    def capture_encoder(_project, **kwargs):
        seen.append(kwargs.get("embedding_encoder"))
        return 0, False

    monkeypatch.setattr(search_index, "process_index_batches", capture_encoder)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        register_watch_evaluate_handler(
            registry,
            workspace_root=tmp_path,
            queue=queue,
            embedding_encoder=encoder,  # type: ignore[arg-type]
        )
        queue.enqueue(
            WATCH_EVALUATE_KIND,
            {"project_id": "docs", "watch_id": watch_id},
        )

        assert Worker(queue, registry).run_once()

    assert seen == [None]


def test_edit_after_index_quantum_prevents_a_stale_watch_run(tmp_path, monkeypatch):
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
