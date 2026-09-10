"""frisket CLI: `frisket <workspace-dir>` serves the local tier (and
auto-spawns the job worker as a child process — one command, but sync CPU
work no longer blocks the web event loop); `frisket worker` runs the worker
loop alone (compose/hosted run it as an explicit service, same image);
`frisket doctor` is the deployment self-probe."""

from __future__ import annotations

import argparse
import difflib
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

from frisket.project_opener import ProjectOpener

from frisket.cli._shared import _standalone_database_url


def action_cmd(argv: list[str]) -> int:
    from frisket.cli.action import action as command

    return command(argv)


def doctor_cmd(argv: list[str] | None = None) -> int:
    from frisket.cli.doctor import doctor as command

    return command(argv)


def owner_admin(argv: list[str]) -> int:
    from frisket.cli.owner import owner_admin as command

    return command(argv)


def op_tools(argv: list[str]) -> int:
    from frisket.cli.op import op as command

    return command(argv)


def plugin_cmd(argv: list[str]) -> int:
    from frisket.cli.plugin import plugin as command

    return command(argv)


def queue_admin(argv: list[str]) -> int:
    from frisket.cli.queue import queue_admin as command

    return command(argv)


def reconcile_cmd(argv: list[str]) -> int:
    from frisket.cli.reconcile import reconcile as command

    return command(argv)


def reset_drain_cmd(argv: list[str]) -> int:
    from frisket.cli.reset_drain import reset_drain as command

    return command(argv)


# A scheduler root paired with its DECLARED storage-org identity (None for
# every non-hosted/flat root). Identity travels WITH the root from the
# moment it is discovered -- never re-derived from `root.name` at the
# call site -- so a flat single-org data dir that happens to have a numeric
# basename (e.g. FRISKET_DATA_DIR=/mnt/7) can never be mistaken for hosted
# per-org storage.
SchedulerRoot = tuple[Path, int | None]


def _hosted_project_roots() -> list[SchedulerRoot]:
    """Per-org roots under the multi-tenant hosted layout,
    ``FRISKET_DATA_DIR/projects/<org_id>/*.frisket``.

    Only directories whose name is a valid positive storage-org id are
    accepted. This is not just cosmetic: ``_source_scheduler_roots`` treats
    a non-empty result here as proof the hosted per-org layout is in use and
    lets it suppress the flat single-org fallback (the ``or`` below); a
    stray non-numeric directory under ``projects/`` (a cache dir, a
    leftover, anything that is not really an org root) must never count as
    that proof, or a flat team-app data dir with an unrelated ``projects/``
    subdirectory would wrongly select hosted per-org routing instead of its
    real flat layout.
    """
    data_dir = os.environ.get("FRISKET_DATA_DIR")
    if not data_dir:
        return []
    projects = Path(data_dir) / "projects"
    if not projects.exists():
        return []
    roots: list[SchedulerRoot] = []
    for p in sorted(projects.iterdir()):
        if not p.is_dir():
            continue
        org_id = _storage_org_id_for_scheduler_root(p)
        if org_id is not None:
            roots.append((p, org_id))
    return roots


def _flat_data_dir_scheduler_root() -> list[SchedulerRoot]:
    """Fallback scheduler root for the open team-server composition.

    ``_hosted_project_roots`` assumes the multi-tenant layout,
    ``FRISKET_DATA_DIR/projects/<org_id>/*.frisket`` (one subdirectory per
    org). The open team server (``frisket.team.app.create_team_app``) is
    single-org and passes ``config.data_dir`` straight through as its
    Workspace root, so bundles land directly at
    ``FRISKET_DATA_DIR/<slug>.frisket`` -- no ``projects/`` nesting at all.
    Scanning ``FRISKET_DATA_DIR`` itself as ONE flat root
    when no per-org ``projects/`` subdirectories exist lets scheduled source
    polls, embedding refreshes, and notification digests start for that
    layout, without disturbing the per-org discovery
    ``test_worker_cli_drains_real_source_poll`` /
    ``test_scheduler_keeps_workspace_roots_distinct_for_hosted_slugs`` (etc.)
    already cover.

    Storage org id is ALWAYS ``None`` here, regardless of the data dir's
    basename (a flat ``FRISKET_DATA_DIR=/mnt/7`` is not org 7 -- it is a
    non-hosted single-org layout with no claimed storage identity at all,
    same as the local single-node tier). The important boundary is:
    inferring an org id from a flat root's name previously misrouted the
    scheduled job through ``claimed_project_location``'s hosted-claim path
    (``<data_dir>/projects/<bogus_org_id>/<slug>.frisket``, which does not
    exist) instead of the real ``<data_dir>/<slug>.frisket``.
    """
    data_dir = os.environ.get("FRISKET_DATA_DIR")
    if not data_dir:
        return []
    root = Path(data_dir)
    return [(root, None)] if root.is_dir() else []


