"""Worker loop (frisket.jobs.worker), the `frisket worker` CLI entrypoint,
and the serve path's child-process spawn.

The loop is the SAME code over both backends; backend semantics live in
tests/test_job_queue.py, so the worker tests run on SQLite."""

import functools
import subprocess
import sys
import threading
from typing import Any

import pytest

import frisket.engine.jobs.runs as run_jobs
import frisket.engine.jobs.worker as worker_module
from frisket.engine.jobs import queue as queue_module
import frisket.engine.runner.map_runner as map_runner_module
from frisket.engine.jobs import (
    ECHO_KIND,
    HandlerRegistry,
    JobHandlerContext,
    RUN_PROJECT_KIND,
    SqliteJobQueue,
    Worker,
    WorkerPorts,
    default_registry,
    register_project_run_handler,
)
from frisket.ai.llm import ChaosConfig, ModelRouter, ResponseCache
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.sources import SourceStore
from frisket.execution.provider import open_execution_composition
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec


@pytest.fixture
def queue(tmp_path):
    q = SqliteJobQueue(tmp_path / "q.db")
    yield q
    q.close()


def drain(worker):
    while worker.run_once():
        pass


def make_regex_project(workspace, *, name="queued") -> tuple[Project, str, int]:
    """Seed a deterministic project for a canonical queued project.run."""

    project_id = name
    p = Project.create(workspace / f"{project_id}.frisket", name=name)
    sheet = p.add_sheet("people")
    cols = {
        "first": p.add_column(sheet, "first"),
        "last": p.add_column(sheet, "last"),
    }
    p.add_rows(
        sheet,
        [
            {"first": "Ada", "last": "Lovelace"},
            {"first": "Grace", "last": "Hopper"},
        ],
        cols,
    )
    return p, project_id, sheet


def enqueue_regex_project_run(
    workspace,
    queue,
    *,
    project_id: str,
    sheet_id: int,
    registry: HandlerRegistry | None = None,
    queue_payload_extra: dict | None = None,
):
    """Enter project.run through the receipt/claim-backed action boundary."""

    ws = Workspace(
        workspace,
        queue=queue,
        registry=registry or HandlerRegistry(),
        queue_payload_extra=queue_payload_extra,
        enable_local_model_pull=False,
    )
    response = ActionRunService(ws).run_action(
        project_id,
        v1_action_from_canonical_run_spec(
            queued_python_run_spec(sheet_id, "last", "display"),
            idempotency_key=f"{project_id}/worker-e2e@sha256:stable",
        ),
    )
    assert response.status_code == 200, response.payload
    launched = response.payload
    assert launched["status"] == "queued", launched
    assert launched["run_id"] is not None
    assert launched["job_id"] is not None
    return ws, int(launched["run_id"]), int(launched["job_id"])


