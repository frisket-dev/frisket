"""Worker code identity for runs and worker heartbeats.

- ``frisket.engine.worker_version.code_version()`` resolves git describe,
  falling back to a root VERSION file, installed package metadata, then
  "unknown"; never raises.
- ``project.run`` stamps the claiming worker's own ``code_version()`` onto
  the ``runs`` row (``runs.worker_version``).
"""

from __future__ import annotations

import frisket.engine.jobs.runs as runs_module
import frisket.engine.worker_version as worker_version
from frisket.engine.jobs.queue import SqliteJobQueue, jobs_table
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from http_test_helpers import (
    queued_python_run_spec,
    run_worker_until_job_terminal,
    v1_action_from_canonical_run_spec,
)


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

        job = run_worker_until_job_terminal(Worker(q, reg), q, jid)
        assert job.status == "done"
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
        run_worker_until_job_terminal(Worker(q, reg), q, jid)

        p2 = Project(workspace / "proj2.frisket")
        run = RunResultStore(p2).get_run(run_id)
        assert run["worker_version"] == "worker-commit-Y"
        assert run["status"] == "failed"
        p2.close()
        q.close()