def _source_scheduler_roots(workspace: Path | None) -> list[SchedulerRoot]:
    raw = os.environ.get("FRISKET_SOURCE_WORKSPACE_ROOTS")
    if raw:
        return [
            (path, _storage_org_id_for_scheduler_root(path))
            for path in (Path(p) for p in raw.split(os.pathsep) if p)
        ]
    if workspace is None:
        return _hosted_project_roots() or _flat_data_dir_scheduler_root()
    data_dir = os.environ.get("FRISKET_DATA_DIR")
    if data_dir:
        queue_root = Path(data_dir) / "queue"
        try:
            is_hosted_queue = workspace.resolve() == queue_root.resolve()
        except OSError:
            is_hosted_queue = workspace == queue_root
        if is_hosted_queue:
            roots = _hosted_project_roots()
            if roots:
                return roots
    return [(workspace, _storage_org_id_for_scheduler_root(workspace))]


def _handler_workspace_root(workspace: Path | None) -> Path:
    if workspace is not None:
        return workspace
    projects_root = os.environ.get("FRISKET_PROJECTS_ROOT")
    if projects_root:
        return Path(projects_root)
    data_dir = os.environ.get("FRISKET_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "projects"
    return Path.cwd() / "frisket-projects"


def _storage_org_id_for_scheduler_root(root: Path) -> int | None:
    try:
        value = int(root.name)
    except ValueError:
        return None
    return value if value > 0 else None


def _enqueue_due_source_polls(
    queue,
    roots: list[SchedulerRoot],
    *,
    project_opener: ProjectOpener | None = None,
) -> int:
    from frisket.engine.jobs import enqueue_due_source_polls

    logger = logging.getLogger("frisket.worker")
    total = 0
    for root, storage_org_id in roots:
        try:
            total += len(
                enqueue_due_source_polls(
                    workspace_root=root,
                    queue=queue,
                    storage_org_id=storage_org_id,
                    project_opener=project_opener,
                )
            )
        except Exception as exc:  # noqa: BLE001 - scheduler must not kill worker
            logger.warning(
                "source_scheduler_skipped",
                extra={
                    "event": "source_scheduler_skipped",
                    "workspace_root": str(root),
                    "error": str(exc),
                },
            )
    return total


def _start_source_scheduler(
    queue,
    roots: list[SchedulerRoot],
    interval: float,
    stop,
    *,
    project_opener: ProjectOpener | None = None,
):
    import threading

    if interval <= 0 or not roots:
        return None

    def loop() -> None:
        while not stop.is_set():
            _enqueue_due_source_polls(queue, roots, project_opener=project_opener)
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="source-scheduler", daemon=True)
    thread.start()
    return thread


def _enqueue_due_scheduled_embeddings(
    queue,
    roots: list[SchedulerRoot],
    *,
    project_opener: ProjectOpener | None = None,
) -> int:
    from frisket.engine.jobs import enqueue_due_scheduled_refreshes

    logger = logging.getLogger("frisket.worker")
    total = 0
    for root, storage_org_id in roots:
        try:
            total += len(
                enqueue_due_scheduled_refreshes(
                    workspace_root=root,
                    queue=queue,
                    storage_org_id=storage_org_id,
                    project_opener=project_opener,
                )
            )
        except Exception as exc:  # noqa: BLE001 - scheduler must not kill worker
            logger.warning(
                "embedding_scheduler_skipped",
                extra={
                    "event": "embedding_scheduler_skipped",
                    "workspace_root": str(root),
                    "error": str(exc),
                },
            )
    return total


def _start_embedding_scheduler(
    queue,
    roots: list[SchedulerRoot],
    interval: float,
    stop,
    *,
    project_opener: ProjectOpener | None = None,
):
    import threading

    if interval <= 0 or not roots:
        return None

    def loop() -> None:
        while not stop.is_set():
            _enqueue_due_scheduled_embeddings(
                queue, roots, project_opener=project_opener
            )
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="embedding-scheduler", daemon=True)
    thread.start()
    return thread


def _enqueue_due_notification_digests(
    queue,
    roots: list[SchedulerRoot],
    *,
    project_opener: ProjectOpener | None = None,
) -> int:
    from frisket.engine.jobs import enqueue_due_notification_digests

    logger = logging.getLogger("frisket.worker")
    total = 0
    for root, storage_org_id in roots:
        try:
            total += enqueue_due_notification_digests(
                queue,
                workspace_root=root,
                storage_org_id=storage_org_id,
                project_opener=project_opener,
            )
        except Exception as exc:  # noqa: BLE001 - scheduler must not kill worker
            logger.warning(
                "notification_digest_scheduler_skipped",
                extra={
                    "event": "notification_digest_scheduler_skipped",
                    "workspace_root": str(root),
                    "error": str(exc),
                },
            )
    return total