class TestExecution:
    def test_echo_job_end_to_end(self, queue):
        jid = queue.enqueue(ECHO_KIND, {"hello": "world"})
        w = Worker(queue, default_registry())
        assert w.run_once()
        job = queue.get(jid)
        assert job.status == "done"
        # The worker injects the running job's id into every handler payload
        # (handlers self-identify by job_id); the echo handler reflects it back.
        assert job.result == {
            "hello": "world",
            "job_id": jid,
            "job_final_attempt": False,
        }

    def test_idle_run_once_returns_false(self, queue):
        assert not Worker(queue).run_once()

    def test_non_dict_result_wrapped(self, queue):
        reg = HandlerRegistry()
        reg.register("answer", lambda payload, _context: 42)
        jid = queue.enqueue("answer", {})
        Worker(queue, reg).run_once()
        assert queue.get(jid).result == {"value": 42}

    def test_handler_decorator_requires_exact_origin_and_delegates_once(self):
        reg = HandlerRegistry()
        calls: list[str] = []

        def base(_payload, _context):
            calls.append("base")
            return "ok"

        initial = reg.add("composed", base, origin="test.base")
        with pytest.raises(ValueError) as duplicate:
            reg.add("composed", base, origin="test.duplicate")
        assert "composed" in str(duplicate.value)
        assert "test.base" in str(duplicate.value)
        assert "test.duplicate" in str(duplicate.value)
        assert reg.get("composed") is base

        decorators: list[str] = []

        def decorate(handler):
            decorators.append("built")

            def wrapped(payload, context):
                calls.append("wrapper")
                return handler(payload, context)

            return wrapped

        installed = reg.decorate(
            "composed",
            expected=initial,
            decorator=decorate,
            origin="test.wrapper",
        )
        handler = reg.get("composed")
        assert handler is not None
        assert handler({}, JobHandlerContext.without_job_row()) == "ok"
        assert calls == ["wrapper", "base"]
        assert decorators == ["built"]
        assert reg.registration("composed", origin="test.wrapper") is installed

        with pytest.raises(ValueError):
            reg.decorate(
                "composed",
                expected=initial,
                decorator=decorate,
                origin="test.second_wrapper",
            )
        assert decorators == ["built"]
        assert reg.get("composed") is handler

    def test_unknown_kind_fails_without_retry(self, queue):
        jid = queue.enqueue("no-such-kind", {}, max_attempts=5)
        Worker(queue, default_registry()).run_once()
        job = queue.get(jid)
        assert job.status == "failed"
        assert "no handler registered" in job.error
        assert job.attempts == 1  # never retried

    def test_non_retryable_job_error_fails_immediately_without_retry(self, queue):
        """Any handler can force a terminal failure regardless of remaining
        attempts, distinct from the "no handler registered" special case.
        Every OTHER handler kind keeps retrying plain exceptions with backoff,
        as pinned by the sibling test below."""
        from frisket.engine.jobs.worker import NonRetryableJobError

        calls = []
        reg = HandlerRegistry()

        def handler(payload, _context):
            calls.append(1)
            raise NonRetryableJobError("some_terminal_code: this will never succeed")

        reg.register("terminal", handler)
        jid = queue.enqueue("terminal", {}, max_attempts=5)
        w = Worker(queue, reg)
        w.run_once()
        job = queue.get(jid)
        assert job.status == "failed"
        assert job.attempts == 1  # never retried despite max_attempts=5
        assert job.error == "some_terminal_code: this will never succeed"
        assert len(calls) == 1

    def test_handler_failure_persists_and_logs_one_safe_error(self, queue, caplog):
        sentinel = "sk-worker-failure-sentinel"
        reg = HandlerRegistry()

        def handler(payload, _context):
            local_secret = sentinel
            raise RuntimeError(f"api_key={local_secret}\nprovider exploded")

        reg.register("unsafe", handler)
        jid = queue.enqueue("unsafe", {}, max_attempts=1)
        worker = Worker(queue, reg, worker_id="safe-worker")

        with caplog.at_level("ERROR", logger="frisket.worker"):
            assert worker.run_once()

        job = queue.get(jid)
        assert job.status == "failed"
        assert job.attempts == 1
        assert job.error == ("worker_exception: api_key=[REDACTED] provider exploded")
        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "job_failed"
        )
        assert record.error == job.error
        assert record.error_code == "worker_exception"
        assert not record.exc_info
        assert len(record.exception_frames) <= 12
        assert sentinel not in str(record.__dict__)
        assert "Traceback (most recent call last)" not in str(record.__dict__)

    def test_failing_handler_retries_with_backoff_then_fails(self, tmp_path):
        from deterministic_time import controlled_time

        calls = []
        reg = HandlerRegistry()

        def boom(payload, _context):
            calls.append(1)
            raise RuntimeError("kaput")

        reg.register("boom", boom)
        with controlled_time() as t:
            queue = t.queue(tmp_path / "q.db")
            jid = queue.enqueue("boom", {}, max_attempts=3)
            w = t.worker(queue, reg, retry_base_seconds=0.01)
            w.run_once()
            job = queue.get(jid)
            assert job.status == "queued"  # requeued for retry
            assert "kaput" in job.error
            assert job.available_at > job.created_at  # backoff applied
            for _ in range(2):
                t.advance_seconds(1)  # past the (tiny) backoff
                assert w.run_once()
            job = queue.get(jid)
            assert job.status == "failed"
            assert job.attempts == 3
            assert len(calls) == 3

    def test_heartbeat_keeps_long_job_alive(self, tmp_path):
        """A handler outliving its initial lease still completes (the
        heartbeat thread renews) and is NOT stolen by recovery.

        Deterministic shape: the queue runs on the controlled clock, so
        "past the original lease" is an advance of the clock, and the
        negative assertion (recovery steals nothing) holds under a frozen
        clock — the renewed lease cannot expire between the renewal and the
        check."""
        from deterministic_time import controlled_time

        with controlled_time() as t:
            queue = t.queue(tmp_path / "q.db")
            reg = HandlerRegistry()
            gate = t.gate()
            reg.register("slow", lambda payload, _context: gate(payload))
            jid = queue.enqueue("slow", {})
            # lease_seconds also sets the heartbeat cadence (lease/3,
            # floored at 50ms real) — small, so the renewal arrives promptly.
            w = t.worker(queue, reg, lease_seconds=0.15)
            t.background(w.run_once)
            t.wait_entered()  # the job is provably mid-handler
            t.advance_seconds(10)  # far past the original lease
            # Positive await: the heartbeat thread renews against the
            # advanced clock.
            t.wait_until(
                lambda: queue.get(jid).lease_expires_at > t.now(),
                message="lease heartbeat never renewed past the advanced clock",
            )
            assert queue.recover_expired() == 0
            t.release()
            t.wait_finalized(jid)
            assert queue.get(jid).status == "done"

    def test_project_run_job_executes_prepared_run(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        p, project_id, sheet = make_regex_project(workspace)
        p.close()
        q = SqliteJobQueue(workspace / ".queue.db")
        reg = default_registry()
        _ws, run_id, jid = enqueue_regex_project_run(
            workspace,
            q,
            project_id=project_id,
            sheet_id=sheet,
            registry=reg,
        )

        Worker(q, reg).run_once()

        job = q.get(jid)
        assert job.status == "done"
        p2 = Project(workspace / f"{project_id}.frisket")
        run = p2.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "completed"
        assert run["completed_rows"] == 2
        col = next(c for c in p2.columns(sheet) if c["name"] == "display")
        assert col["current_run_id"] == run_id
        assert list(p2.get_values(sheet, col["id"]).values()) == [
            "Lovelace",
            "Hopper",
        ]
        p2.close()
        q.close()

    def test_project_run_uses_a_fresh_execution_router_for_each_job(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        cache = ResponseCache(tmp_path / "shared-worker.cache.db")
        chaos = ChaosConfig(seed=398, enabled=True)

        class Policy:
            def __init__(self, run_id=None):
                self.run_id = run_id

            def bound(self, run_id):
                return Policy(run_id)

            def prepare(self, request, credential_source):
                del credential_source
                return request

            def before_live(self, request, credential_source):
                del request, credential_source

            def after_live(self, request, response, credential_source):
                del request, response, credential_source

        bases = []

        def fresh_router():
            router = ModelRouter(
                cache=cache,
                cache_mode="fresh",
                chaos=chaos,
                max_retries=7,
                use_env_keys=False,
                model_call_policy=Policy(),
            )
            bases.append(router)
            return router

        composition_routers = []

        def compose(project, router, context):
            router.model_call_policy = router.model_call_policy.bound(context.run_id)
            composition_routers.append(router)
            return open_execution_composition(project, router, context)

        runner_routers = []
        real_map_runner = run_jobs.MapRunner

        def capture_map_runner(project, router, **kwargs):
            runner_routers.append(router)
            return real_map_runner(project, router, **kwargs)

        monkeypatch.setattr(run_jobs, "MapRunner", capture_map_runner)
        monkeypatch.setattr(
            run_jobs,
            "_project_model_keys",
            lambda _project: {"gemini": "project-key"},
        )
        registry = HandlerRegistry()
        queue = SqliteJobQueue(workspace / ".queue.db")
        ws = Workspace(
            workspace,
            router=ModelRouter(cache=cache, cache_mode="fresh", use_env_keys=False),
            queue=queue,
            registry=registry,
            execution_router_factory=fresh_router,
            execution_composition_factory=compose,
            enable_local_model_pull=False,
        )
        launched = []
        for name in ("first", "second"):
            project, project_id, sheet_id = make_regex_project(workspace, name=name)
            project.close()
            response = ActionRunService(ws).run_action(
                project_id,
                v1_action_from_canonical_run_spec(
                    queued_python_run_spec(sheet_id, "last", "display"),
                    idempotency_key=f"{project_id}/fresh-router@sha256:stable",
                ),
            )
            assert response.status_code == 200, response.payload
            launched.append(response.payload)

        # One request launch owns one router from its missing-key gate through
        # queued admission. The worker will deliberately acquire another.
        assert len(bases) == 2
        assert bases[0] is not bases[1]
        assert composition_routers == bases

        # Admission also composes a router. Isolate the worker executions this
        # test is proving from those already-completed request compositions.
        bases.clear()
        composition_routers.clear()

        try:
            worker = Worker(queue, registry)
            assert worker.run_once()
            assert worker.run_once()
            for result in launched:
                job = queue.get(int(result["job_id"]))
                assert job is not None and job.status == "done", job.error
            assert len(bases) == 2
            assert bases[0] is not bases[1]
            assert composition_routers == runner_routers
            assert composition_routers[0] is not composition_routers[1]
            assert all(router.cache is cache for router in composition_routers)
            assert all(router.max_retries == 7 for router in composition_routers)
            assert all(
                router.chaos is not None and router.chaos.config == chaos
                for router in composition_routers
            )
            assert all(
                router.configured_key_sources() == {"gemini": "project_key"}
                for router in composition_routers
            )
            assert all(
                router is not base for router, base in zip(composition_routers, bases)
            )
            assert all(base.model_call_policy.run_id is None for base in bases)
            assert [router.model_call_policy.run_id for router in runner_routers] == [
                launched[0]["run_id"],
                launched[1]["run_id"],
            ]
        finally:
            queue.close()
            cache.close()

    def test_managed_post_claim_exception_preserves_reconciliation_tuple(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        project, project_id, sheet_id = make_regex_project(workspace)
        project.close()
        queue = SqliteJobQueue(workspace / ".queue.db")
        registry = default_registry()
        _workspace, run_id, job_id = enqueue_regex_project_run(
            workspace,
            queue,
            project_id=project_id,
            sheet_id=sheet_id,
            registry=registry,
        )

        def fail_terminal_seal(*_args, **_kwargs):
            raise RuntimeError("injected post-claim terminal failure")

        monkeypatch.setattr(
            map_runner_module,
            "finalize_dispatched_run",
            fail_terminal_seal,
        )
        handler = registry.get(RUN_PROJECT_KIND)
        assert handler is not None
        job = queue.get(job_id)
        assert job is not None
        payload = dict(job.payload)
        payload["job_id"] = job_id
        payload["job_final_attempt"] = True
        with pytest.raises(RuntimeError, match="post-claim terminal failure"):
            handler(payload, JobHandlerContext.without_job_row())

        reopened = Project(workspace / f"{project_id}.frisket")
        try:
            run = reopened.db.execute(
                "SELECT status,current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            assert run is not None
            assert run["status"] == "running"
            assert run["current_attempt_id"] is not None
            attempt = reopened.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (run["current_attempt_id"],),
            ).fetchone()
            assert attempt is not None and attempt["state"] == "dispatching"
            binding = reopened.db.execute(
                "SELECT state FROM run_output_generations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            assert binding is not None and binding["state"] == "active"
            claim = reopened.db.execute(
                "SELECT status FROM output_column_claims WHERE run_id=?",
                (run_id,),
            ).fetchone()
            assert claim is not None and claim["status"] == "active"
        finally:
            reopened.close()
            queue.close()

    def test_settlement_receives_job_row_org_not_mutated_payload_org(self, tmp_path):
        """Settlement identity crosses the worker/port seam outside payload.

        The persisted payload agrees with the immutable row, so the worker's
        normal reconciliation passes. An interposer then mutates the ordinary
        payload field before the project handler runs. The separately carried
        row fact must still reach settlement; changing the call site back to
        ``payload['org_id']`` makes this test fail with the other org.
        """
        trusted_job_org_id = 7001
        mutated_payload_org_id = 7002
        workspace = tmp_path / "ws"
        workspace.mkdir()
        project, project_id, sheet_id = make_regex_project(workspace)
        queue = SqliteJobQueue(workspace / ".queue.db")
        _ws, run_id, job_id = enqueue_regex_project_run(
            workspace,
            queue,
            project_id=project_id,
            sheet_id=sheet_id,
            queue_payload_extra={"org_id": trusted_job_org_id},
        )
        assert RunResultStore(project).request_cancel(run_id)
        project.close()

        class RecordingSettlement:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def settle_run(self, **kwargs: Any) -> None:
                self.calls.append(dict(kwargs))

            def settle_action_receipt(self, **_kwargs: Any) -> None:
                pass

        settlement = RecordingSettlement()
        registry = HandlerRegistry()
        register_project_run_handler(
            registry,
            workspace_root=workspace,
            worker_ports=WorkerPorts(settlement_port=settlement),
        )

        def mutate_payload_org(payload: dict) -> dict:
            changed = dict(payload)
            changed["org_id"] = mutated_payload_org_id
            return changed

        project_run_handler = registry.get(RUN_PROJECT_KIND)
        assert project_run_handler is not None

        def interposed_project_run(payload: dict, context: JobHandlerContext) -> object:
            return project_run_handler(mutate_payload_org(payload), context)

        registry.register(RUN_PROJECT_KIND, interposed_project_run)
        assert Worker(queue, registry, worker_id="settlement-org-seam").run_once()

        job = queue.get(job_id)
        assert job.status == "done", job.error
        assert len(settlement.calls) == 1
        call = settlement.calls[0]
        assert call["terminal_status"] == "cancelled"
        assert call["trusted_job_org_id"] == trusted_job_org_id
        assert call["trusted_job_org_id"] != mutated_payload_org_id
        queue.close()

    def test_project_run_failure_cleanup_keeps_original_error(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        q = SqliteJobQueue(workspace / ".queue.db")
        p, project_id, sheet = make_regex_project(workspace)
        _ws, run_id, jid = enqueue_regex_project_run(
            workspace,
            q,
            project_id=project_id,
            sheet_id=sheet,
        )
        p.close()
        with q.engine.begin() as connection:
            connection.execute(
                queue_module.jobs_table.update()
                .where(queue_module.jobs_table.c.id == jid)
                .values(max_attempts=1)
            )

        class FailingRunner:
            def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
                pass

            def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise RuntimeError("original failure")

        def failing_finish_run(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("cleanup failure")

        monkeypatch.setattr(run_jobs, "MapRunner", FailingRunner)
        monkeypatch.setattr(run_jobs.RunResultStore, "finish_run", failing_finish_run)
        reg = default_registry()
        register_project_run_handler(reg, workspace_root=workspace)

        Worker(q, reg).run_once()

        job = q.get(jid)
        assert job.status == "failed"
        assert job.error == "worker_exception: original failure"
        # Exception notes are traceback metadata and are not persisted.
        assert "cleanup failure" not in job.error
        q.close()


class TestConcurrencyAndRecovery:
    def test_two_workers_no_double_execution(self, queue):
        from deterministic_time import controlled_time

        n = 20
        seen: list[int] = []
        lock = threading.Lock()
        reg = HandlerRegistry()

        def record(payload, _context):
            with lock:
                seen.append(payload["i"])
            return {}

        reg.register("record", record)
        for i in range(n):
            queue.enqueue("record", {"i": i})
        workers = [Worker(queue, reg, worker_id=f"w{k}") for k in range(2)]
        with controlled_time() as t:
            threads = [t.background(functools.partial(drain, w)) for w in workers]
            for thread in threads:
                thread.join(timeout=30)
        assert sorted(seen) == list(range(n))  # every job exactly once
        assert queue.counts()["done"] == n

    def test_dead_a_worker_job_recovers_and_finishes_under_b(
        self, tmp_path, monkeypatch
    ):
        """Simulate a SIGKILLed worker: claim with a tiny lease and never
        heartbeat/complete. The next worker's poll recovers and runs it.

        Deterministic shape: the queue runs on the controlled clock, so
        "past the tiny lease" is an advance of the clock, never a real
        sleep racing a 0.01s lease."""
        from deterministic_time import controlled_time

        with controlled_time() as t:
            queue = t.queue(tmp_path / "q.db")
            monkeypatch.setattr(queue_module, "code_version", lambda: "commit-A")
            jid = queue.enqueue(ECHO_KIND, {"x": 1}, max_attempts=3)
            dead = queue.claim("dead-worker", lease_seconds=0.01)
            assert dead.id == jid
            t.advance_seconds(1)
            monkeypatch.setattr(queue_module, "code_version", lambda: "commit-B")
            monkeypatch.setattr(worker_module, "code_version", lambda: "commit-B")
            w = t.worker(queue, default_registry(), worker_id="survivor")
            assert w.run_once()  # recover_expired + claim + execute
            job = queue.get(jid)
            assert job.status == "done"
            assert job.result == {
                "x": 1,
                "job_id": jid,
                "job_final_attempt": False,
            }  # worker injects job_id + attempt finality
            assert job.attempts == 2  # the dead claim counted
            assert job.code_version == "commit-A"
            assert job.claimed_code_version == "commit-B"

    def test_run_forever_drain_and_stop(self, queue):
        from deterministic_time import controlled_time

        for i in range(3):
            queue.enqueue(ECHO_KIND, {"i": i})
        w = Worker(queue, default_registry())
        w.run_forever(drain=True)  # exits at first idle poll
        assert queue.counts()["done"] == 3
        # stop event halts a live loop
        stop = threading.Event()
        with controlled_time() as t:
            thread = t.background(functools.partial(w.run_forever, stop, drain=False))
            stop.set()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_workspace_and_cli_worker_register_same_production_job_kinds(
    tmp_path, monkeypatch
):
    from frisket import cli
    import frisket.engine.jobs as jobs
    from frisket.server.workspace import Workspace

    assert hasattr(jobs, "register_production_handlers")
    workspace_registry = HandlerRegistry()
    workspace_queue = SqliteJobQueue(tmp_path / "workspace.queue.db")
    workspace = Workspace(
        tmp_path / "workspace",
        queue=workspace_queue,
        registry=workspace_registry,
    )

    captured: dict[str, HandlerRegistry] = {}

    def capture_run_forever(self, stop=None, *, drain=False):
        captured["registry"] = self.registry

    monkeypatch.setattr(Worker, "run_forever", capture_run_forever)
    assert (
        cli.worker(
            [
                str(tmp_path / "cli"),
                "--drain",
                "--schedule-sources-interval",
                "0",
                "--schedule-embeddings-interval",
                "0",
                "--schedule-notification-digests-interval",
                "0",
            ]
        )
        == 0
    )

    assert "registry" in captured
    assert set(workspace.registry.kinds()) == (
        set(captured["registry"].kinds()) - {ECHO_KIND}
    )
    workspace_queue.close()


class TestCli:
    def test_worker_cli_drains_queue(self, tmp_path):
        """The `frisket worker <ws> --drain` entrypoint, in-process."""
        from frisket import cli
        from frisket.engine.jobs import open_queue

        q = open_queue(workspace=tmp_path)
        jid = q.enqueue(ECHO_KIND, {"via": "cli"})
        assert cli.worker([str(tmp_path), "--drain"]) == 0
        assert q.get(jid).status == "done"
        assert q.get(jid).result == {
            "via": "cli",
            "job_id": jid,
            "job_final_attempt": False,
        }  # worker injects job_id + attempt finality
        q.close()

    def test_worker_cli_drains_real_project_run(self, tmp_path):
        """The CLI worker registry executes real project.run jobs, not only echo."""
        from frisket import cli
        from frisket.engine.jobs import open_queue
        from frisket.engine.store import Project

        p, project_id, sheet = make_regex_project(tmp_path, name="cli-real-run")
        p.close()
        q = open_queue(workspace=tmp_path)
        _ws, run_id, _job_id = enqueue_regex_project_run(
            tmp_path,
            q,
            project_id=project_id,
            sheet_id=sheet,
        )

        assert cli.worker([str(tmp_path), "--drain"]) == 0

        p2 = Project(tmp_path / f"{project_id}.frisket")
        run = p2.db.execute(
            "SELECT status, completed_rows FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert dict(run) == {"status": "completed", "completed_rows": 2}
        p2.close()
        q.close()

    def test_worker_cli_database_url_registers_real_handlers(
        self, tmp_path, monkeypatch
    ):
        """Postgres/control-plane mode still executes real jobs, not only echo."""
        from frisket import cli
        from frisket.engine.jobs import open_queue
        from frisket.engine.store import Project

        data_dir = tmp_path / "data"
        project_root = data_dir / "projects" / "1"
        project_root.mkdir(parents=True)
        monkeypatch.setenv("FRISKET_DATA_DIR", str(data_dir))
        monkeypatch.setenv("FRISKET_PROJECTS_ROOT", str(project_root))
        p, project_id, sheet = make_regex_project(project_root, name="pg-real-run")
        p.close()
        db_url = f"sqlite:///{tmp_path}/control-queue.db"
        q = open_queue(database_url=db_url)
        _ws, run_id, jid = enqueue_regex_project_run(
            project_root,
            q,
            project_id=project_id,
            sheet_id=sheet,
        )
        assert (
            cli.worker(
                [
                    "--database-url",
                    db_url,
                    "--drain",
                    "--schedule-sources-interval",
                    "0",
                ]
            )
            == 0
        )

        assert q.get(jid).status == "done"
        p2 = Project(project_root / f"{project_id}.frisket")
        run = p2.db.execute(
            "SELECT status, completed_rows FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        job_result = q.get(jid).result or {}
        errors = job_result.get("action_result", {}).get("errors", [])
        assert dict(run) == {
            "status": "completed",
            "completed_rows": 2,
        }, [(error.get("code"), error.get("message")) for error in errors[:1]]
        p2.close()
        q.close()

    @pytest.mark.parametrize("trusted_org_id", [None, 7])
    def test_missing_consent_principal_distinguishes_local_and_trusted_org(
        self, tmp_path, trusted_org_id
    ):
        """Storage ownership is not a funding-org claim; a real org stays gated."""
        projects_root = tmp_path / "projects"
        project_root = projects_root / "1"
        project_root.mkdir(parents=True)
        project, project_id, sheet = make_regex_project(
            project_root, name=f"missing-principal-{trusted_org_id}"
        )
        project.close()
        queue = SqliteJobQueue(tmp_path / "missing-principal.queue.db")
        queue_payload_extra = {"storage_org_id": 1}
        if trusted_org_id is not None:
            queue_payload_extra["org_id"] = trusted_org_id
        _workspace, run_id, job_id = enqueue_regex_project_run(
            project_root,
            queue,
            project_id=project_id,
            sheet_id=sheet,
            queue_payload_extra=queue_payload_extra,
        )
        prepared = Project(project_root / f"{project_id}.frisket")
        prepared.db.execute(
            "UPDATE runs SET consent_principal=NULL WHERE id=?", (run_id,)
        )
        prepared.db.commit()
        prepared.close()
        registry = HandlerRegistry()
        register_project_run_handler(
            registry,
            workspace_root=projects_root,
        )

        assert Worker(queue, registry).run_once()

        job = queue.get(job_id)
        assert job is not None and job.status == "done"
        assert job.result is not None
        errors = job.result["action_result"]["errors"]
        reopened = Project(project_root / f"{project_id}.frisket")
        run = reopened.db.execute(
            "SELECT status, completed_rows FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if trusted_org_id is None:
            assert errors == []
            assert dict(run) == {"status": "completed", "completed_rows": 2}
        else:
            assert errors[0]["code"] == "consent_missing"
            assert dict(run) == {"status": "failed", "completed_rows": 0}
        reopened.close()
        queue.close()

    def test_worker_cli_drains_real_source_poll(self, tmp_path, monkeypatch):
        """The CLI worker executes source.poll jobs and honors payload root.

        This mirrors compose/hosted: the worker polls a shared queue directory,
        while the source job carries the org/project workspace root internally.
        """
        import httpx

        from frisket import cli, ingest
        from frisket.engine.jobs import SOURCE_POLL_KIND, open_queue
        from frisket.engine.store import Project

        project_root = tmp_path / "projects" / "1"
        queue_root = tmp_path / "queue"
        project_root.mkdir(parents=True)
        p = Project.create(project_root / "source-cli.frisket", name="source-cli")
        sid = SourceStore(p).add_source(
            name="CLI Feed",
            kind="rss",
            url="https://example.com/feed.xml",
            schedule="@hourly",
        )
        p.close()
        monkeypatch.setattr(ingest, "url_is_safe", lambda url: True)
        feed = (
            "<?xml version='1.0'?><rss version='2.0'><channel>"
            "<title>CLI Feed</title><item><guid>a</guid><title>A</title>"
            "<link>https://x/a</link></item></channel></rss>"
        ).encode()

        class FakeResponse:
            is_redirect = False
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def iter_bytes(self):
                yield feed

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url):
                assert method == "GET"
                assert url == "https://example.com/feed.xml"
                return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        q = open_queue(workspace=queue_root)
        jid = q.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "source-cli",
                "source_id": sid,
                "workspace_root": str(project_root),
            },
            max_attempts=1,
        )

        assert cli.worker([str(queue_root), "--drain"]) == 0

        job = q.get(jid)
        assert job.status == "done"
        assert job.result["new_rows"] == 1
        p2 = Project(project_root / "source-cli.frisket")
        assert SourceStore(p2).source_runs(sid)[0]["status"] == "ok"
        p2.close()
        q.close()

    def test_worker_cli_schedules_due_source_before_drain(self, tmp_path, monkeypatch):
        """`frisket worker <workspace> --drain` schedules due RSS sources once."""
        import httpx

        from frisket import cli, ingest
        from frisket.engine.jobs import SOURCE_POLL_KIND, open_queue
        from frisket.engine.store import Project

        monkeypatch.delenv("FRISKET_DATA_DIR", raising=False)
        monkeypatch.setattr(ingest, "url_is_safe", lambda url: True)
        feed = (
            "<?xml version='1.0'?><rss version='2.0'><channel>"
            "<title>Scheduled Feed</title><item><guid>a</guid><title>A</title>"
            "<link>https://x/a</link></item></channel></rss>"
        ).encode()

        class FakeResponse:
            is_redirect = False
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def iter_bytes(self):
                yield feed

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url):
                assert method == "GET"
                assert url == "https://example.com/feed.xml"
                return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        p = Project.create(tmp_path / "scheduled-cli.frisket", name="scheduled-cli")
        sid = SourceStore(p).add_source(
            name="Scheduled Feed",
            kind="rss",
            url="https://example.com/feed.xml",
            schedule="@hourly",
        )
        p.close()
        q = open_queue(workspace=tmp_path)

        assert cli.worker([str(tmp_path), "--drain"]) == 0

        jobs = q.list_jobs(status="done")
        assert len([job for job in jobs if job.kind == SOURCE_POLL_KIND]) == 1
        p2 = Project(tmp_path / "scheduled-cli.frisket")
        assert SourceStore(p2).source_runs(sid)[0]["status"] == "ok"
        p2.close()
        q.close()

    def test_worker_cli_prefers_workspace_over_env(self, tmp_path, monkeypatch):
        """A named workspace must NOT be hijacked by FRISKET_DATABASE_URL
        (the hosted control plane's env var)."""
        from frisket import cli
        from frisket.engine.jobs import open_queue

        monkeypatch.setenv("FRISKET_DATABASE_URL", f"sqlite:///{tmp_path}/control.db")
        q = open_queue(workspace=tmp_path)
        jid = q.enqueue(ECHO_KIND, {})
        assert cli.worker([str(tmp_path), "--drain"]) == 0
        assert q.get(jid).status == "done"
        assert not (tmp_path / "control.db").exists()  # control plane untouched
        q.close()

    def test_spawned_child_runs_and_terminates_cleanly(self, tmp_path, monkeypatch):
        """The serve path's child-process mechanics: _spawn_worker starts a
        real `python -m frisket.cli worker <ws>` child; _stop_worker SIGTERMs
        it and it exits within the grace period.

        Real child process; every wait rides `wait_until` (positive awaits —
        fine anywhere per the deterministic-time contract), but they are
        split by PHASE, because the two phases cost wildly different things:

        1. **The child announces itself.** `Worker.run_forever` writes a
           forced liveness heartbeat before its first poll, so a row in
           `worker_heartbeats` is the child saying "interpreter booted,
           package imported, THIS queue opened, SIGTERM handler installed,
           entering the loop". That start-up is ~3s of imports and is not
           what this test measures, so its bound is a failure bound only —
           generous enough that CPU contention cannot reach it, and short-
           circuited if the child dies, so a broken child still fails fast.
        2. **Then queue behaviour**, on the normal bound, measured from a
           child that is provably already polling. Charging start-up to the
           job's budget is what made this flake ~1 in 5,800 under load; the
           fix is the event, not a bigger number.

        "Still polling, not a one-shot" is proven POSITIVELY by a second job
        — enqueued only after the first completed — also getting done,
        instead of racing `proc.poll() is None` against a would-be one-shot's
        exit. And because phase 1 witnessed the handler being installed,
        "cleanly" can be the exit CODE rather than mere absence."""
        from deterministic_time import controlled_time

        from frisket import cli
        from frisket.engine.jobs import open_queue

        # _spawn_worker is gated on FRISKET_NO_WORKER, which the Windows CI job
        # and plenty of dev shells export. Inherited, it makes _spawn_worker
        # return None and every wait below burn its full liveness bound before
        # failing as a timeout -- the shape this test was blamed for as a
        # "process-spawn flake". The test owns the gate it depends on, and
        # asserts the child exists before waiting on it.
        monkeypatch.delenv("FRISKET_NO_WORKER", raising=False)
        q = open_queue(workspace=tmp_path)
        first = q.enqueue(ECHO_KIND, {"spawned": True})
        proc = cli._spawn_worker(tmp_path)
        assert proc is not None
        try:

            def child_is_polling():
                if proc.poll() is not None:
                    raise AssertionError(
                        "spawned worker exited during start-up "
                        f"(exit {proc.returncode}) instead of polling"
                    )
                return q.list_worker_heartbeats()

            with controlled_time(timeout=120) as boot:
                boot.wait_until(
                    child_is_polling,
                    message="spawned worker never reached its first poll",
                )
            with controlled_time() as t:
                t.wait_until(
                    lambda: q.get(first).status == "done",
                    message="spawned worker never finished the first job",
                )
                # A one-shot child would exit after its first job; a polling
                # worker must also pick up work enqueued afterwards.
                second = q.enqueue(ECHO_KIND, {"spawned": True, "again": True})
                t.wait_until(
                    lambda: q.get(second).status == "done",
                    message="spawned worker stopped polling after its first job",
                )
        finally:
            cli._stop_worker(proc)
        # Happens-after `_stop_worker`'s own `proc.wait()`, not a race. 0 is
        # the SIGTERM handler's own exit path (stop set, loop returns, queue
        # closed); the grace-period SIGKILL fallback would leave -9, and the
        # pre-handler default disposition -15.
        assert proc.returncode == 0
        q.close()

    def test_spawn_respects_no_worker_env(self, tmp_path, monkeypatch):
        from frisket import cli

        monkeypatch.setenv("FRISKET_NO_WORKER", "1")
        assert cli._spawn_worker(tmp_path) is None

    def test_worker_argv_shape(self, tmp_path):
        from frisket import cli

        argv = cli._worker_argv(tmp_path)
        assert argv[:3] == [sys.executable, "-m", "frisket.cli"]
        assert argv[3:] == ["worker", str(tmp_path)]
        # and the module really is runnable that way (serve relies on it)
        out = subprocess.run(
            [sys.executable, "-m", "frisket.cli", "worker", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0
        assert "--drain" in out.stdout
