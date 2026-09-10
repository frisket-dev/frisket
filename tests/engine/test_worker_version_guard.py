"""Worker-version guard for a workspace shared by multiple processes.

Three stale worktree workers once shared a live workspace's
`.queue.db` with the canonical dev server and silently claimed jobs with
divergent code. This guards the class:

- ``frisket.worker_version.code_version()`` — the cheapest reliable code
  identity (git describe, falling back to a root VERSION file, installed
  package metadata, then "unknown"; never raises).
- Every ``JobQueue.enqueue()`` stamps the enqueuing process's OWN
  code_version() onto the job row (``jobs.code_version`` — additive column,
  both backends).
- ``Worker._execute`` compares a claimed job's stamp against ITS OWN
  code_version(): a mismatch is logged LOUDLY but remains diagnostic-only.
- ``project.run``'s handler additionally stamps the claiming worker's own
  code_version() onto the ``runs`` row itself (``runs.worker_version`` —
  additive column), so the run-detail panel (run-detail-full-params-v1) can
  show which code actually touched a run.
"""

from __future__ import annotations

import logging

import pytest

import frisket.engine.jobs.runs as runs_module
import frisket.engine.jobs.worker as worker_module
import frisket.engine.worker_version as worker_version
from frisket.engine.jobs import queue as queue_module
from frisket.engine.jobs.queue import SqliteJobQueue, jobs_table
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec


def _null_code_version(queue, job_id):
    """Blank a job's enqueue-time code_version on the queue engine, simulating a
    row written before worker-version-guard-v1 added the column."""
    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(code_version=None)
        )


@pytest.fixture
def queue(tmp_path):
    q = SqliteJobQueue(tmp_path / "q.db")
    yield q
    q.close()


class TestCodeVersion:
    @staticmethod
    def _package_not_found(_distribution: str) -> str:
        raise worker_version.importlib_metadata.PackageNotFoundError

    def test_git_describe_precedes_version_file_and_installed_metadata(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "VERSION").write_text("a" * 40, encoding="utf-8")
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            worker_version, "_git_describe", lambda _root: "git-identity"
        )
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            lambda _distribution: "1.2.3",
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == "git-identity"
        finally:
            worker_version.code_version.cache_clear()

    def test_version_file_sha_precedes_installed_metadata_when_git_absent(
        self, tmp_path, monkeypatch
    ):
        source_sha = "a" * 40
        (tmp_path / "VERSION").write_text(f"{source_sha}\n", encoding="utf-8")
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            lambda _distribution: "1.2.3",
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == source_sha
        finally:
            worker_version.code_version.cache_clear()

    def test_installed_package_fallback_when_git_and_version_file_absent(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            lambda distribution: "1.2.3" if distribution == "frisket-data" else None,
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == "1.2.3"
        finally:
            worker_version.code_version.cache_clear()

    def test_composite_image_version_identity_precedes_installed_metadata(
        self, tmp_path, monkeypatch
    ):
        identity = f"{'a' * 40}+public.{'b' * 40}"
        (tmp_path / "VERSION").write_text(identity, encoding="utf-8")
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            lambda _distribution: "1.2.3",
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == identity
        finally:
            worker_version.code_version.cache_clear()

    def test_malformed_version_file_falls_through_to_installed_metadata(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "VERSION").write_text("v9.9.9\n", encoding="utf-8")
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            lambda _distribution: "1.2.3",
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == "1.2.3"
        finally:
            worker_version.code_version.cache_clear()

    def test_invalid_utf8_version_file_falls_through_to_installed_metadata(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "VERSION").write_bytes(b"\xff")
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            lambda _distribution: "1.2.3",
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == "1.2.3"
        finally:
            worker_version.code_version.cache_clear()

    def test_malformed_version_file_and_missing_metadata_are_unknown(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "VERSION").write_text("A" * 40, encoding="utf-8")
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            self._package_not_found,
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == "unknown"
        finally:
            worker_version.code_version.cache_clear()

    def test_unknown_when_nothing_available(self, tmp_path, monkeypatch):
        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda root: None)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            self._package_not_found,
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == "unknown"
        finally:
            worker_version.code_version.cache_clear()

    def test_never_raises_when_git_binary_is_missing(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("no git binary")

        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version.subprocess, "run", boom)
        monkeypatch.setattr(
            worker_version.importlib_metadata,
            "version",
            self._package_not_found,
        )
        worker_version.code_version.cache_clear()
        try:
            # _git_describe swallows the subprocess OSError internally; this
            # pins that code_version() as a whole never propagates one.
            assert worker_version.code_version() == "unknown"
        finally:
            worker_version.code_version.cache_clear()


class TestQueueStampsEnqueuer:
    def test_enqueue_stamps_code_version_on_the_job(self, queue, monkeypatch):
        monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
        jid = queue.enqueue("echo", {"x": 1})
        assert queue.get(jid).code_version == "commit-A"

    def test_legacy_rows_get_null_code_version_not_an_error(self, tmp_path):
        """Opening a queue db is idempotent even when older rows predate the
        column (the ALTER-IF-MISSING migration path)."""
        q = SqliteJobQueue(tmp_path / "q.db")
        jid = q.enqueue("echo", {})
        # Simulate a pre-guard row.
        _null_code_version(q, jid)
        q.close()
        # Reopening (the migration re-running) must not blow away real data.
        q2 = SqliteJobQueue(tmp_path / "q.db")
        job = q2.get(jid)
        assert job.code_version is None
        assert job.payload == {}
        q2.close()