def _start_notification_digest_scheduler(
    queue,
    roots: list[SchedulerRoot],
    interval: float,
    stop,
    *,
    project_opener: ProjectOpener | None = None,
):
    import threading

    if interval <= 0 or not roots:
        return None

    def loop() -> None:
        while not stop.is_set():
            _enqueue_due_notification_digests(
                queue, roots, project_opener=project_opener
            )
            stop.wait(interval)

    thread = threading.Thread(
        target=loop,
        name="notification-digest-scheduler",
        daemon=True,
    )
    thread.start()
    return thread


def _write_model_pull_finished_audit(
    *, queue, control_database_url: str | None, payload: dict
) -> None:
    """Best-effort terminal-outcome audit for one ``model.pull`` attempt.
    The request-side audit is incomplete without the terminal outcome.
    Reads the pull row's own final ``status``/``model_ref``/
    ``correlation_id`` back from the run-queue database — the durable,
    already-sanitized truth every ``mark_done``/``mark_failed``/
    ``mark_cancelled`` call wrote — rather than trusting the handler's
    return value or any exception text, neither of which this function ever
    touches. Never raises: an audit-write failure must not turn an
    otherwise-successful (or already-failed) job outcome into a worker
    crash."""
    if not control_database_url:
        return
    pull_id = payload.get("pull_id")
    if pull_id is None:
        return
    from frisket.engine.jobs import model_pull_store

    try:
        row = model_pull_store.get(queue.engine, int(pull_id))
    except Exception:  # noqa: BLE001 — audit is best-effort
        logging.getLogger("frisket.worker").exception(
            "model_pull_finished_audit_row_lookup_failed",
            extra={"event": "model_pull_finished_audit_row_lookup_failed"},
        )
        return
    if row is None:
        return
    if row.status not in model_pull_store.TERMINAL_STATUSES:
        # This wrapper used to fire after EVERY attempt, including
        # non-final retryable ones -- the row stays ACTIVE
        # (`_fail`/`record_attempt_error`) whenever attempts remain, so a
        # job that retries N times would write N misleading
        # `model_pull_finished` rows all claiming a non-terminal
        # 'pending'/'running' outcome, none of which is the pull's actual
        # final result. Only a row the store itself has already finalized
        # (done/failed/cancelled) is a real terminal outcome worth auditing;
        # a handler-less registration gap or lease-exhaustion path that
        # never runs this wrapper at all is a separate, accepted residual,
        # not something this per-attempt check can paper over.
        return
    detail = f"{row.id}:{row.model_ref}:{row.status}"
    if row.correlation_id:
        detail = f"{detail}:{row.correlation_id}"
    org_id_raw = payload.get("org_id")
    try:
        org_id = int(org_id_raw) if org_id_raw is not None else None
    except (TypeError, ValueError):
        org_id = None
    try:
        import sqlalchemy as sa

        from frisket.team.schema import audit_log

        engine = sa.create_engine(control_database_url, future=True)
        with engine.begin() as cx:
            cx.execute(
                audit_log.insert().values(
                    user_id=None,
                    org_id=org_id,
                    action="model_pull_finished",
                    detail=detail,
                )
            )
    except Exception:  # noqa: BLE001 — audit is best-effort
        logging.getLogger("frisket.worker").exception(
            "model_pull_finished_audit_write_failed",
            extra={"event": "model_pull_finished_audit_write_failed"},
        )


def _wrap_model_pull_handler_with_finished_audit(
    handler, *, queue, control_database_url
):
    """Wrap a registered ``model.pull`` handler so a ``model_pull_finished``
    audit row lands after every attempt, whether the handler returns or
    raises — the exception path matters just as much as success: a job that
    ends up ``failed``/``cancelled`` still needs its terminal outcome on the
    audit trail."""

    def wrapped(payload: dict, handler_context) -> dict:
        try:
            result = handler(payload, handler_context)
        except BaseException:
            _write_model_pull_finished_audit(
                queue=queue, control_database_url=control_database_url, payload=payload
            )
            raise
        _write_model_pull_finished_audit(
            queue=queue, control_database_url=control_database_url, payload=payload
        )
        return result

    return wrapped


