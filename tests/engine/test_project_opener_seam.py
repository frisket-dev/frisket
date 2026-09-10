"""The ProjectOpener contract available to downstream compositions.

This suite pins the public import path (``frisket.jobs.worker.ProjectOpener``),
the alias's exact type, the ``project_opener`` keyword on every
registration/scan/scheduler that
opens a project, and — most importantly — that with an opener injected NO code
path falls back to a direct ``Project(path)`` open or to a payload-derived
identity.

The action-job recovery probe uses the real ``worker_lease_expired:`` job-error
prefix. Free-text errors are correctly ignored; loosening that filter would
terminalize receipts for jobs that failed for unrelated reasons.
"""

from __future__ import annotations

import importlib
import inspect
import signal
import threading
from collections.abc import Callable as CallableABC
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, get_args, get_origin, get_type_hints

import pytest

from frisket.engine.jobs import SqliteJobQueue, default_registry
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import S3ProjectBlobStore


def _assert_parameter(fn: Callable[..., Any], name: str) -> None:
    parameters = inspect.signature(fn).parameters
    assert name in parameters, (
        f"{fn.__module__}.{fn.__qualname__} lacks the typed {name} handoff"
    )
    parameter = parameters[name]
    assert parameter.default is None, (
        f"{fn.__module__}.{fn.__qualname__}.{name} must stay optional"
    )
    opener_type = getattr(
        importlib.import_module("frisket.engine.jobs.worker"), "ProjectOpener", None
    )
    assert opener_type is not None
    try:
        annotation = get_type_hints(fn).get(name)
    except Exception as exc:  # noqa: BLE001 - broken public hints are semantic drift
        raise AssertionError(
            f"{fn.__module__}.{fn.__qualname__}.{name} has unresolvable typing"
        ) from exc
    assert annotation == opener_type or opener_type in get_args(annotation), (
        f"{fn.__module__}.{fn.__qualname__}.{name} is not typed as ProjectOpener"
    )


def _public_opener_modules() -> dict[str, Any]:
    """Return the released public surfaces after defensively freezing names."""

    modules = {
        name: importlib.import_module(name)
        for name in (
            "frisket.engine.jobs.worker",
            "frisket.engine.jobs.runs",
            "frisket.engine.jobs.enclosures",
            "frisket.engine.jobs.sources",
            "frisket.engine.jobs.embeddings",
            "frisket.engine.jobs.watches",
            "frisket.engine.jobs.notifications_delivery",
            "frisket.engine.jobs.notifications_digest",
            "frisket.cli",
        )
    }
    worker = modules["frisket.engine.jobs.worker"]
    assert hasattr(worker, "ProjectOpener"), (
        "the pinned public Frisket wheel lacks typed ProjectOpener"
        "(ProjectStorageKey, Path) -> Project"
    )
    opener_value = getattr(worker.ProjectOpener, "__value__", worker.ProjectOpener)
    opener_args = get_args(opener_value)
    assert get_origin(opener_value) is CallableABC and len(opener_args) == 2
    opener_parameters, opener_result = opener_args
    assert tuple(opener_parameters) == (ProjectStorageKey, Path)
    assert opener_result is Project

    opener_functions = (
        worker.register_production_handlers,
        modules["frisket.engine.jobs.runs"].register_project_run_handler,
        modules["frisket.engine.jobs.runs"].register_action_run_handler,
        modules["frisket.engine.jobs.enclosures"].register_enclosure_download_handler,
        modules["frisket.engine.jobs.sources"].register_source_poll_handler,
        modules["frisket.engine.jobs.sources"].enqueue_due_source_polls,
        modules["frisket.engine.jobs.embeddings"].register_embedding_refresh_handler,
        modules["frisket.engine.jobs.embeddings"].enqueue_due_scheduled_refreshes,
        modules["frisket.engine.jobs.watches"].register_watch_evaluate_handler,
        modules[
            "frisket.engine.jobs.notifications_delivery"
        ].register_notification_handlers,
        modules[
            "frisket.engine.jobs.notifications_delivery"
        ].deliver_notification_request,
        modules[
            "frisket.engine.jobs.notifications_delivery"
        ].enqueue_notification_delivery,
        modules[
            "frisket.engine.jobs.notifications_delivery"
        ].reconcile_notification_delivery_requests,
        modules[
            "frisket.engine.jobs.notifications_digest"
        ].register_notification_digest_handlers,
        modules[
            "frisket.engine.jobs.notifications_digest"
        ].compose_notification_digest_job,
        modules[
            "frisket.engine.jobs.notifications_digest"
        ].enqueue_due_notification_digests,
        modules["frisket.cli"]._enqueue_due_source_polls,
        modules["frisket.cli"]._enqueue_due_scheduled_embeddings,
        modules["frisket.cli"]._enqueue_due_notification_digests,
        modules["frisket.cli"]._start_source_scheduler,
        modules["frisket.cli"]._start_embedding_scheduler,
        modules["frisket.cli"]._start_notification_digest_scheduler,
    )
    for fn in opener_functions:
        _assert_parameter(fn, "project_opener")

    assert hasattr(modules["frisket.cli"], "SchedulerRoot"), (
        "the pinned public CLI lacks SchedulerRoot=(Path, storage_org_id)"
    )
    assert modules["frisket.cli"].SchedulerRoot == tuple[Path, int | None]
    return modules


