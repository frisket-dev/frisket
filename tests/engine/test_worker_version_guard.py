"""Worker code identity for runs and worker heartbeats.

- ``frisket.engine.worker_version.code_version()`` resolves git describe,
  falling back to a root VERSION file, installed package metadata, then
  "unknown"; never raises.
- ``project.run`` stamps the claiming worker's own ``code_version()`` onto
  the ``runs`` row (``runs.worker_version``).
"""

from __future__ import annotations

import pytest

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

    @pytest.mark.parametrize(
        ("version_file", "git_identity", "installed_version", "expected"),
        [
            pytest.param(
                "a" * 40, "git-identity", "1.2.3", "git-identity", id="git_precedes_all"
            ),
            pytest.param(
                f"{'a' * 40}\n", None, "1.2.3", "a" * 40, id="sha_version_file"
            ),
            pytest.param(
                None, None, "1.2.3", "1.2.3", id="installed_metadata_fallback"
            ),
            pytest.param(
                f"{'a' * 40}+public.{'b' * 40}",
                None,
                "1.2.3",
                f"{'a' * 40}+public.{'b' * 40}",
                id="composite_image_identity",
            ),
            pytest.param(
                "v9.9.9\n", None, "1.2.3", "1.2.3", id="malformed_version_falls_through"
            ),
            pytest.param(
                b"\xff", None, "1.2.3", "1.2.3", id="invalid_utf8_falls_through"
            ),
            pytest.param(
                "A" * 40, None, None, "unknown", id="malformed_and_no_metadata"
            ),
            pytest.param(None, None, None, "unknown", id="nothing_available"),
        ],
    )
    def test_code_version_resolution_precedence(
        self,
        tmp_path,
        monkeypatch,
        version_file: str | bytes | None,
        git_identity: str | None,
        installed_version: str | None,
        expected: str,
    ):
        if isinstance(version_file, bytes):
            (tmp_path / "VERSION").write_bytes(version_file)
        elif isinstance(version_file, str):
            (tmp_path / "VERSION").write_text(version_file, encoding="utf-8")

        monkeypatch.setattr(worker_version, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(worker_version, "_git_describe", lambda _root: git_identity)
        if installed_version is None:
            version_lookup = self._package_not_found
        else:

            def version_lookup(distribution: str) -> str | None:
                return installed_version if distribution == "frisket-data" else None

        monkeypatch.setattr(
            worker_version.importlib_metadata, "version", version_lookup
        )
        worker_version.code_version.cache_clear()
        try:
            assert worker_version.code_version() == expected
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