def _register_team_model_pull_handler(
    registry,
    *,
    workspace_root,
    queue,
    control_database_url: str | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    """Register the artifact/local-model pull worker for a team queue.

    Artifact pulls are independent of local endpoint provisioning. Local
    endpoint requests are still gated by their selected endpoint's
    ``pull_enabled`` fact before enqueue.

    The registered handler is wrapped with a terminal-outcome audit
    (``model_pull_finished``). The route already audits the request
    (``model_pull_requested``); nothing
    previously recorded what actually happened once a worker ran the job.
    """
    from frisket.engine.jobs.model_pull import MODEL_PULL_KIND as _MODEL_PULL_KIND
    from frisket.engine.jobs.model_pull import register_model_pull_handler
    from frisket.engine.jobs.worker import HandlerRegistration

    registered = register_model_pull_handler(
        registry,
        workspace_root=workspace_root,
        queue=queue,
    )
    if not isinstance(registered, HandlerRegistration):
        # Test and edition seams may provide a compatible registrar which
        # predates registration tokens. Capture the exact handler it installed
        # before composing the audit wrapper.
        registered = registry.registration(_MODEL_PULL_KIND)

    def with_finished_audit(handler):
        return _wrap_model_pull_handler_with_finished_audit(
            handler,
            queue=queue,
            control_database_url=control_database_url,
        )

    registry.decorate(
        _MODEL_PULL_KIND,
        expected=registered,
        decorator=with_finished_audit,
        origin="frisket.team.model_pull_terminal_audit",
    )
    return True


def _run_worker(argv: list[str], *, hosted: bool) -> int:
    """Run the shared worker loop with an explicit registration boundary.

    Backend selection: an explicit --database-url (or, when no workspace is
    named, FRISKET_RUN_QUEUE_DATABASE_URL) means a shared database-addressed
    queue (SQLite for Standalone or Postgres for Multi-service); a workspace
    path means its local SQLite queue. A named workspace always wins over the
    env var so the Personal one-command path cannot accidentally point its
    child worker at server infrastructure. FRISKET_DATABASE_URL remains the
    control-plane locator for handlers that need it."""
    import argparse
    import signal
    import threading

    command_name = "hosted-worker" if hosted else "worker"
    if hosted and any(
        value == "--database-url" or value.startswith("--database-url=")
        for value in argv
    ):
        print(
            "frisket hosted-worker: run-queue locator is environment-only",
            file=sys.stderr,
        )
        return 2
    ap = argparse.ArgumentParser(
        prog=f"frisket {command_name}", description="run the frisket job worker"
    )
    ap.add_argument("workspace", nargs="?", help="workspace dir (SQLite queue)")
    ap.add_argument(
        "--database-url",
        default=None,
        help="shared run-queue database URL",
    )
    ap.add_argument("--drain", action="store_true", help="exit when the queue is empty")
    ap.add_argument("--poll-interval", type=float, default=0.5)
    ap.add_argument("--lease-seconds", type=float, default=60.0)
    ap.add_argument(
        "--schedule-sources-interval",
        type=float,
        default=float(os.environ.get("FRISKET_SOURCE_SCHEDULER_INTERVAL", "60")),
        help="seconds between due-source scans; 0 disables RSS scheduling",
    )
    ap.add_argument(
        "--schedule-embeddings-interval",
        type=float,
        default=float(os.environ.get("FRISKET_EMBEDDING_SCHEDULER_INTERVAL", "0")),
        help=(
            "seconds between due-scheduled-embedding-index scans; "
            "0 (default) disables scheduled embedding refresh"
        ),
    )
    ap.add_argument(
        "--schedule-notification-digests-interval",
        type=float,
        default=float(
            os.environ.get("FRISKET_NOTIFICATION_DIGEST_SCHEDULER_INTERVAL", "60")
        ),
        help=(
            "seconds between due notification-digest scans; "
            "0 disables notification digest scheduling"
        ),
    )
    args = ap.parse_args(argv)

    from frisket.operability.structured_logging import configure_logging

    configure_logging()

    from frisket.engine.jobs import (
        Worker,
        default_registry,
        open_queue,
        register_hosted_handlers,
        register_production_handlers,
    )

    database_url = args.database_url or (
        None if args.workspace else os.environ.get("FRISKET_RUN_QUEUE_DATABASE_URL")
    )
    if database_url:
        # Declare hosted posture only for the hosted worker pointed at
        # the real Postgres run queue. A named workspace always wins
        # over the env var above, so the local one-command path can
        # never acquire hosted posture.
        hosted_run_queue = hosted and database_url.startswith("postgresql")
        queue = open_queue(
            database_url=database_url,
            schema_mode="strict" if hosted_run_queue else None,
            hosted=hosted_run_queue,
        )
        where = "run queue (database)"
        workspace = None
    else:
        workspace = (
            Path(args.workspace) if args.workspace else Path.cwd() / "frisket-projects"
        )
        queue = open_queue(workspace=workspace)
        where = str(workspace)

    registry = default_registry()
    handler_root = _handler_workspace_root(workspace)
    register_handlers = (
        register_hosted_handlers if hosted else register_production_handlers
    )
    # The composition's own project-open seam (None for the open worker).
    # Reused for the scheduler scans below so that EVERY project this process
    # opens — claimed job handlers and background scans alike — goes through
    # one callable rather than the scans quietly falling back to direct opens.
    project_opener = register_handlers(
        registry,
        workspace_root=handler_root,
        queue=queue,
        control_database_url=os.environ.get("FRISKET_DATABASE_URL"),
    )
    if not hosted:
        if database_url:
            # This branch is the TEAM worker (a run-queue-backed deployment,
            # never an external managed composition -- that is `hosted=True`
            # and never reaches this call at all). Team org surfaces have no per-
            # workspace opt-in file. The handler is unconditional because
            # non-local artifacts do not depend on a local endpoint; local
            # downloads are gated per endpoint at enqueue and claim.
            _register_team_model_pull_handler(
                registry,
                workspace_root=handler_root,
                queue=queue,
                control_database_url=os.environ.get("FRISKET_DATABASE_URL"),
            )
        else:
            # The plain local worker command path, unconditional (the
            # workspace opt-in file/env flag is enforced at the route,
            # not at registration, for this tier).
            from frisket.engine.jobs.model_pull import register_model_pull_handler

            register_model_pull_handler(
                registry, workspace_root=handler_root, queue=queue
            )
    source_roots = _source_scheduler_roots(workspace)

    w = Worker(
        queue,
        registry,
        poll_interval=args.poll_interval,
        lease_seconds=args.lease_seconds,
        queue_label=where,
    )
    stop = threading.Event()
    source_scheduler = None
    if args.schedule_sources_interval > 0 and source_roots:
        if args.drain:
            _enqueue_due_source_polls(
                queue, source_roots, project_opener=project_opener
            )
        else:
            source_scheduler = _start_source_scheduler(
                queue,
                source_roots,
                args.schedule_sources_interval,
                stop,
                project_opener=project_opener,
            )
    embedding_scheduler = None
    if args.schedule_embeddings_interval > 0 and source_roots:
        if args.drain:
            _enqueue_due_scheduled_embeddings(
                queue, source_roots, project_opener=project_opener
            )
        else:
            embedding_scheduler = _start_embedding_scheduler(
                queue,
                source_roots,
                args.schedule_embeddings_interval,
                stop,
                project_opener=project_opener,
            )
    notification_digest_scheduler = None
    if args.schedule_notification_digests_interval > 0 and source_roots:
        if args.drain:
            _enqueue_due_notification_digests(
                queue, source_roots, project_opener=project_opener
            )
        else:
            notification_digest_scheduler = _start_notification_digest_scheduler(
                queue,
                source_roots,
                args.schedule_notification_digests_interval,
                stop,
                project_opener=project_opener,
            )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())  # finish current job, then exit
    logging.getLogger("frisket.worker").info(
        "worker_started",
        extra={
            "event": "worker_started",
            "worker_id": w.worker_id,
            "queue": where,
        },
    )
    try:
        w.run_forever(stop, drain=args.drain)
    finally:
        stop.set()
        if source_scheduler is not None:
            source_scheduler.join(timeout=5)
        if embedding_scheduler is not None:
            embedding_scheduler.join(timeout=5)
        if notification_digest_scheduler is not None:
            notification_digest_scheduler.join(timeout=5)
        queue.close()
    return 0