class TestClaimantMismatch:
    def test_mismatch_is_diagnostic_only_and_handler_completes_once(
        self, queue, monkeypatch, caplog
    ):
        monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
        jid = queue.enqueue("record", {"hello": "world"})
        monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
        # This retired deployment variable may still be inherited by a
        # long-lived service manager. It must not turn a release identity
        # mismatch into a second control plane.
        monkeypatch.setenv("FRISKET_WORKER_VERSION_REFUSE", "1")
        calls: list[dict] = []
        registry = HandlerRegistry()

        def record(payload, _context):
            calls.append(payload)
            return {"recorded": len(calls)}

        registry.register("record", record)
        w = Worker(queue, registry, worker_id="w1")
        with caplog.at_level(logging.WARNING, logger="frisket.worker"):
            assert w.run_once()
        job = queue.get(jid)
        assert job.status == "done"
        assert job.code_version == "commit-A"
        assert job.claimed_code_version == "commit-B"
        assert job.result == {"recorded": 1}
        assert calls == [{"hello": "world", "job_id": jid, "job_final_attempt": False}]
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("worker_version_mismatch" in r.getMessage() for r in warnings)

    def test_matching_versions_no_warning(self, queue, monkeypatch, caplog):
        monkeypatch.setattr(queue_module, "code_version", lambda: "same-commit")
        monkeypatch.setattr(worker_module, "code_version", lambda: "same-commit")
        jid = queue.enqueue("echo", {})
        w = Worker(queue, worker_id="w1")
        with caplog.at_level(logging.WARNING, logger="frisket.worker"):
            assert w.run_once()
        assert queue.get(jid).status == "done"
        assert not any(
            "worker_version_mismatch" in r.getMessage() for r in caplog.records
        )

    def test_legacy_job_without_stamp_is_not_a_mismatch(
        self, queue, monkeypatch, caplog
    ):
        jid = queue.enqueue("echo", {})
        _null_code_version(queue, jid)
        monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
        w = Worker(queue, worker_id="w1")
        with caplog.at_level(logging.WARNING, logger="frisket.worker"):
            assert w.run_once()
        assert queue.get(jid).status == "done"
        assert not any(
            "worker_version_mismatch" in r.getMessage() for r in caplog.records
        )


class TestRunRecordStamp:
    def test_project_run_stamps_worker_version_on_the_run_record(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        p = Project.create(workspace / "proj.frisket", name="proj")
        sheet = p.add_sheet("people")
        cols = {"first": p.add_column(sheet, "first")}
        p.add_rows(sheet, [{"first": "Ada"}], cols)
        p.close()

        q = SqliteJobQueue(workspace / ".queue.db")
        reg = HandlerRegistry()
        ws = Workspace(
            workspace,
            queue=q,
            registry=reg,
            enable_local_model_pull=False,
        )
        launched = ActionRunService(ws).run_action(
            "proj",
            v1_action_from_canonical_run_spec(
                queued_python_run_spec(sheet, "first", "out"),
                idempotency_key="worker-version/run-stamp@sha256:stable",
            ),
        )
        assert launched.status_code == 200, launched.payload
        assert launched.payload["status"] == "queued", launched.payload
        run_id = int(launched.payload["run_id"])
        jid = int(launched.payload["job_id"])
        monkeypatch.setattr(runs_module, "code_version", lambda: "worker-commit-Z")

        Worker(q, reg).run_once()

        assert q.get(jid).status == "done"
        p2 = Project(workspace / "proj.frisket")
        run = RunResultStore(p2).get_run(run_id)
        assert run["worker_version"] == "worker-commit-Z"
        assert run["status"] == "completed"
        p2.close()
        q.close()

    def test_run_stamped_even_when_handler_fails(self, tmp_path, monkeypatch):
        """A run that blows up mid-execution still shows WHICH code touched
        it — that is the whole point of the guard for debugging an incident
        after the fact."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        p = Project.create(workspace / "proj2.frisket", name="proj2")
        sheet = p.add_sheet("people")
        cols = {"first": p.add_column(sheet, "first")}
        p.add_rows(sheet, [{"first": "Ada"}], cols)
        p.close()
        q = SqliteJobQueue(workspace / ".queue.db")
        reg = HandlerRegistry()
        ws = Workspace(
            workspace,
            queue=q,
            registry=reg,
            enable_local_model_pull=False,
        )
        launched = ActionRunService(ws).run_action(
            "proj2",
            v1_action_from_canonical_run_spec(
                queued_python_run_spec(sheet, "first", "out"),
                idempotency_key="worker-version/run-failure-stamp@sha256:stable",
            ),
        )
        assert launched.status_code == 200, launched.payload
        assert launched.payload["status"] == "queued", launched.payload
        run_id = int(launched.payload["run_id"])
        jid = int(launched.payload["job_id"])
        with q.engine.begin() as cx:
            cx.execute(
                jobs_table.update().where(jobs_table.c.id == jid).values(max_attempts=1)
            )

        class FailingRunner:
            def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
                pass

            def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise RuntimeError("boom")

        monkeypatch.setattr(runs_module, "MapRunner", FailingRunner)
        monkeypatch.setattr(runs_module, "code_version", lambda: "worker-commit-Y")
        Worker(q, reg).run_once()

        p2 = Project(workspace / "proj2.frisket")
        run = RunResultStore(p2).get_run(run_id)
        assert run["worker_version"] == "worker-commit-Y"
        assert run["status"] == "failed"
        p2.close()
        q.close()
