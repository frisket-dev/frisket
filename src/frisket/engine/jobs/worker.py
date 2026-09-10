"""The worker loop — the same code over either JobQueue backend.

Loop: recover expired leases → claim → execute the registered handler →
record (complete, or fail with exponential backoff). While a handler runs, a
heartbeat thread extends the job's lease, so long jobs survive and dead
workers' jobs do not.

Handlers are contextual callables
`(payload: dict, context: JobHandlerContext) -> result`, registered on a
HandlerRegistry. ``register_production_handlers`` is the ONE product
registration (project runs, action jobs, source polls, enclosure downloads,
embeddings, watches, notifications) that every edition composes;
``default_registry`` wires only the echo test handler for bare-loop tests.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from frisket.ai.llm import ResponseCache
from frisket.authoring.workbench.plugin_runtime_shared import PluginCompositionPolicy
from frisket.redaction import (
    DEFAULT_ERROR_TEXT_MAX_CHARS,
    SafeError,
    canonical_error_code,
    redact_text,
    safe_error,
    safe_stack_frames,
)
from frisket.operability.structured_logging import bind_log_context, job_log_context
from frisket.engine.worker_version import code_version

from frisket.project_identity import ProjectStorageKey

from .editions import (
    CLOUD_EDITION,
    WORKER_EDITION_GROUP,
    available_worker_editions,
    load_worker_edition,
)
from .ports import JobHandlerContext, WorkerPorts, coerce_worker_ports
from frisket.execution.pricing_policy import (
    PricingPolicy,
    install_pricing_policy,
)
from frisket.execution.provider import ExecutionCompositionFactory
from .project_opener import (
    ProjectOpener,
    open_claimed_project,
    require_opener_storage_org_id,
)
from .queue import (
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    Job,
    JobQueue,
    claimed_project_location,
    normalize_queue_org_id as _normalize_queue_org_id,
)

Handler = Callable[[dict, JobHandlerContext], object]


@dataclass(frozen=True, slots=True, eq=False)
class HandlerRegistration:
    """Identity of one installed job handler."""

    kind: str
    origin: str
    _handler: Handler = field(repr=False)


# Re-exported so ``frisket.jobs.worker.ProjectOpener`` — the name compositions
# import — resolves next to the registration functions that take it.
__all__ = [
    "ProjectOpener",
    "open_claimed_project",
    "require_opener_storage_org_id",
]

ECHO_KIND = "echo"
STRICT_STORAGE_HANDLER_ORIGIN = "frisket.production.strict_storage"
LOG = logging.getLogger("frisket.worker")


class NonRetryableJobError(RuntimeError):
    """Raised by a handler to force this job to a terminal ``failed`` state
    immediately, regardless of remaining attempts. This is a small, GENERIC
    mechanism, not specific to ``model.pull``, so any handler can distinguish
    "this will never succeed on retry" from an ordinary transient failure.
    ``Worker._execute`` maps it to
    ``queue.fail(..., retry=False)``; every other handler's plain exceptions
    keep the existing retry-with-backoff semantics unchanged."""


def _safe_error_fields(error: SafeError) -> dict[str, object]:
    fields: dict[str, object] = {
        "error_code": error.code,
        "error": error.text,
    }
    if error.exception_type is not None:
        fields["exception_type"] = error.exception_type
    if error.frames:
        fields["exception_frames"] = error.frames
    return fields


def _non_retryable_safe_error(exc: NonRetryableJobError) -> SafeError:
    fallback = "non_retryable_job_error"
    try:
        raw_detail = str(exc)
    except Exception:  # noqa: BLE001 - never repr an exception
        raw_detail = "operation failed"
    redacted = redact_text(
        raw_detail,
        max_chars=DEFAULT_ERROR_TEXT_MAX_CHARS,
        one_line=True,
    )
    candidate, separator, detail = redacted.partition(":")
    candidate = candidate.strip()
    if separator and canonical_error_code(candidate, fallback=fallback) == candidate:
        code = candidate
        detail = detail.lstrip()
    else:
        code = fallback
        detail = redacted
    detail = redact_text(
        detail or "operation failed",
        max_chars=DEFAULT_ERROR_TEXT_MAX_CHARS - len(code) - 2,
        one_line=True,
    )
    return SafeError(
        code=code,
        detail=detail or "operation failed",
        exception_type=redact_text(type(exc).__name__, max_chars=256),
        frames=safe_stack_frames(exc.__traceback__),
    )


class HandlerRegistry:
    """The ONE registration surface for a worker composition.

    Job-kind handlers (``register``) drive the worker loop. Action-job
    executors (``register_action_executor``) are the second dispatch level
    inside the generic ``action.run`` handler: one executor per action kind,
    keyed here — on the registry the composition already owns — rather than in
    a second process-global registry.

    Every registered handler receives immutable claimed-row context separately
    from its mutable payload. Plugin-authored one-argument handlers are adapted
    once at their installation boundary.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._registrations: dict[str, HandlerRegistration] = {}
        self._action_executors: dict[str, Callable[[Any, Any], Any]] = {}
        self._recovery_hooks: list[Callable[[JobQueue], None]] = []

    @staticmethod
    def _callable_origin(fn: Handler) -> str:
        module = getattr(fn, "__module__", type(fn).__module__)
        name = getattr(fn, "__qualname__", type(fn).__qualname__)
        return f"{module}.{name}"

    def _install(self, kind: str, fn: Handler, *, origin: str) -> HandlerRegistration:
        registration = HandlerRegistration(kind=kind, origin=origin, _handler=fn)
        self._handlers[kind] = fn
        self._registrations[kind] = registration
        return registration

    def register(self, kind: str, fn: Handler) -> None:
        """Legacy direct registration.

        Intentional wrappers use :meth:`decorate`; existing test and plugin
        seams retain direct replacement until they are migrated deliberately.
        """

        self._install(kind, fn, origin=self._callable_origin(fn))

    def add(self, kind: str, fn: Handler, *, origin: str) -> HandlerRegistration:
        """Install an initial handler, refusing an existing registration."""

        current = self._registrations.get(kind)
        if current is not None:
            raise ValueError(
                f"handler {kind!r} is already registered by {current.origin!r}; "
                f"attempted origin {origin!r}"
            )
        return self._install(kind, fn, origin=origin)

    def registration(
        self, kind: str, *, origin: str | None = None
    ) -> HandlerRegistration:
        """Return the current identity, optionally requiring its exact origin."""

        current = self._registrations.get(kind)
        if current is None:
            raise ValueError(f"handler {kind!r} is not registered")
        if origin is not None and current.origin != origin:
            raise ValueError(
                f"handler {kind!r} has origin {current.origin!r}; "
                f"expected origin {origin!r}"
            )
        return current

    def decorate(
        self,
        kind: str,
        *,
        expected: HandlerRegistration,
        decorator: Callable[[Handler], Handler],
        origin: str,
    ) -> HandlerRegistration:
        """Replace exactly ``expected`` with one explicitly named wrapper."""

        current = self._registrations.get(kind)
        if (
            current is not expected
            or expected.kind != kind
            or self._handlers.get(kind) is not expected._handler
        ):
            current_origin = current.origin if current is not None else "<unregistered>"
            raise ValueError(
                f"cannot decorate handler {kind!r} from origin {origin!r}: "
                f"current origin is {current_origin!r}, expected "
                f"{expected.origin!r} with the exact registration identity"
            )
        decorated = decorator(expected._handler)
        if not callable(decorated):
            raise TypeError(
                f"handler decorator from {origin!r} did not return callable"
            )
        if (
            self._registrations.get(kind) is not expected
            or self._handlers.get(kind) is not expected._handler
        ):
            raise ValueError(
                f"handler {kind!r} changed while decorator from {origin!r} was built"
            )
        return self._install(kind, decorated, origin=origin)

    def register_action_executor(
        self, kind: str, fn: Callable[[Any, Any], Any]
    ) -> None:
        self._action_executors[kind] = fn

    def register_recovery_hook(self, fn: Callable[[JobQueue], None]) -> None:
        self._recovery_hooks.append(fn)

    def get(self, kind: str) -> Handler | None:
        return self._handlers.get(kind)

    def action_executor(self, kind: str) -> Callable[[Any, Any], Any] | None:
        return self._action_executors.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def run_recovery_hooks(self, queue: JobQueue) -> None:
        for hook in tuple(self._recovery_hooks):
            hook(queue)