def worker(argv: list[str]) -> int:
    """Run the trusted local/source-checkout worker."""

    return _run_worker(argv, hosted=False)


def hosted_worker(argv: list[str]) -> int:
    """Run the public-hosted worker with arbitrary code hard-disabled."""

    return _run_worker(argv, hosted=True)


def _worker_argv(workspace: Path) -> list[str]:
    return [sys.executable, "-m", "frisket.cli", "worker", str(workspace)]


def _database_worker_argv(database_url: str) -> list[str]:
    """Worker argv for a server composition with an explicit shared queue."""
    return [
        sys.executable,
        "-m",
        "frisket.cli",
        "worker",
        "--database-url",
        database_url,
    ]


def _spawn_worker(workspace: Path) -> "subprocess.Popen | None":
    """`frisket <ws>` auto-spawns the worker as a child process so sync CPU
    work (e.g. transcription) stops blocking the web event loop while the
    local tier stays one command. FRISKET_NO_WORKER=1 opts out (tests,
    running a separate worker yourself)."""
    if os.environ.get("FRISKET_NO_WORKER"):
        return None
    return subprocess.Popen(_worker_argv(workspace))


def _stop_worker(proc: "subprocess.Popen | None") -> None:
    """Shutdown: `terminate()` first, then forcibly terminated after a grace
    period — a killed job's lease expires and the next worker recovers it.
    On POSIX, `terminate()` sends SIGTERM, so the worker finishes its current
    job and exits between jobs, and the grace-period fallback is SIGKILL. On
    Windows, `terminate()`/`kill()` both call `TerminateProcess`, so shutdown
    is always abrupt there — there is no graceful Windows worker shutdown."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _subcommands() -> "dict[str, tuple]":
    """Subcommand registry: name -> (handler, one-line help). Single source of
    truth for BOTH dispatch and the usage text, so a new command can never leave
    `--help` stale. Each handler takes the post-subcommand argv and returns an
    exit code."""

    def _mcp(argv: list[str]) -> int:
        from frisket.server.mcp.server import main as mcp_main

        return mcp_main(argv)

    def _remote(argv: list[str]) -> int:
        from frisket.remote.cli import remote_command

        return remote_command(argv)

    def _users(argv: list[str]) -> int:
        from frisket.remote.cli import users_command

        return users_command(argv)

    def _token(argv: list[str]) -> int:
        from frisket.remote.cli import token_command

        return token_command(argv)

    def _secrets(argv: list[str]) -> int:
        from frisket.remote.cli import secrets_command

        return secrets_command(argv)

    def _proxy(argv: list[str]) -> int:
        from frisket.remote.cli import proxy_command

        return proxy_command(argv)

    def _runtimes(argv: list[str]) -> int:
        from frisket.plugins.managed_runtime import managed_runtime_cli

        return managed_runtime_cli(argv)

    return {
        "server": (server, "run the single-replica production server"),
        "owner": (owner_admin, "recover the sole server owner account"),
        "queue": (queue_admin, "provision and migrate the hosted run queue"),
        "worker": (worker, "run the job-queue worker by itself"),
        "hosted-worker": (
            hosted_worker,
            "run the public-hosted worker with code execution disabled",
        ),
        "doctor": (doctor_cmd, "deployment self-probe (frisket doctor --help)"),
        "remote": (_remote, "manage linked team servers (link/status/list/default)"),
        "users": (
            _users,
            "manage a linked server's users (add/list/remove/reset/role)",
        ),
        "token": (_token, "rotate a linked server's operator token"),
        "secrets": (
            _secrets,
            "manage a linked server's provider keys (set/list/unset/push)",
        ),
        "proxy": (
            _proxy,
            "route a linked server's media downloads through this computer "
            "(up/status/down)",
        ),
        "mcp": (_mcp, "run the MCP server"),
        "runtimes": (
            _runtimes,
            "inspect/apply managed plugin runtimes (inspect|apply <name>)",
        ),
        "action": (action_cmd, "v1 action contracts (schema/validate/run)"),
        "plugin": (
            plugin_cmd,
            "trusted-local plugin author tools (init/validate/build/test-backend/dev)",
        ),
        "op": (
            op_tools,
            "first-party op author tools (try <kind> --input '{...}')",
        ),
        "reconcile": (
            reconcile_cmd,
            "decide stuck paid-effect checkpoints (list/discard/accept-charged)",
        ),
        "reset-drain": (
            reset_drain_cmd,
            "machine-readable stop-the-world reset preflight",
        ),
    }


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    from frisket import DISTRIBUTION_NAME

    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:  # editable/source checkout without metadata
        return "unknown"


def _usage() -> str:
    """Top-level help. The command list is generated from _subcommands() so it
    stays in sync with what actually dispatches."""
    commands = [
        (
            "[WORKSPACE] [PORT]",
            "serve the local tier (default ./frisket-projects on :8000)",
        ),
        *((f"{name} ...", desc) for name, (_h, desc) in _subcommands().items()),
    ]
    options = [
        ("-h, --help", "show this help and exit"),
        ("-V, --version", "show the installed frisket version and exit"),
        ("-y, --yes", "create a missing workspace without confirmation"),
    ]
    width = max(len(label) for label, _ in commands + options)
    cmd_lines = "\n".join(
        f"  frisket {label:<{width}}  {desc}" for label, desc in commands
    )
    opt_lines = "\n".join(f"  {label:<{width + 8}}  {desc}" for label, desc in options)
    return (
        "frisket — local-first structured-data workspace\n\n"
        "Usage:\n" + cmd_lines + "\n\n"
        "Options:\n" + opt_lines + "\n\n"
        "Run a subcommand with --help for its own options.\n"
    )


def _usage_error(message: str) -> "SystemExit":
    """Friendly top-level usage error: one-line reason + usage, exit 2."""
    print(f"frisket: {message}\n", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return SystemExit(2)


def _looks_pathlike(token: str) -> bool:
    """Tell a workspace path apart from a mistyped subcommand: a real or
    intended path has a separator, starts with '.' or '~', or names the
    workspace suffix explicitly. Anything else that isn't a registered
    subcommand and doesn't already exist on disk is more likely a typo than
    a workspace name someone meant to create."""
    return (
        "/" in token
        or os.sep in token
        or token.startswith((".", "~"))
        or token.endswith(".frisket")
    )


def _unknown_command_error(token: str, commands: "dict[str, tuple]") -> "SystemExit":
    """A mistyped subcommand must not be mistaken for a workspace name — the
    default serve path mkdir's whatever it's given (create_app) and boots a
    server on it. Only a registered subcommand, an existing path, or
    something that looks like a path reaches the serve path; everything else
    is treated as a typo."""
    matches = difflib.get_close_matches(token, commands.keys(), n=1)
    if matches:
        print(
            f"frisket: unknown command '{token}' (did you mean '{matches[0]}'?)",
            file=sys.stderr,
        )
    else:
        print(f"frisket: unknown command '{token}'", file=sys.stderr)
    print(
        "frisket: to serve a workspace, pass an existing path or one that "
        f"looks like a path (./{token})",
        file=sys.stderr,
    )
    return SystemExit(2)


def _confirm_create_workspace(workspace: Path, *, assume_yes: bool) -> bool:
    """Confirm before create_app() mkdir's a brand-new workspace. Existing
    workspaces, --yes / FRISKET_YES, and non-interactive runs (no TTY: CI, e2e,
    Docker, pipes) proceed without prompting so automation never blocks on a
    prompt it can't answer."""
    if workspace.exists() or assume_yes:
        return True
    if not sys.stdin.isatty():
        return True
    reply = input(f"Create a new frisket workspace at {workspace}? [y/N] ")
    return reply.strip().lower() in ("y", "yes")