class _DirectProjectBypass(AssertionError):
    """A direct Project open must remain visible through recovery catch-alls."""


def test_workspace_registration_derives_opener_from_declared_storage_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_module = importlib.import_module("frisket.server.workspace")
    sources_module = importlib.import_module("frisket.engine.jobs.sources")
    real_register = workspace_module.register_production_handlers
    registered_openers: list[Any] = []

    def capture_registration(
        *args: Any, project_opener: Any = None, **kwargs: Any
    ) -> Any:
        registered_openers.append(project_opener)
        if project_opener is None:
            return None
        return real_register(*args, project_opener=project_opener, **kwargs)

    monkeypatch.setattr(
        workspace_module, "register_production_handlers", capture_registration
    )

    storage_org_id = 23
    root = tmp_path / str(storage_org_id)
    key = ProjectStorageKey(storage_org_id=storage_org_id, project_slug="same")
    bundle = root / "same.frisket"
    created = Project.create(bundle, name="Same")
    created.close()
    store_keys: list[ProjectStorageKey] = []

    def store_factory(project_slug: str) -> S3ProjectBlobStore:
        store_key = ProjectStorageKey(
            storage_org_id=storage_org_id,
            project_slug=project_slug,
        )
        store_keys.append(store_key)
        return S3ProjectBlobStore(
            client=object(),
            bucket="workspace-oracle",
            prefix="canonical",
            project_storage_key=store_key,
        )

    opened_paths: list[Path] = []
    real_project = workspace_module.Project

    def recording_project(path: Any, *args: Any, **kwargs: Any) -> Project:
        opened_paths.append(Path(path))
        return real_project(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "Project", recording_project)
    registry = default_registry()
    queue = SqliteJobQueue(tmp_path / "workspace-queue.db", hosted=True)
    try:
        workspace_module.Workspace(
            root,
            queue=queue,
            registry=registry,
            queue_payload_extra={"storage_org_id": storage_org_id},
            project_blob_store_factory=store_factory,
        )
        assert len(registered_openers) == 1
        opener = registered_openers[0]
        assert callable(opener), (
            "Workspace must derive a ProjectOpener from its declared storage org "
            "and project_blob_store_factory"
        )

        opened = opener(key, bundle)
        try:
            assert isinstance(opened.blob_store, S3ProjectBlobStore)
            assert opened.blob_store.project_storage_key == key
            assert opened_paths == [bundle]
            assert store_keys == [key]
        finally:
            opened.close()

        before_wrong_org = (list(opened_paths), list(store_keys))
        with pytest.raises((RuntimeError, ValueError)):
            opener(
                ProjectStorageKey(storage_org_id=99, project_slug=key.project_slug),
                bundle,
            )
        assert (opened_paths, store_keys) == before_wrong_org

        direct_opens: list[Path] = []

        def forbid_direct_project(path: Any, *_: Any, **__: Any) -> Any:
            direct_opens.append(Path(path))
            raise _DirectProjectBypass(
                f"Workspace handler bypassed ProjectOpener for {path}"
            )

        monkeypatch.setattr(sources_module, "Project", forbid_direct_project)
        handler = registry.get("source.poll")
        assert handler is not None
        before_handler_keys = len(store_keys)
        before_handler_paths = len(opened_paths)
        try:
            handler(
                {
                    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
                    "project_id": "payload-attacker",
                    "storage_org_id": 99,
                    "workspace_root": str(tmp_path / "payload-root"),
                    "source_id": -1,
                },
                JobHandlerContext.without_job_row(),
            )
        except _DirectProjectBypass:
            raise
        except Exception:
            pass
        assert direct_opens == []
        handler_keys = store_keys[before_handler_keys:]
        handler_paths = opened_paths[before_handler_paths:]
        assert handler_keys and all(handler_key == key for handler_key in handler_keys)
        assert handler_paths and all(path == bundle for path in handler_paths)
    finally:
        queue.close()