def default_registry() -> HandlerRegistry:
    """A registry with only the echo test handler — the bare worker loop.
    Product compositions call ``register_production_handlers`` instead."""
    reg = HandlerRegistry()
    reg.register(ECHO_KIND, lambda payload, _context: dict(payload))
    return reg


def register_action_job_binding_handlers(
    registry: HandlerRegistry,
    *,
    executor_deps_factory: Callable[[str, Any], Any] | None = None,
) -> None:
    """Install every canonical action-job executor on the worker registry."""

    from frisket.engine.executor.action_bindings import action_job_bindings

    for kind, executor in action_job_bindings(
        executor_deps_factory=executor_deps_factory
    ).items():
        registry.register_action_executor(kind, executor)


def register_production_handlers(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    router: Any | None = None,
    control_database_url: str | None = None,
    executor_deps_factory: Callable[[str, Any], Any] | None = None,
    notification_delivery_runtime: Any | None = None,
    notification_delivery_runtime_factory: Any | None = None,
    worker_ports: WorkerPorts | None = None,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
    install_queue_terminalization: bool = True,
    pricing_policy: PricingPolicy | None = None,
    plugin_composition_policy: PluginCompositionPolicy | None = None,
    execution_router_factory: Callable[[], Any] | None = None,
    response_cache_factory: Callable[[JobHandlerContext], ResponseCache | None]
    | None = None,
    execution_composition_factory: ExecutionCompositionFactory | None = None,
) -> ProjectOpener | None:
    """Register the product handlers on the OPEN worker.

    This is the ONE product registration: every edition composes it. Edition
    policy is injected as explicit ports (``worker_ports`` — credential /
    admission / settlement, see ``frisket.jobs.ports``), never imported. With no
    ports the worker is exactly the trusted local/team worker: it admits every
    action, settles nothing, and resolves org BYOK keys from the open identity
    control plane. An external edition composes this same function with its
    own ports.

    ``workspace_root_storage_org_id`` declares that ``workspace_root`` is the
    org-scoped storage directory of exactly that org (the hosted per-org
    ``Workspace``); see ``frisket.jobs.queue.claimed_project_location``. Leave
    it ``None`` for global/local roots.

    ``project_opener`` injects how every handler opens its
    claimed project; ``claimed_project_location`` keeps deciding WHERE. They
    compose as ONE story — location first, then the opener consumes exactly
    that location — never as parallel paths. Returned unchanged so a
    composition root can hand the same callable to its scheduler scans.

    ``install_queue_terminalization=False`` is for a composition that owns one
    global terminalization router for a shared multi-tenant queue. It prevents
    later per-workspace handler registration from replacing that queue-global
    hook; local/single-workspace callers retain the existing default.
    """
    if execution_composition_factory is not None and not callable(
        execution_composition_factory
    ):
        raise TypeError(
            "execution_composition_factory must be a callable returning an "
            "ExecutionComposition (or None for the open default); got "
            f"{type(execution_composition_factory).__name__}"
        )
    if execution_router_factory is not None and not callable(execution_router_factory):
        raise TypeError("execution_router_factory must be callable")
    if response_cache_factory is not None and not callable(response_cache_factory):
        raise TypeError("response_cache_factory must be callable")

    from frisket.engine.executor.queue_terminalization import (
        register_queue_terminalization,
    )
    from frisket.server.notifications.delivery import default_delivery_runtime

    from .embeddings import register_embedding_refresh_handler
    from .enclosures import register_enclosure_download_handler
    from .notifications_delivery import register_notification_handlers
    from .notifications_digest import register_notification_digest_handlers
    from .runs import register_action_run_handler, register_project_run_handler
    from .sources import register_source_poll_handler
    from .watches import register_watch_evaluate_handler

    # The deployment's price list, installed process-wide (see
    # ``frisket.execution.pricing_policy`` for why it cannot ride ``ports``:
    # the five executor-family mints are reached through builders that rebuild
    # ExecutorDeps router-only, so a deps field would be silently dropped).
    # A downstream composition's ``worker.py`` is the intended caller, and it
    # must install the SAME policy its app does — a worker that rates
    # differently from the gate that quoted the run would refuse its own
    # consent.
    if pricing_policy is not None:
        install_pricing_policy(pricing_policy)
    # A separately deployed worker must install the same plugin exposure
    # policy as the app that admitted the job. Without this explicit seam a
    # fresh worker process would fall back to the permissive open policy.
    if plugin_composition_policy is not None:
        from frisket.authoring.workbench.plugin_runtime_shared import (
            install_plugin_composition_policy,
        )

        install_plugin_composition_policy(plugin_composition_policy)
    ports = coerce_worker_ports(worker_ports)
    root = Path(workspace_root)
    # Standalone workers do not construct ``Workspace``. Rehydrate the shared
    # registry here, after installing the exact same composition policy, so a
    # restrictive worker cannot revive catalog packages the app omitted.
    from frisket.authoring.workbench.plugin_runtime import (
        ensure_workspace_plugin_packages,
    )

    ensure_workspace_plugin_packages(root, policy=plugin_composition_policy)
    # Layering inversion seam: the queue never imports product stores; the
    # product terminalization hook (paired receipts / run rows on terminal
    # queue events) is registered here, on the open queue, with the SAME
    # declared storage roots the handlers use.
    if install_queue_terminalization:
        register_queue_terminalization(
            queue,
            workspace_root=root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            project_opener=project_opener,
        )
    # queue-hosted-posture-explicit-v1: strict storage-identity enforcement
    # follows the queue's DECLARED posture, never the engine dialect. A queue
    # opened with hosted posture enforces the full identity contract even on a
    # SQLite engine; a local queue never does.
    strict_hosted_storage = require_storage_identity and bool(
        getattr(queue, "_hosted_storage", False)
    )
    project_run_registration = register_project_run_handler(
        registry,
        workspace_root=root,
        router=router,
        control_database_url=control_database_url,
        worker_ports=ports,
        require_storage_identity=strict_hosted_storage,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        executor_deps_factory=executor_deps_factory,
        project_opener=project_opener,
        execution_router_factory=execution_router_factory,
        response_cache_factory=response_cache_factory,
        execution_composition_factory=execution_composition_factory,
    )
    action_run_registration = register_action_run_handler(
        registry,
        workspace_root=root,
        router=router,
        control_database_url=control_database_url,
        worker_ports=ports,
        require_storage_identity=strict_hosted_storage,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    register_action_job_binding_handlers(
        registry,
        executor_deps_factory=executor_deps_factory,
    )
    enclosure_download_registration = register_enclosure_download_handler(
        registry,
        workspace_root=root,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    source_poll_registration = register_source_poll_handler(
        registry,
        workspace_root=root,
        queue=queue,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    register_embedding_refresh_handler(
        registry,
        workspace_root=root,
        router=router,
        queue=queue,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    watch_evaluate_registration = register_watch_evaluate_handler(
        registry,
        workspace_root=root,
        queue=queue,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    notification_delivery_registration = register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime=(
            notification_delivery_runtime
            or (
                default_delivery_runtime()
                if notification_delivery_runtime_factory is None
                else None
            )
        ),
        delivery_runtime_factory=notification_delivery_runtime_factory,
        queue=queue,
        storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    notification_digest_registration = register_notification_digest_handlers(
        registry,
        workspace_root=root,
        queue=queue,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
        project_opener=project_opener,
    )
    if strict_hosted_storage:
        # Every hosted product handler consumes the same claimed physical
        # identity.  The guard overrides caller-controlled path diagnostics
        # before legacy handler bodies see them and fails closed if the worker
        # did not reconstruct a key from queue columns.
        registrations = {
            "project.run": project_run_registration,
            "action.run": action_run_registration,
            "watch.evaluate": watch_evaluate_registration,
            "source.poll": source_poll_registration,
            "enclosure.download": enclosure_download_registration,
            "notification.deliver": notification_delivery_registration,
            "notification.digest": notification_digest_registration,
        }

        def with_trusted_storage(handler: Handler) -> Handler:
            def trusted_handler(payload: dict, context: JobHandlerContext) -> object:
                claimed = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
                if not isinstance(claimed, ProjectStorageKey):
                    raise ValueError(
                        "hosted queue handler requires claimed storage identity"
                    )
                _project_id, project_root, _project_path = claimed_project_location(
                    payload,
                    workspace_root=root,
                    require_storage_identity=True,
                    workspace_root_storage_org_id=workspace_root_storage_org_id,
                )
                trusted_payload = dict(payload)
                # Identity comes from the claimed row, never a parsed directory name.
                trusted_payload["project_id"] = claimed.project_slug
                trusted_payload["storage_org_id"] = claimed.storage_org_id
                trusted_payload["workspace_root"] = str(project_root)
                return handler(trusted_payload, context)

            return trusted_handler

        for kind, registration in registrations.items():
            registry.decorate(
                kind,
                expected=registration,
                decorator=with_trusted_storage,
                origin=STRICT_STORAGE_HANDLER_ORIGIN,
            )
    return project_opener


def register_hosted_handlers(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    router: Any | None = None,
    control_database_url: str | None = None,
    executor_deps_factory: Callable[[str, Any], Any] | None = None,
    notification_delivery_runtime: Any | None = None,
    notification_delivery_runtime_factory: Any | None = None,
    execution_composition_factory: ExecutionCompositionFactory | None = None,
) -> ProjectOpener | None:
    """Compose the hosted worker: the open worker plus an external edition's ports.

    This is a SEAM, not a policy. The hosted policy — the code-execution
    admission that keeps public tenants out of python/agent actions, the funding
    settlement reconcile, the spend-capped BYOK credentials — lives outside
    this tree, in an external package that registers its composition under the
    ``frisket.worker_editions`` entry point (explicit ports / entry-point
    registration; monkey-patching REJECTED). The open tree therefore
    never names it, and still imports and starts with that external package
    physically absent.

    Fails CLOSED: if the external edition is not installed there is no hosted
    worker. It never degrades into the trusted local worker, which would run the
    very code the hosted tier exists to refuse.
    """

    compose = load_worker_edition(CLOUD_EDITION)
    if compose is None:
        raise RuntimeError(
            f"the {CLOUD_EDITION!r} worker edition is not installed "
            f"(no {WORKER_EDITION_GROUP!r} entry point named {CLOUD_EDITION!r}; "
            f"installed editions: {available_worker_editions() or 'none'}). "
            "Refusing to register hosted handlers: without that edition's "
            "admission port this worker would execute the arbitrary code the "
            "hosted tier exists to refuse."
        )
    # The edition's return value is its composed ProjectOpener (or None),
    # forwarded unchanged so the worker CLI can hand the SAME open seam to its
    # scheduler scans instead of letting them fall back to direct opens.
    compose_kwargs = {
        "workspace_root": workspace_root,
        "queue": queue,
        "router": router,
        "control_database_url": control_database_url,
        "executor_deps_factory": executor_deps_factory,
        "notification_delivery_runtime": notification_delivery_runtime,
        "execution_composition_factory": execution_composition_factory,
    }
    if notification_delivery_runtime_factory is not None:
        compose_kwargs["notification_delivery_runtime_factory"] = (
            notification_delivery_runtime_factory
        )
    return compose(registry, **compose_kwargs)


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        registry: HandlerRegistry | None = None,
        *,
        worker_id: str | None = None,
        poll_interval: float = 0.5,
        lease_seconds: float = 60.0,
        retry_base_seconds: float = 2.0,
        retry_cap_seconds: float = 300.0,
        liveness_interval: float | None = None,
        queue_label: str | None = None,
        clock: Any | None = None,
    ):
        self.queue = queue
        self.registry = registry or default_registry()
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        # This process's own code identity is retained with heartbeats and
        # claims for operator diagnostics across releases.
        self.worker_version = code_version()
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_cap_seconds = retry_cap_seconds
        # Liveness heartbeats are decoupled from job leases: the worker stamps
        # its presence every idle/active poll (throttled) so the status/health
        # surfaces can tell "no worker" apart from "worker busy on a long job".
        self.liveness_interval = (
            liveness_interval
            if liveness_interval is not None
            else max(lease_seconds / 2.0, 1.0)
        )
        self.queue_label = queue_label
        self._last_liveness_monotonic: float | None = None
        # record_liveness() now runs from two threads — the main loop
        # (run_once) and the background lease-
        # heartbeat thread during a job — so the throttle's check-then-set on
        # _last_liveness_monotonic needs a lock: unlocked, both threads could
        # pass the throttle check before either sets the timestamp, doubling
        # up an otherwise-throttled heartbeat write (benign — an extra write,
        # not corruption — but cheap to close).
        self._liveness_lock = threading.Lock()
        # Injected time source behind the liveness throttle (rate_limiter's
        # now_ms idiom). The idle-poll and heartbeat cadences stay real
        # Event.waits — bounded positive waits tests never assert on.
        if clock is None:
            from frisket.engine.jobs.rate_limiter import SystemClock

            clock = SystemClock()
        self._clock = clock
        # Test-visible transition seam (same shape as the queue's
        # TerminalizationHook): fired at "claimed" and "finalized" so tests
        # await state transitions instead of polling job rows with sleeps.
        self._transition_hook: Callable[[str, Job], None] | None = None

    def set_transition_hook(self, hook: Callable[[str, Job], None] | None) -> None:
        self._transition_hook = hook

    def _notify_transition(self, event: str, job: Job) -> None:
        if self._transition_hook is not None:
            self._transition_hook(event, job)

    # ---------- one iteration ----------

    def record_liveness(self, *, force: bool = False) -> None:
        """Throttled worker-presence heartbeat (best-effort)."""
        with self._liveness_lock:
            nowm = self._clock.now_ms() / 1000.0
            if (
                not force
                and self._last_liveness_monotonic is not None
                and (nowm - self._last_liveness_monotonic) < self.liveness_interval
            ):
                return
            try:
                self.queue.record_worker_heartbeat(
                    self.worker_id,
                    queue=self.queue_label,
                    worker_version=self.worker_version,
                    # Which capabilities THIS worker actually advertises, so a
                    # health surface can report live model.pull-capable
                    # workers, not just "some worker is alive". Empty
                    # registry -> None (nothing to advertise), matching the
                    # existing queue/worker_version "unknown, keep prior
                    # value" convention rather than clobbering it with "".
                    kinds=",".join(self.registry.kinds()) or None,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort liveness
                safe = safe_error(
                    "worker_liveness_heartbeat_failed",
                    exc,
                    include_frames=True,
                )
                LOG.debug(
                    "worker_liveness_heartbeat_failed",
                    extra={
                        "event": "worker_liveness_heartbeat_failed",
                        **_safe_error_fields(safe),
                    },
                    exc_info=False,
                )
                return
            self._last_liveness_monotonic = nowm

    def run_once(self) -> bool:
        """Recover, claim, execute one job. Returns False when idle."""
        self.record_liveness()
        if self.queue.recover_expired():
            self.registry.run_recovery_hooks(self.queue)
        job = self.queue.claim(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            worker_version=self.worker_version,
        )
        if job is None:
            return False
        self._notify_transition("claimed", job)
        try:
            # _execute drives the job row to a finalize attempt, but the row
            # may already have been cancelled or lease-recovered while the
            # handler was still executing. Handler exit is therefore a
            # separate durable acknowledgement in this finally block.
            self._execute(job)
        finally:
            try:
                self.queue.acknowledge_handler_exit(
                    job.id,
                    self.worker_id,
                    authority_id=job.handler_authority_id,
                )
            finally:
                self._notify_transition("finalized", job)
        return True

    def _backoff(self, attempts: int) -> float:
        return min(
            self.retry_base_seconds * (2 ** max(attempts - 1, 0)),
            self.retry_cap_seconds,
        )

    def _execute(self, job: Job) -> None:
        with bind_log_context(**job_log_context(job.id, job.payload)):
            LOG.info(
                "job_started",
                extra={
                    "event": "job_started",
                    "job_kind": job.kind,
                    "worker_id": self.worker_id,
                    "attempt": job.attempts,
                    "max_attempts": job.max_attempts,
                },
            )
            if job.code_version is not None and job.code_version != self.worker_version:
                # Keep release/worktree differences visible without changing
                # queue control flow. WARNING (not DEBUG) makes the diagnostic
                # available in production logs with DEBUG disabled.
                LOG.warning(
                    "worker_version_mismatch: job %s enqueued by code_version=%r,"
                    " claimed by worker_version=%r (worker_id=%s, kind=%s)."
                    " A different release or worktree shares this queue;"
                    " this is diagnostic only.",
                    job.id,
                    job.code_version,
                    self.worker_version,
                    self.worker_id,
                    job.kind,
                    extra={
                        "event": "worker_version_mismatch",
                        "job_id": job.id,
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                        "enqueued_code_version": job.code_version,
                        "worker_code_version": self.worker_version,
                    },
                )
            handler = self.registry.get(job.kind)
            if handler is None:
                # a config error, not a transient one — never retry
                safe = safe_error(
                    "no_handler_registered",
                    f"no handler registered for kind {job.kind!r}",
                )
                self.queue.fail(job.id, self.worker_id, safe.text, retry=False)
                LOG.error(
                    "job_failed",
                    extra={
                        "event": "job_failed",
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                        "retry": False,
                        **_safe_error_fields(safe),
                    },
                    exc_info=False,
                )
                return
            # The credential-owning org is the first-class queue column org_id,
            # written at enqueue and immutable in the row — the SAME trust
            # boundary the storage columns select the physical project from.
            # The mutable JSON payload["org_id"] a forged/stale/mis-migrated
            # body carries must never resolve another tenant's BYOK keys against
            # this org's project. Reconcile the two here (the seam that already
            # overrides project_id/storage_org_id from claimed columns): a
            # divergence or a malformed identity fails closed rather than run
            # org A's data with org B's credentials/billing.
            #
            # TRUST INVARIANT: job.org_id is itself only as
            # trusted as the ENQUEUE producer — server/action_enqueue.py spreads
            # the authenticated, server-controlled ``ctx.queue_payload_extra``
            # (which carries {org_id, storage_org_id}) LAST into the payload, so
            # both the column and the payload copy reflect the authenticated org
            # at enqueue. This reconciliation therefore closes the DURABLE-
            # PAYLOAD-TAMPER vector (a stored payload forged/mutated/mis-migrated
            # after enqueue while the column stays valid). It does NOT substitute
            # for enqueue-time authz: a caller who bypasses action_enqueue and
            # calls queue.enqueue directly with a forged org in BOTH is an
            # enqueue-boundary concern, not the worker's — queue.enqueue is an
            # internal API, no production path reaches it caller-controlled.
            try:
                trusted_org_id = _normalize_queue_org_id(job.org_id)
                payload_org_id = _normalize_queue_org_id(
                    (job.payload or {}).get("org_id")
                )
            except (TypeError, ValueError) as exc:
                safe = safe_error(
                    "invalid_org_identity",
                    exc,
                    include_frames=True,
                )
                self.queue.fail(job.id, self.worker_id, safe.text, retry=False)
                LOG.error(
                    "job_failed",
                    extra={
                        "event": "job_failed",
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                        "retry": False,
                        **_safe_error_fields(safe),
                    },
                    exc_info=False,
                )
                return
            if payload_org_id != trusted_org_id:
                safe = safe_error(
                    "org_identity_mismatch",
                    f"refusing job {job.id}; the durable "
                    f"payload names org_id={payload_org_id!r} but the trusted "
                    f"queue column names org_id={trusted_org_id!r}. Credentials "
                    "resolve from the trusted column, never the payload — failing "
                    "closed rather than run one tenant's project with another "
                    "tenant's keys (worker-credential-org-trust-divergence-v1).",
                )
                self.queue.fail(job.id, self.worker_id, safe.text, retry=False)
                LOG.error(
                    "job_failed",
                    extra={
                        "event": "job_failed",
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                        "retry": False,
                        **_safe_error_fields(safe),
                    },
                    exc_info=False,
                )
                return
            handler_context = JobHandlerContext.from_claimed_job(
                trusted_org_id=trusted_org_id
            )
            stop_hb = threading.Event()
            hb = threading.Thread(
                target=self._heartbeat_loop, args=(job.id, stop_hb), daemon=True
            )
            hb.start()
            try:
                handler_payload = dict(job.payload or {})
                handler_payload.setdefault("job_id", job.id)
                # Attempt-finality from the claim the worker already holds:
                # attempts incremented at claim time, so this comparison names
                # THIS attempt. Handlers that must decide "terminalize domain
                # state or let the queue retry" (notification delivery) read
                # it here instead of re-reading the job row mid-handler, which
                # races recovery/requeue. Worker-written, never caller input.
                handler_payload["job_final_attempt"] = job.attempts >= job.max_attempts
                # Credentials resolve from the trusted queue column, never the
                # mutable JSON body. payload/column agreement was enforced
                # above; normalize the
                # handler's org_id to the trusted column value so the credential
                # seam (jobs/runs.resolve_run_keys) can never read a forged one.
                if trusted_org_id is None:
                    handler_payload.pop("org_id", None)
                else:
                    handler_payload["org_id"] = trusted_org_id
                storage_key = job.project_storage_key
                if storage_key is None:
                    handler_payload.pop(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY, None)
                else:
                    # This value is reconstructed from claimed queue columns,
                    # never trusted from the persisted caller-controlled payload.
                    handler_payload[CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY] = (
                        storage_key
                    )
                    handler_payload["project_id"] = storage_key.project_slug
                    handler_payload["storage_org_id"] = storage_key.storage_org_id
                result = handler(handler_payload, handler_context)
            except NonRetryableJobError as exc:
                # A handler's explicit "this will never succeed on retry"
                # signal: terminal-fail immediately, bypassing the attempts budget.
                stop_hb.set()
                hb.join(timeout=5)
                safe = _non_retryable_safe_error(exc)
                self.queue.fail(job.id, self.worker_id, safe.text, retry=False)
                LOG.error(
                    "job_failed",
                    extra={
                        "event": "job_failed",
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                        "retry": False,
                        **_safe_error_fields(safe),
                    },
                    exc_info=False,
                )
                return
            except Exception as exc:  # noqa: BLE001 — isolate job failures
                stop_hb.set()
                hb.join(timeout=5)
                retry_delay = self._backoff(job.attempts)
                safe = safe_error(
                    "worker_exception",
                    exc,
                    include_frames=True,
                )
                self.queue.fail(
                    job.id,
                    self.worker_id,
                    safe.text,
                    retry_delay_seconds=retry_delay,
                )
                LOG.error(
                    "job_failed",
                    extra={
                        "event": "job_failed",
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                        "retry": True,
                        "retry_delay_seconds": retry_delay,
                        **_safe_error_fields(safe),
                    },
                    exc_info=False,
                )
                return
            stop_hb.set()
            hb.join(timeout=5)
            completed = self.queue.complete(
                job.id,
                self.worker_id,
                result if isinstance(result, dict) else {"value": result},
            )
            if not completed:
                LOG.warning(
                    "job_finalize_rejected",
                    extra={
                        "event": "job_finalize_rejected",
                        "job_kind": job.kind,
                        "worker_id": self.worker_id,
                    },
                )
                return
            LOG.info(
                "job_completed",
                extra={
                    "event": "job_completed",
                    "job_kind": job.kind,
                    "worker_id": self.worker_id,
                },
            )

    def _heartbeat_loop(self, job_id: int, stop: threading.Event) -> None:
        interval = max(self.lease_seconds / 3.0, 0.05)
        while not stop.wait(interval):
            if not self.queue.heartbeat(
                job_id, self.worker_id, lease_seconds=self.lease_seconds
            ):
                return  # lost the lease — the job was recovered elsewhere
            # Worker-presence liveness:
            # record_liveness() is decoupled from the job lease on purpose
            # (status/health surfaces tell "no worker" apart from "worker
            # busy"), but until now it only ran at the top of run_once —
            # never while a handler was actually executing. A job running
            # longer than the admin liveness window (production default 90s)
            # made a perfectly busy worker look dead. record_liveness()
            # already throttles itself via self.liveness_interval, so this
            # is not a new thread and does not add unthrottled writes — it
            # just keeps the SAME presence row fresh during long jobs too.
            self.record_liveness()

    # ---------- the loop ----------

    def run_forever(
        self, stop: threading.Event | None = None, *, drain: bool = False
    ) -> None:
        """Poll until `stop` is set. drain=True exits at the first idle poll
        (tests, batch runs). The current job always finishes — `stop` is only
        checked between jobs; if the process is killed mid-job, the lease
        expires and another worker recovers it."""
        stop = stop or threading.Event()
        self.record_liveness(force=True)
        while not stop.is_set():
            worked = self.run_once()
            if not worked:
                if drain:
                    return
                stop.wait(self.poll_interval)