def _port_available(host: str, port: int) -> bool:
    """Bind-and-release before create_app()/uvicorn claim the port, so a
    collision is caught before the workspace directory is created and before
    the startup banner has a chance to claim success. SO_REUSEADDR mirrors
    what the eventual server bind will do, so a socket still draining in
    TIME_WAIT isn't reported as busy."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _print_startup_banner(url: str, *, serves_ui: bool) -> None:
    """Tell the operator exactly what to open. When the built UI is served
    (packaged install or FRISKET_STATIC_DIR), point straight at the URL. In a
    dev/source checkout the backend is API-only and vite serves the UI, so
    print the real two-step: start vite, open its URL — vite proxies /api here
    (web/vite.config.ts targets :8000)."""
    print(f"\n  frisket is serving the API at {url}", flush=True)
    if serves_ui:
        print(f"  Open {url} in your browser to get started.\n", flush=True)
    else:
        print(
            "  Dev mode: the built UI is not packaged here, so vite serves it.\n"
            "  In another terminal:  npm --prefix web run dev\n"
            "  then open the URL it prints (default http://localhost:5173) —\n"
            f"  vite proxies /api to this server ({url}).\n",
            flush=True,
        )


def _standalone_env(data_dir: Path, database_url: str) -> None:
    """Set the exact common locator inherited by app and worker processes."""
    os.environ["FRISKET_DATA_DIR"] = str(data_dir)
    os.environ["FRISKET_PROJECTS_ROOT"] = str(data_dir)
    os.environ["FRISKET_TEAM_DATABASE_URL"] = database_url
    os.environ["FRISKET_DATABASE_URL"] = database_url
    os.environ["FRISKET_RUN_QUEUE_DATABASE_URL"] = database_url
    os.environ["FRISKET_RUN_QUEUE_SCHEMA_MODE"] = "initialize"
    os.environ["FRISKET_SECRETS_KEY_FILE"] = str(data_dir / "secrets" / "master.key")


def _stop_server_worker(proc: "subprocess.Popen | None") -> None:
    """Use the existing bounded graceful shutdown policy for the child."""
    _stop_worker(proc)


def _request_server_worker_stop(proc: "subprocess.Popen | None") -> None:
    """Forward a terminating server signal without blocking its handler."""
    if proc is not None and proc.poll() is None:
        proc.terminate()


def server(argv: list[str]) -> int:
    """Run the one-replica Standalone server composition.

    The app initializes its common SQLite schema before the independently
    connected worker is started.  A worker crash makes the service exit
    nonzero so the surrounding host restarts the whole single-replica unit.
    """
    ap = argparse.ArgumentParser(prog="frisket server")
    ap.add_argument("--data-dir", default=os.environ.get("FRISKET_DATA_DIR", "/data"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args(argv)
    if not 0 < args.port < 65536:
        ap.error("--port must be between 1 and 65535")
    base_url = os.environ.get("FRISKET_BASE_URL", "").strip()
    if not base_url:
        print("frisket server: FRISKET_BASE_URL is required", file=sys.stderr)
        return 2

    import threading
    import uvicorn

    from frisket.server.standalone import (
        StandaloneAlreadyRunning,
        StandaloneLifetimeLock,
        StandaloneRuntimeState,
    )
    from frisket.server.workspace import DataRootUnwritable
    from frisket.operability.structured_logging import configure_logging
    from frisket.team.app import create_team_app_from_env

    data_dir = Path(args.data_dir).expanduser().resolve()
    database_url = _standalone_database_url(data_dir)
    state = StandaloneRuntimeState()
    try:
        with StandaloneLifetimeLock(data_dir):
            _standalone_env(data_dir, database_url)
            configure_logging()
            # This completes team/control and queue schema initialization
            # before the worker can claim anything from the shared database.
            app = create_team_app_from_env()
            app.state.standalone_runtime = state
            worker_proc = subprocess.Popen(_database_worker_argv(database_url))
            state.worker_pid = worker_proc.pid
            watcher: threading.Thread | None = None
            try:
                config = uvicorn.Config(
                    app,
                    host="0.0.0.0",
                    port=args.port,
                    log_config=None,
                    proxy_headers=False,
                )
                uvicorn_server = uvicorn.Server(config)

                # Uvicorn installs ``handle_exit`` as the process signal
                # handler. Wrap it so the child begins its own graceful stop
                # while HTTP drains, not only after run() returns.
                original_handle_exit = getattr(uvicorn_server, "handle_exit", None)
                if callable(original_handle_exit):

                    def handle_exit(sig, frame) -> None:
                        state.begin_stopping()
                        _request_server_worker_stop(worker_proc)
                        original_handle_exit(sig, frame)

                    uvicorn_server.handle_exit = handle_exit

                def watch_worker() -> None:
                    worker_proc.wait()
                    if not state.stopping:
                        state.worker_exited_unexpectedly = True
                        uvicorn_server.should_exit = True

                watcher = threading.Thread(target=watch_worker, daemon=True)
                watcher.start()
                logging.getLogger("frisket.server").info(
                    "standalone_server_started",
                    extra={
                        "event": "standalone_server_started",
                        "data_dir": str(data_dir),
                        "url": base_url,
                        "worker_pid": worker_proc.pid,
                    },
                )
                uvicorn_server.run()
                return 1 if state.worker_exited_unexpectedly else 0
            finally:
                state.begin_stopping()
                _stop_server_worker(worker_proc)
                if watcher is not None:
                    watcher.join(timeout=1)
    except StandaloneAlreadyRunning as exc:
        print(f"frisket server: {exc}", file=sys.stderr)
        return 1
    except DataRootUnwritable as exc:
        print(f"frisket server: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    argv = sys.argv[1:]

    # Help/version are handled before anything touches the filesystem so a typo
    # like `frisket --help` can't be mistaken for a workspace path — which would
    # silently mkdir a junk directory (create_app mkdir's the workspace) and
    # boot a server. Subcommands' own parsers (argparse / doctor) handle their
    # `--help`.
    if argv and argv[0] in ("-h", "--help"):
        print(_usage())
        raise SystemExit(0)
    if argv and argv[0] in ("-V", "--version"):
        print(f"frisket {_version()}")
        raise SystemExit(0)

    commands = _subcommands()
    if argv and argv[0] in commands:
        handler = commands[argv[0]][0]
        raise SystemExit(handler(argv[1:]))

    # A bare typo (`frisket doctro`) must not fall through to the serve path
    # below — that path mkdir's whatever it's given and boots a server on
    # it. Anything that isn't a registered subcommand, doesn't already exist
    # on disk, and doesn't look like a path is rejected here as a typo.
    if (
        argv
        and not argv[0].startswith("-")
        and not Path(argv[0]).exists()
        and not _looks_pathlike(argv[0])
    ):
        raise _unknown_command_error(argv[0], commands)

    # Default: serve. Args are [WORKSPACE] [PORT] plus -y/--yes. Reject stray
    # options and surplus args here instead of turning them into a workspace dir.
    assume_yes = bool(os.environ.get("FRISKET_YES"))
    positionals: list[str] = []
    for arg in argv:
        if arg in ("-y", "--yes"):
            assume_yes = True
        elif arg.startswith("-"):
            raise _usage_error(f"unknown option '{arg}'")
        else:
            positionals.append(arg)
    if len(positionals) > 2:
        raise _usage_error("too many arguments (expected at most WORKSPACE and PORT)")

    workspace = Path(positionals[0]) if positionals else Path.cwd() / "frisket-projects"
    if len(positionals) > 1:
        try:
            port = int(positionals[1])
        except ValueError:
            raise _usage_error(
                f"PORT must be an integer, got '{positionals[1]}'"
            ) from None
    else:
        port = 8000
    if not 0 < port < 65536:
        raise _usage_error(f"PORT must be between 1 and 65535, got {port}")

    # Checked before create_app()/the startup banner: a busy port must fail
    # here, not after the banner has already claimed success and a fresh
    # workspace directory has been left behind.
    host = "127.0.0.1"
    if not _port_available(host, port):
        print(f"frisket: port {port} is already in use", file=sys.stderr)
        print(
            f"frisket: try a different port, e.g. frisket {workspace} {port + 1}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not _confirm_create_workspace(workspace, assume_yes=assume_yes):
        print("frisket: aborted (workspace not created)", file=sys.stderr)
        raise SystemExit(1)

    import uvicorn

    from frisket.server.app import create_app
    from frisket.server.static_serving import resolve_static_dir
    from frisket.operability.structured_logging import configure_logging

    configure_logging()
    app = create_app(workspace)
    worker_proc = _spawn_worker(workspace)
    url = f"http://localhost:{port}"
    logging.getLogger("frisket.server").info(
        "server_started",
        extra={
            "event": "server_started",
            "workspace_root": str(workspace),
            "url": url,
        },
    )
    _print_startup_banner(url, serves_ui=resolve_static_dir() is not None)
    try:
        uvicorn.run(app, host=host, port=port, log_config=None)
    finally:
        _stop_worker(worker_proc)