def test_all_project_handlers_call_one_typed_opener_instead_of_direct_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _public_opener_modules()
    worker_module = modules["frisket.engine.jobs.worker"]
    global_root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=23, project_slug="trusted")
    bundle = global_root / "23" / "trusted.frisket"
    created = Project.create(bundle, name="Trusted")
    created.close()

    direct_modules = (
        "frisket.engine.jobs.runs",
        "frisket.engine.jobs.enclosures",
        "frisket.engine.jobs.sources",
        "frisket.engine.jobs.embeddings",
        "frisket.engine.jobs.watches",
        "frisket.engine.jobs.notifications_delivery",
        "frisket.engine.jobs.notifications_digest",
    )

    direct_opens: list[Path] = []

    def forbid_direct_project(path: Any, *_: Any, **__: Any) -> Any:
        direct_opens.append(Path(path))
        raise _DirectProjectBypass(f"handler bypassed ProjectOpener for {path}")

    for name in direct_modules:
        monkeypatch.setattr(modules[name], "Project", forbid_direct_project)

    calls: list[tuple[ProjectStorageKey, Path]] = []

    def opener(storage_key: ProjectStorageKey, path: Path) -> Project:
        calls.append((storage_key, Path(path)))
        return Project(path)

    queue = SqliteJobQueue(tmp_path / "handler-queue.db", hosted=True)
    registry = default_registry()
    try:
        worker_module.register_production_handlers(
            registry,
            workspace_root=global_root,
            queue=queue,
            require_storage_identity=True,
            project_opener=opener,
        )
        common = {
            CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
            "project_id": "payload-attacker",
            "storage_org_id": 999,
            "org_id": 555,
            "workspace_root": str(tmp_path / "payload-root"),
            "job_id": 1,
            "run_id": 999,
            "spec": {},
            "source_id": 999,
            "sheet_id": 999,
            "row_id": 999,
            "index_id": "missing-index",
            "watch_id": 999,
            "request_id": 999,
            "route_id": 999,
            "cadence": "daily",
            "window_key": "2026-07-17",
            "window_start_at": "2026-07-17T00:00:00+00:00",
            "window_end_at": "2026-07-18T00:00:00+00:00",
        }
        kinds = (
            "project.run",
            # Embedding refresh dispatches through this generic kind's
            # action-executor registry (registered under the action kind
            # "embedding.index_refresh") — there is no separate top-level
            # job kind for it anymore.
            "action.run",
            "watch.evaluate",
            "source.poll",
            "enclosure.download",
            "notification.deliver",
            "notification.digest",
        )
        for kind in kinds:
            calls.clear()
            direct_opens.clear()
            handler = registry.get(kind)
            assert handler is not None, f"production registration omitted {kind}"
            try:
                handler(dict(common), JobHandlerContext.without_job_row())
            except _DirectProjectBypass:
                raise
            except Exception:
                pass  # business data is deliberately minimal; opening is the proof
            assert not direct_opens, f"{kind} bypassed the injected ProjectOpener"
            assert calls, f"{kind} never called the injected ProjectOpener"
            assert all(call == (key, bundle) for call in calls), (
                f"{kind} opened an untrusted key/path: {calls}"
            )

            calls.clear()
            direct_opens.clear()
            missing_claim = dict(common)
            missing_claim.pop(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
            try:
                handler(missing_claim, JobHandlerContext.without_job_row())
            except _DirectProjectBypass:
                raise
            except Exception:
                pass
            assert calls == [], f"{kind} opened without a claimed storage identity"
            assert direct_opens == [], f"{kind} fell back to a payload-derived path"
    finally:
        queue.close()


def test_recovery_schedulers_nested_digest_and_cli_reuse_returned_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _public_opener_modules()
    cli = modules["frisket.cli"]
    runs = modules["frisket.engine.jobs.runs"]
    notifications = modules["frisket.engine.jobs.notifications_delivery"]
    digests = modules["frisket.engine.jobs.notifications_digest"]

    # The root basename is intentionally 999 while its declared identity is 23.
    # Re-parsing root.name would cross the test's tenant boundary.
    root = tmp_path / "999"
    key = ProjectStorageKey(storage_org_id=23, project_slug="same")
    bundle = root / "same.frisket"
    created = Project.create(bundle, name="Same")
    created.close()
    calls: list[tuple[ProjectStorageKey, Path]] = []
    direct_opens: list[Path] = []

    def opener(storage_key: ProjectStorageKey, path: Path) -> Project:
        calls.append((storage_key, Path(path)))
        return Project(path)

    def forbid_direct_project(path: Any, *_: Any, **__: Any) -> Any:
        direct_opens.append(Path(path))
        raise _DirectProjectBypass(f"runtime path bypassed ProjectOpener for {path}")

    for name in (
        "frisket.engine.jobs.sources",
        "frisket.engine.jobs.embeddings",
        "frisket.engine.jobs.notifications_digest",
    ):
        monkeypatch.setattr(modules[name], "Project", forbid_direct_project)

    queue = SqliteJobQueue(tmp_path / "scheduler.db", hosted=True)
    try:
        for scheduler in (
            cli._enqueue_due_source_polls,
            cli._enqueue_due_scheduled_embeddings,
            cli._enqueue_due_notification_digests,
        ):
            calls.clear()
            direct_opens.clear()
            scheduler(queue, [(root, 23)], project_opener=opener)
            assert calls == [(key, bundle)], (
                f"{scheduler.__name__} ignored tuple identity/opener: {calls}"
            )
            assert direct_opens == []
            for invalid_identity in (None, 0, -7):
                calls.clear()
                direct_opens.clear()
                try:
                    scheduler(
                        queue,
                        [(root, invalid_identity)],
                        project_opener=opener,
                    )
                except (RuntimeError, ValueError):
                    pass
                assert calls == [], (
                    f"{scheduler.__name__} opened invalid identity "
                    f"{invalid_identity}: {calls}"
                )
                assert direct_opens == []

        # The actual global hosted notification recovery root is
        # /data/projects, not a pre-selected per-org directory.
        global_root = tmp_path / "projects"
        recovery_key = ProjectStorageKey(storage_org_id=31, project_slug="recover")
        recovery_bundle = global_root / "31" / "recover.frisket"
        recovery = Project.create(recovery_bundle, name="Recover")
        recovery.close()
        for invalid_org in ("0", "-9", "not-an-org"):
            invalid = Project.create(
                global_root / invalid_org / "invalid.frisket",
                name="Invalid",
            )
            invalid.close()
        monkeypatch.setattr(notifications, "Project", forbid_direct_project)
        calls.clear()
        direct_opens.clear()
        notifications.reconcile_notification_delivery_requests(
            queue,
            workspace_root=global_root,
            project_opener=opener,
        )
        assert calls == [(recovery_key, recovery_bundle)]
        assert direct_opens == []

        # Registration must pass the opener into the real recovery callback.
        registry = HandlerRegistry()
        notifications.register_notification_handlers(
            registry,
            workspace_root=global_root,
            queue=queue,
            project_opener=opener,
        )
        calls.clear()
        direct_opens.clear()
        registry.run_recovery_hooks(queue)
        assert calls == [(recovery_key, recovery_bundle)]
        assert direct_opens == []

        # Action recovery uses Job.project_storage_key even when every mutable
        # payload identity points elsewhere.
        from frisket.engine.executor import action_jobs
        from frisket.engine.executor.action_jobs import ActionJobEnvelope
        from frisket.engine.jobs.queue import ACTION_RUN_KIND
        import frisket.engine.store as store_module

        action_key = ProjectStorageKey(storage_org_id=41, project_slug="action")
        action_bundle = global_root / "41" / "action.frisket"
        action_project = Project.create(action_bundle, name="Action")
        action_project.close()
        payload_bundle = tmp_path / "payload-root" / "payload-attacker.frisket"
        payload_project = Project.create(payload_bundle, name="Payload attacker")
        payload_project.close()
        envelope = ActionJobEnvelope(
            action_kind="project.update",
            action_id="action-id",
            receipt_id="missing-receipt",
            params_hash="sha256:params",
            idempotency_key="r10-action",
            project_id="payload-attacker",
            action={},
        )
        failed_job = SimpleNamespace(
            id=77,
            kind=ACTION_RUN_KIND,
            error="worker_lease_expired: worker lease expired during opener probe",
            project_storage_key=action_key,
            payload={
                "project_id": "payload-attacker",
                "storage_org_id": 999,
                "org_id": 555,
                "workspace_root": str(tmp_path / "payload-root"),
                "action_job": envelope.to_json(),
            },
        )

        class FailedQueue:
            def list_jobs(self, *, status: str, limit: int) -> list[Any]:
                assert status == "failed" and limit == 1000
                return [failed_job]

        monkeypatch.setattr(store_module, "Project", forbid_direct_project)
        _assert_parameter(
            action_jobs.reconcile_exhausted_action_run_jobs, "project_opener"
        )
        calls.clear()
        direct_opens.clear()
        action_jobs.reconcile_exhausted_action_run_jobs(
            FailedQueue(),
            workspace_root=global_root,
            project_opener=opener,
        )
        assert calls == [(action_key, action_bundle)]
        assert direct_opens == []

        failed_job.project_storage_key = None
        calls.clear()
        direct_opens.clear()
        action_jobs.reconcile_exhausted_action_run_jobs(
            FailedQueue(),
            workspace_root=global_root,
            project_opener=opener,
        )
        assert calls == []
        assert direct_opens == []
        failed_job.project_storage_key = action_key

        action_registry = HandlerRegistry()
        runs.register_action_run_handler(
            action_registry,
            workspace_root=global_root,
            require_storage_identity=True,
            project_opener=opener,
        )
        calls.clear()
        direct_opens.clear()
        action_registry.run_recovery_hooks(FailedQueue())
        assert calls == [(action_key, action_bundle)]
        assert direct_opens == []

        # Digest composition opens once, then hands the *same callable* to the
        # nested delivery enqueue instead of doing a second direct Project open.
        captured_nested: list[Any] = []
        monkeypatch.setattr(
            digests,
            "compose_notification_digest",
            lambda project, route_id, *, window: {
                "id": 9,
                "status": "queued",
                "item_count": 1,
                "delivery_request_id": 17,
            },
        )

        def fake_enqueue_delivery(
            *args: Any, project_opener: Any = None, **kwargs: Any
        ) -> int:
            captured_nested.append(project_opener)
            return 19

        monkeypatch.setattr(
            digests, "enqueue_notification_delivery", fake_enqueue_delivery
        )
        calls.clear()
        window_type = importlib.import_module(
            "frisket.server.notifications.digests"
        ).NotificationDigestWindow
        result = digests.compose_notification_digest_job(
            workspace_root=root,
            project_id=key.project_slug,
            storage_org_id=key.storage_org_id,
            route_id=5,
            window=window_type(
                cadence="daily",
                window_key="2026-07-17",
                start_at=datetime.now(UTC) - timedelta(days=1),
                end_at=datetime.now(UTC),
            ),
            queue=queue,
            project_opener=opener,
        )
        assert result["delivery_job_id"] == 19
        assert calls == [(key, bundle)]
        assert captured_nested == [opener]
        assert direct_opens == []

        # Public hosted registration returns the external edition's object
        # unchanged; drain and long-running CLI modes preserve it.
        def sentinel(storage_key: ProjectStorageKey, path: Path) -> None:
            return None

        worker_module = modules["frisket.engine.jobs.worker"]
        monkeypatch.setattr(
            worker_module,
            "load_worker_edition",
            lambda edition: lambda *args, **kwargs: sentinel,
        )
        forwarded = worker_module.register_hosted_handlers(
            HandlerRegistry(), workspace_root=global_root, queue=queue
        )
        assert forwarded is sentinel

        import frisket.engine.jobs as jobs_package

        scheduler_openers: list[Any] = []
        drain_modes: list[bool] = []

        class CliQueue:
            closed = False

            def close(self) -> None:
                self.closed = True

        cli_queue = CliQueue()

        class CliWorker:
            worker_id = "r10-cli-worker"

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def run_forever(self, stop: Any, *, drain: bool = False) -> None:
                drain_modes.append(drain)

        monkeypatch.setattr(jobs_package, "open_queue", lambda **kwargs: cli_queue)
        monkeypatch.setattr(jobs_package, "default_registry", HandlerRegistry)
        monkeypatch.setattr(jobs_package, "Worker", CliWorker)
        monkeypatch.setattr(
            jobs_package,
            "register_hosted_handlers",
            lambda *args, **kwargs: sentinel,
        )
        monkeypatch.setattr(
            cli, "_source_scheduler_roots", lambda workspace: [(root, 23)]
        )

        for starter_name, enqueue_name in (
            ("_start_source_scheduler", "_enqueue_due_source_polls"),
            ("_start_embedding_scheduler", "_enqueue_due_scheduled_embeddings"),
            (
                "_start_notification_digest_scheduler",
                "_enqueue_due_notification_digests",
            ),
        ):
            stop = threading.Event()
            loop_openers: list[Any] = []

            def capture_loop(
                _queue: Any,
                _roots: Any,
                *,
                project_opener: Any = None,
                _stop: threading.Event = stop,
            ) -> int:
                loop_openers.append(project_opener)
                _stop.set()
                return 0

            monkeypatch.setattr(cli, enqueue_name, capture_loop)
            scheduler = getattr(cli, starter_name)(
                cli_queue,
                [(root, 23)],
                0.01,
                stop,
                project_opener=sentinel,
            )
            assert scheduler is not None
            scheduler.join(timeout=1)
            assert not scheduler.is_alive()
            assert loop_openers == [sentinel]

        def capture_scheduler(
            _queue: Any,
            _roots: Any,
            *,
            project_opener: Any = None,
        ) -> int:
            scheduler_openers.append(project_opener)
            return 0

        monkeypatch.setattr(cli, "_enqueue_due_source_polls", capture_scheduler)
        monkeypatch.setattr(cli, "_enqueue_due_scheduled_embeddings", capture_scheduler)
        monkeypatch.setattr(cli, "_enqueue_due_notification_digests", capture_scheduler)
        monkeypatch.setattr(signal, "signal", lambda *args, **kwargs: None)
        monkeypatch.setenv("FRISKET_RUN_QUEUE_DATABASE_URL", "postgresql://unused/r10")
        rc = cli._run_worker(
            [
                "--drain",
                "--schedule-sources-interval",
                "1",
                "--schedule-embeddings-interval",
                "1",
                "--schedule-notification-digests-interval",
                "1",
            ],
            hosted=True,
        )
        assert rc == 0
        assert scheduler_openers == [sentinel, sentinel, sentinel]
        assert cli_queue.closed is True
        assert drain_modes == [True]

        starter_openers: list[Any] = []

        class JoinedScheduler:
            joined = False

            def join(self, *, timeout: float) -> None:
                assert timeout == 5
                self.joined = True

        schedulers: list[JoinedScheduler] = []

        def capture_starter(
            _queue: Any,
            _roots: Any,
            _interval: float,
            _stop: Any,
            *,
            project_opener: Any = None,
        ) -> JoinedScheduler:
            starter_openers.append(project_opener)
            thread = JoinedScheduler()
            schedulers.append(thread)
            return thread

        monkeypatch.setattr(cli, "_start_source_scheduler", capture_starter)
        monkeypatch.setattr(cli, "_start_embedding_scheduler", capture_starter)
        monkeypatch.setattr(
            cli, "_start_notification_digest_scheduler", capture_starter
        )
        cli_queue.closed = False
        rc = cli._run_worker(
            [
                "--schedule-sources-interval",
                "1",
                "--schedule-embeddings-interval",
                "1",
                "--schedule-notification-digests-interval",
                "1",
            ],
            hosted=True,
        )
        assert rc == 0
        assert starter_openers == [sentinel, sentinel, sentinel]
        assert drain_modes == [True, False]
        assert all(scheduler.joined for scheduler in schedulers)
        assert cli_queue.closed is True
    finally:
        queue.close()
