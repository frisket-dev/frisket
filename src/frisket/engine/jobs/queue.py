"""Unified job queue.

ONE SQLAlchemy-backed queue class serves both backends off a single
``jobs_metadata`` definition; ``open_queue()`` selects the engine (a local
``<workspace>/.queue.db`` SQLite file, or the hosted run-queue database URL).
The only backend-specific behavior lives in two explicit, named seams:

- claim serialization — Postgres claims use ``SELECT ... FOR UPDATE SKIP
  LOCKED`` so concurrent workers skip each other's in-flight rows; SQLite
  claims run under ``BEGIN IMMEDIATE`` (write lock taken up front so two
  workers never select the same row) and return ``None`` on lock contention so
  the worker simply polls again;
- timestamp coercion — ``_parse`` restores UTC on the tz-naive strings SQLite
  round-trips.

Liveness is lease-based, not connection-based: a claim stamps
lease_expires_at; the worker heartbeats to extend it; `recover_expired()`
returns expired running jobs to the queue (or fails them once attempts are
exhausted), so a forcibly terminated worker's jobs are picked up by the next
one.
Retries are scheduled via available_at (the worker supplies exponential
backoff); attempts increments at claim time, so a crash mid-job still counts
against max_attempts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from filelock import FileLock

from frisket.project_identity import ProjectStorageKey
from frisket.redaction import redact_stored_error
from frisket.engine.worker_version import code_version

LOG = logging.getLogger("frisket.jobs.queue")

STATUSES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL = ("done", "failed", "cancelled")

QUEUE_DB_NAME = ".queue.db"  # invisible to Workspace's *.frisket glob
PROJECT_RUN_KIND = "project.run"
ACTION_RUN_KIND = "action.run"
MODEL_PULL_KIND = "model.pull"
CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY = "__frisket_claimed_project_storage_key"
# Server-owned jobs require an explicit marker; missing tenant identity fails closed.
SERVER_SCOPED_JOB_PAYLOAD_KEY = "server_scoped"
WORKSPACE_SCOPED_REF_KINDS = {"source.poll", "enclosure.download", MODEL_PULL_KIND}
COOPERATIVELY_CANCELLABLE_RUNNING_KINDS = frozenset({ACTION_RUN_KIND, MODEL_PULL_KIND})


def _requeue_budget_left(row) -> bool:
    """The ONE requeue predicate every AUTOMATIC requeue path shares
    (fail / recover_expired): a job may be requeued while
    ``attempts < max_attempts``."""
    return row.attempts < row.max_attempts


def claimed_project_root(
    claimed: ProjectStorageKey,
    *,
    workspace_root: str | Path,
    workspace_root_storage_org_id: int | None = None,
) -> Path:
    """The directory a claimed key resolves to under a declared root.

    THE three-way rule, in one place, for every caller that has a trusted
    ``ProjectStorageKey`` and a declared registration root — the payload-shaped
    ``claimed_project_location``, the queue terminalization hook, and the
    action-job recovery scan all route through here so a storage-routing fix
    cannot land in one and miss the others.

    An org-scoped root (``workspace_root_storage_org_id`` set) IS that org's
    directory, so a matching claim resolves directly under it (appending the
    org id would double-nest ``<org>/<org>/<slug>``) and a claim for any other
    org fails closed. An unscoped root is the global projects root, so the
    claim's org id is its per-org subdirectory.
    """
    if workspace_root_storage_org_id is None:
        return Path(workspace_root) / str(claimed.storage_org_id)
    if claimed.storage_org_id == workspace_root_storage_org_id:
        return Path(workspace_root)
    raise ValueError(
        f"claimed storage identity (org {claimed.storage_org_id}) does "
        "not belong to this registration's org-scoped storage root "
        f"(org {workspace_root_storage_org_id})"
    )


def claimed_project_location(
    payload: Mapping[str, object],
    *,
    workspace_root: str | Path,
    require_storage_identity: bool,
    workspace_root_storage_org_id: int | None = None,
) -> tuple[str, Path, Path]:
    """Return trusted slug, workspace directory, and bundle path for a handler.

    A claimed ``ProjectStorageKey`` always beats the mutable payload fields:
    identity (org + slug) comes from claimed queue columns and the directory
    comes from registration-time configuration, never from the payload's
    ``project_id``/``workspace_root``.

    ``workspace_root_storage_org_id`` is declared registration config (the
    queue-hosted-posture-explicit-v1 direction: posture is a parameter
    threaded from the composition root, never inferred from the path). It
    states that ``workspace_root`` IS the storage directory of exactly that
    org (the hosted per-org ``Workspace`` roots at
    ``FRISKET_DATA_DIR/projects/<org_id>``), so a matching claim resolves the
    slug directly under the root — appending the claim's org id there would
    double-nest to ``<org>/<org>/<slug>.frisket`` — and a claim for any OTHER
    org fails closed rather than resolving into the wrong tenant's directory.
    When it is ``None`` the root is the global, org-unscoped projects root
    (the shared worker CLI and local workspaces) and the claim's org id is
    appended as its per-org subdirectory.
    """
    claimed = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
    if isinstance(claimed, ProjectStorageKey):
        project_root = claimed_project_root(
            claimed,
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
        )
        return (
            claimed.project_slug,
            project_root,
            project_root / f"{claimed.project_slug}.frisket",
        )
    if require_storage_identity:
        raise ValueError("hosted queue handler requires claimed storage identity")
    project_id = _coerce_str(payload.get("project_id"))
    if project_id is None:
        raise ValueError("queue handler requires project_id")
    project_root = Path(str(payload.get("workspace_root") or workspace_root))
    return project_id, project_root, project_root / f"{project_id}.frisket"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    # fixed-width microseconds so ISO strings compare correctly in SQL
    return dt.isoformat(timespec="microseconds")


def _parse(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:  # sqlite loses tz (same caveat as hosted schema)
        ts = ts.replace(tzinfo=UTC)
    return ts


def _coerce_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


# Product code registers this hook, keeping the queue free of product imports.
TerminalizationHook = Callable[..., None]


def _job_refs(kind: str, payload: Mapping[str, object] | None) -> dict[str, object]:
    data = payload or {}
    spec = _mapping(data.get("spec"))
    # Normalize every supported source-reference spelling at enqueue time.
    trigger_ref = _mapping(data.get("trigger_ref"))
    return {
        "org_id": _coerce_str(data.get("org_id")),
        "storage_org_id": _coerce_int(data.get("storage_org_id")),
        "project_id": _coerce_str(data.get("project_id")),
        "run_id": _coerce_int(data.get("run_id")),
        "source_id": _coerce_int(
            data.get("source_id")
            or spec.get("source_id")
            or trigger_ref.get("source_id")
        ),
        "sheet_id": _coerce_int(data.get("sheet_id") or spec.get("sheet_id")),
        "row_id": _coerce_int(data.get("row_id") or spec.get("row_id")),
        "receipt_id": _coerce_str(data.get("v1_receipt_id") or data.get("receipt_id")),
        "action_kind": _coerce_str(
            data.get("action_kind") or spec.get("kind") or spec.get("action_kind")
        ),
        "trace_id": _coerce_str(data.get("trace_id")),
        "workspace_root": _coerce_str(data.get("workspace_root")),
        "dedupe_key": _coerce_str(data.get("dedupe_key")),
    }


def normalize_queue_org_id(value: object) -> int | None:
    """Coerce a queue org identity (the FUNDING account) to a positive int, or None.

    Absent/empty -> None (the local, unkeyed tier). Anything present must be a
    positive integer org identity; a value that cannot be one raises so a
    malformed identity fails closed rather than normalizing into some tenant's
    keys. Mirrors ProjectStorageKey's fail-closed identity validation and the
    ``int(org_id)`` the credential seam (jobs/runs.resolve_run_keys) performs.

    ONE definition, minted here and applied at BOTH ends of the job's life:
    ``enqueue`` validates the identity a caller claims, and ``Worker._execute``
    re-validates the persisted column before resolving credentials against it.
    It used to exist only at the claim end, so a malformed funding identity was
    accepted at enqueue and only terminal-failed a claim later, with the work
    silently lost in between.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"queue org_id must be an int or numeric str, got {value!r}")
    try:
        org_id = int(value)  # non-numeric str -> fail closed
    except ValueError as exc:
        # Name the knob: a bare "invalid literal for int()" says nothing about
        # which identity refused or where to fix it.
        raise ValueError(
            f"queue org_id must be an int or numeric str, got {value!r}"
        ) from exc
    if org_id < 1:
        raise ValueError(f"queue org_id must be a positive integer, got {org_id!r}")
    return org_id


def _validate_hosted_attribution(
    refs: Mapping[str, object], body: Mapping[str, object]
) -> None:
    """hosted-job-attribution-v1: on the hosted store, refuse a job that names
    no owner. Called only when the queue's DECLARED posture is ``hosted=True``
    (queue-hosted-posture-explicit-v1), so the local single-user tier and the
    self-hosted team server — both of which open the queue ``hosted=False`` and
    enqueue these same kinds with no org at all — never reach it.

    Two facts, deliberately not merged (they diverge on a hosted tenant
    sub-app, where ``org_id`` is the FUNDING account and ``storage_org_id`` is
    the DATA OWNER — see a downstream composition's tenant-app builder):

    * ``storage_org_id`` is the tenancy label. It is what makes a
      ``project_id`` sensitive — the org whose directory the bundle lives in —
      so it is what per-tenant surfaces filter on, and it is REQUIRED here.
    * ``org_id`` is who pays and whose BYOK keys resolve. Background work
      minted by the engine (source polls, watch evaluations, digests,
      deliveries) legitimately has no funding account in scope, so it is
      OPTIONAL — but a value that IS claimed must be a well-formed identity.

    A job with no ``project_id`` is not thereby exempt: it must either carry a
    ``storage_org_id`` (tenant-scoped work that happens not to name a bundle)
    or declare ``server_scoped`` (work owned by the workspace itself).
    """
    server_scoped = body.get(SERVER_SCOPED_JOB_PAYLOAD_KEY) is True
    if refs["project_id"] is not None:
        if server_scoped:
            raise ValueError(
                "a job cannot be both server-scoped and project-scoped: "
                f"{SERVER_SCOPED_JOB_PAYLOAD_KEY}=True was set alongside "
                f"project_id={refs['project_id']!r}"
            )
        raw_storage_org_id = body.get("storage_org_id")
        if raw_storage_org_id is None:
            raise ValueError(
                "hosted project jobs require first-class storage_org_id identity"
            )
        # Validate the raw payload value, not a coerced one: ProjectStorageKey
        # rejects bool/float/str fail-closed so a malformed identity can never
        # normalize into tenant 1 or persist a row a worker can't route safely.
        ProjectStorageKey(
            storage_org_id=raw_storage_org_id,
            project_slug=str(refs["project_id"]),
        )
    elif refs["storage_org_id"] is None and not server_scoped:
        raise ValueError(
            "hosted jobs require an owner: carry a storage_org_id, or set "
            f"{SERVER_SCOPED_JOB_PAYLOAD_KEY}=True to declare this job owned "
            "by the server/workspace rather than by a tenant"
        )
    # Whatever funding account the caller claims is validated at the mint site,
    # so a malformed one is refused here rather than terminal-failing a claim.
    normalize_queue_org_id(body.get("org_id"))


def _validate_ref_lookup(
    kind: str,
    *,
    statuses: tuple[str, ...],
    project_id: str | None,
    storage_org_id: int | None,
    source_id: int | None,
    sheet_id: int | None,
    row_id: int | None,
    workspace_root: str | None,
    dedupe_key: str | None = None,
) -> None:
    if not statuses:
        raise ValueError("find_job_by_refs requires at least one status")
    if (
        source_id is not None
        or sheet_id is not None
        or row_id is not None
        or dedupe_key is not None
    ) and project_id is None:
        raise ValueError("find_job_by_refs requires project_id for scoped refs")
    if storage_org_id is not None and project_id is None:
        raise ValueError(
            "find_job_by_refs requires project_id for storage identity scope"
        )
    if kind in WORKSPACE_SCOPED_REF_KINDS and workspace_root is None:
        raise ValueError(f"find_job_by_refs requires workspace_root for {kind} lookups")


@dataclass
class WorkerHeartbeat:
    worker_id: str
    queue: str | None
    first_seen_at: datetime | None
    last_heartbeat_at: datetime | None
    worker_version: str | None = None
    kinds: str | None = None


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    locked_by: str | None
    locked_at: datetime | None
    lease_expires_at: datetime | None
    available_at: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict | None
    error: str | None
    org_id: str | None = None
    storage_org_id: int | None = None
    project_id: str | None = None
    run_id: int | None = None
    source_id: int | None = None
    sheet_id: int | None = None
    row_id: int | None = None
    receipt_id: str | None = None
    action_kind: str | None = None
    trace_id: str | None = None
    workspace_root: str | None = None
    dedupe_key: str | None = None
    # Version fields are diagnostic/legacy metadata only; never gate claims.
    code_version: str | None = None
    claimed_code_version: str | None = None
    version_mismatch_requeues: int = 0
    # Lease recovery may create overlapping claims; acknowledge this exact token.
    handler_authority_id: str | None = None

    @property
    def project_storage_key(self) -> ProjectStorageKey | None:
        """Trusted physical project identity carried by queue columns."""
        if self.storage_org_id is None or self.project_id is None:
            return None
        return ProjectStorageKey(
            storage_org_id=self.storage_org_id,
            project_slug=self.project_id,
        )


@dataclass(frozen=True)
class CancelResult:
    """Result of :meth:`JobQueue.cancel_with_report`.

    ``previous_status`` is the job's status AT THE MOMENT the cancel
    transaction observed it (``None`` when nothing was cancelled — missing
    job or a status the cancel filter refuses). ``previous_status ==
    "queued"`` means the job had never been claimed — no handler ever ran or
    will run for it.
    """

    cancelled: bool
    previous_status: str | None


@dataclass(frozen=True)
class HandlerAuthorityAbandonResult:
    """Atomic stopped-worker authority recovery result.

    ``live_worker`` means the named worker had a heartbeat at or after the
    caller's cutoff, so no authority was removed.
    """

    abandoned_authorities: int
    live_worker: bool


@dataclass(frozen=True)
class UnheardHandlerAuthorities:
    """Handler authorities whose holding worker has gone quiet — a FACT, not a
    verdict.

    Releasing an authority requires proof the handler code stopped, which no
    timeout can supply (see
    :meth:`JobQueue.abandon_stopped_worker_handler_authorities`), so these
    rows are deliberately left alone. But an authority held by a worker that
    stopped heartbeating is also the exact shape of a project deletion that
    will never finish, and until something publishes the count nobody learns
    that except the user whose deletion sat at "pending".

    ``oldest_claim_age_seconds`` is what separates a five-second worker
    restart from a six-day wedge; without it a bare count is unactionable.
    """

    count: int
    oldest_claim_age_seconds: float | None


class JobQueue(ABC):
    """The interface the worker loop runs against — identical everywhere."""

    @abstractmethod
    def enqueue(
        self,
        kind: str,
        payload: dict | None = None,
        *,
        max_attempts: int = 3,
        delay_seconds: float = 0.0,
    ) -> int:
        """Add a job; returns its id. delay_seconds defers availability."""

    @abstractmethod
    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 60.0,
        worker_version: str | None = None,
    ) -> Job | None:
        """Atomically take the oldest available queued job (or None). Marks
        it running, increments ``attempts``, stamps the lease. ``worker_version``
        is diagnostic metadata supplied by a worker; direct queue users retain
        the queue process's current identity by default."""

    @abstractmethod
    def acknowledge_handler_exit(
        self,
        job_id: int,
        worker_id: str,
        *,
        authority_id: str | None = None,
    ) -> bool:
        """Release a claim's durable handler authority after code has exited.

        ``authority_id`` identifies one exact claim. It may be omitted only by
        direct queue executors that use ``complete``/``fail`` themselves; the
        product Worker always supplies the token returned by ``claim``.
        """

    @abstractmethod
    def has_active_project_handler(
        self,
        project_id: str,
        *,
        storage_org_id: int | None,
    ) -> bool:
        """Whether executable code still owns this exact project key.

        This is deliberately independent of the job row's status and lease.
        """

    @abstractmethod
    def abandon_stopped_worker_handler_authorities(
        self,
        worker_id: str,
        *,
        heartbeat_cutoff: datetime,
    ) -> HandlerAuthorityAbandonResult:
        """Atomically refuse a live worker or release a stopped one's claims.

        The caller must establish that the named worker process cannot still
        execute. Heartbeat age is contradictory evidence, not proof of death:
        a heartbeat at/after ``heartbeat_cutoff`` refuses without deleting.
        The heartbeat check and worker-scoped delete share one write
        transaction, so a concurrent heartbeat cannot slip between them.
        """

    @abstractmethod
    def heartbeat(
        self, job_id: int, worker_id: str, *, lease_seconds: float = 60.0
    ) -> bool:
        """Extend the lease on a job this worker holds. False = lost it
        (lease expired and someone recovered the job) — abandon the work."""

    @abstractmethod
    def complete(self, job_id: int, worker_id: str, result: dict | None = None) -> bool:
        """Mark done. False if this worker no longer holds the job."""

    @abstractmethod
    def fail(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        *,
        retry: bool = True,
        retry_delay_seconds: float = 0.0,
    ) -> bool:
        """Record a failure. Requeues with available_at = now + delay while
        attempts < max_attempts (and retry=True); otherwise terminal-fails."""

    @abstractmethod
    def cancel(self, job_id: int) -> bool:
        """Cancel a queued job.

        Jobs of a kind in ``COOPERATIVELY_CANCELLABLE_RUNNING_KINDS``
        (``action.run``, ``model.pull``) may also be marked cancelled while
        running because their handlers poll the durable job row between
        external calls. ``action.run`` additionally terminalizes a paired v1
        receipt; other cooperatively-cancellable kinds have no receipt to
        terminalize and just stop at the row flip.
        """

    @abstractmethod
    def cancel_queued(self, job_id: int) -> bool:
        """Cancel ``job_id`` if queued, including a recovered prior claim.

        A concurrent claim wins by changing the row to ``running``. Lifecycle
        deletion, which must also distinguish never-started work from a lease-
        recovered handler, uses :meth:`cancel_unstarted` instead.
        """

    @abstractmethod
    def cancel_unstarted(self, job_id: int) -> bool:
        """Cancel only while queued and never claimed, checked atomically."""

    @abstractmethod
    def cancel_with_report(self, job_id: int) -> "CancelResult":
        """Same cancellation as :meth:`cancel`, plus the pre-cancel status. A
        caller that
        needs to know whether the job had EVER been claimed (still
        ``queued``, so no handler will ever run for it) reads
        ``previous_status`` atomically from the SAME transaction that
        performs the cancel -- never a separate get-then-cancel read, which
        would race a concurrent claim. The model.pull cancel route uses this
        to finalize a never-claimed pull's row directly, since its handler
        will never get a chance to."""

    @abstractmethod
    def recover_expired(self) -> int:
        """Return lease-expired running jobs to queued (or failed when
        attempts are exhausted). Returns how many were recovered."""

    @abstractmethod
    def fail_queued(self, job_id: int, error: str) -> bool:
        """Terminal-fail a job that is still ``queued`` (never claimed).

        The lease-based ``fail``/``recover_expired`` path only touches jobs a
        worker holds; a job that no worker ever claimed hangs in ``queued``
        forever. This lets the status/timeout path fail it loudly. Returns
        False if the job is not queued."""

    @abstractmethod
    def record_worker_heartbeat(
        self,
        worker_id: str,
        *,
        queue: str | None = None,
        worker_version: str | None = None,
        kinds: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Upsert this worker's liveness marker (decoupled from job leases).
        ``worker_version`` records the heartbeating worker's own code identity
        for audit surfaces. ``kinds`` is a comma-joined,
        sorted list of this worker's registered handler kinds."""

    @abstractmethod
    def count_live_workers(
        self, *, within_seconds: float, now: datetime | None = None
    ) -> int:
        """How many workers heartbeated within the window ending at ``now``."""

    @abstractmethod
    def list_worker_heartbeats(self) -> list["WorkerHeartbeat"]:
        """All known worker heartbeats, most-recently-seen first."""

    @abstractmethod
    def unheard_handler_authorities(
        self, *, heartbeat_cutoff: datetime, now: datetime
    ) -> "UnheardHandlerAuthorities":
        """Authorities held by workers with no heartbeat at/after the cutoff.

        A pure read: it releases nothing and changes no behavior. Its only
        job is to make an otherwise invisible condition publishable, because
        the automatic recovery paths cannot touch these rows and the operator
        lever that can is never pulled by someone who was never told.

        The liveness predicate is deliberately the SAME one
        ``abandon_stopped_worker_handler_authorities`` uses to refuse a live
        worker — one spelling of "this worker has gone quiet", so the surface
        that reports the condition and the lever that resolves it cannot
        disagree about which workers they mean. A worker with NO heartbeat
        row at all counts: an absent fact is not evidence of liveness.
        """

    @abstractmethod
    def oldest_queued_created_at(self) -> datetime | None:
        """Oldest ``created_at`` across every queued job, or ``None``.

        This is an aggregate health query, not a bounded job listing: queue
        saturation must never hide the longest-waiting row.
        """

    @abstractmethod
    def get_scoped(self, job_id: int, *, org_id: str, kind: str) -> Job | None:
        """Same as :meth:`get`, but the WHERE clause additionally requires
        ``org_id`` AND ``kind`` to match exactly: a caller-scoped org/kind
        read, never fetch-then-
        authorize. Returns ``None`` for a wrong org, a wrong kind, or a
        missing job -- all three look identical to the caller, which is the
        point (no oracle for "the job exists but isn't yours")."""

    @abstractmethod
    def cancel_scoped(self, job_id: int, *, org_id: str, kind: str) -> bool:
        """Same as :meth:`cancel`, but the WHERE clause additionally requires
        ``org_id`` AND ``kind`` to match exactly -- an atomic, single
        transaction gate rather than a separate authorization read followed
        by a plain ``cancel()``. A wrong org or wrong kind refuses (``False``)
        exactly like a missing or already-terminal job."""

    @abstractmethod
    def get(self, job_id: int) -> Job | None: ...

    @abstractmethod
    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Job]: ...

    @abstractmethod
    def get_project_run_job(
        self,
        project_id: str,
        run_id: int,
        *,
        storage_org_id: int | None = None,
    ) -> Job | None: ...

    @abstractmethod
    def list_project_jobs(
        self,
        project_id: str,
        *,
        storage_org_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        kind: str | None = None,
        source_id: int | None = None,
    ) -> list[Job]:
        """Newest-first project jobs, narrowed in SQL.

        ``source_id`` matches the ``jobs.source_id`` column minted by
        ``_job_refs``. Narrowing here rather than in the caller is what makes
        ``limit`` a budget for the rows the caller actually wants: a quiet
        source beside a busy one used to have its jobs pushed out of the
        window by traffic the caller then discarded in Python.
        """

    @abstractmethod
    def find_job_by_refs(
        self,
        kind: str,
        *,
        statuses: tuple[str, ...] = ("queued", "running"),
        project_id: str | None = None,
        storage_org_id: int | None = None,
        source_id: int | None = None,
        sheet_id: int | None = None,
        row_id: int | None = None,
        workspace_root: str | None = None,
        dedupe_key: str | None = None,
    ) -> Job | None: ...

    @abstractmethod
    def counts(self) -> dict[str, int]:
        """status -> count, for ops/status endpoints."""

    def close(self) -> None:  # noqa: B027 — optional hook
        pass


def _queue_engine_url(locator: str | Path) -> str:
    """Map a queue locator to a SQLAlchemy URL.

    A ``Path`` (or a bare string with no ``://`` scheme) is a local SQLite
    ``.queue.db`` file. Anything with a URL scheme is a database URL (the
    hosted run queue).
    """
    if isinstance(locator, Path) or (isinstance(locator, str) and "://" not in locator):
        path = Path(locator)
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    return str(locator)


_SQLITE_BEGIN_IMMEDIATE_OPT = "frisket_sqlite_begin_immediate"

_SQLITE_INIT_LOCKS: dict[str, threading.Lock] = {}
_SQLITE_INIT_LOCKS_GUARD = threading.Lock()


def _sqlite_memory_engine(engine: sa.engine.Engine) -> bool:
    """Whether *engine* targets a private SQLite in-memory database."""
    database = engine.url.database
    return database in (None, "", ":memory:")


@contextmanager
def _sqlite_initialization_lock(engine: sa.engine.Engine):
    """Serialize first WAL negotiation and schema setup per SQLite database.

    The thread lock closes the race between engines in this process; the lock
    file extends that serialization to separate worker processes.  In-memory
    engines have no shared database and deliberately skip this path.
    """
    if _sqlite_memory_engine(engine):
        yield
        return
    database = engine.url.database
    if not database:
        yield
        return
    path = Path(database).expanduser().resolve()
    key = str(path)
    with _SQLITE_INIT_LOCKS_GUARD:
        thread_lock = _SQLITE_INIT_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        lock_path = Path(f"{path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=-1):
            yield


def _negotiate_sqlite_wal(engine: sa.engine.Engine) -> None:
    """Set WAL once while the per-database initialization lock is held."""
    if _sqlite_memory_engine(engine):
        return
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
    finally:
        connection.close()


def _install_sqlite_begin_immediate(engine: sa.engine.Engine) -> None:
    """Claim-serialization seam for SQLite.

    pysqlite's implicit transaction management is disabled (isolation_level =
    None) so SQLAlchemy owns every ``BEGIN``. The ``claim`` transaction opts in
    to ``BEGIN IMMEDIATE`` (the ``frisket_sqlite_begin_immediate`` execution
    option), taking the write lock up front exactly as the local workspace
    queue always has, so two workers can never select the same queued row.
    Every other transaction takes a plain deferred ``BEGIN`` — reads must NOT
    acquire the write lock, or pooled connections would deadlock each other.
    ``busy_timeout`` absorbs brief contention; only a wait past it surfaces as
    an ``OperationalError`` that ``claim()`` turns back into a poll (``None``).
    File-backed ``journal_mode=WAL`` is negotiated once by the constructor
    under ``_sqlite_initialization_lock``; doing it here on every connection
    reintroduces a cold-boot race.  In-memory engines intentionally skip WAL.
    """

    @sa.event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _record):  # pragma: no cover - trivial
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    @sa.event.listens_for(engine, "begin")
    def _begin(connection):  # pragma: no cover - trivial
        if connection.get_execution_options().get(_SQLITE_BEGIN_IMMEDIATE_OPT):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.exec_driver_sql("BEGIN")


jobs_metadata = sa.MetaData()

jobs_table = sa.Table(
    "jobs",
    jobs_metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("payload", sa.Text, nullable=False, default="{}", server_default="{}"),
    sa.Column("org_id", sa.String),
    sa.Column("storage_org_id", sa.Integer),
    sa.Column("project_id", sa.String),
    sa.Column("run_id", sa.Integer),
    sa.Column("source_id", sa.Integer),
    sa.Column("sheet_id", sa.Integer),
    sa.Column("row_id", sa.Integer),
    sa.Column("receipt_id", sa.String),
    sa.Column("action_kind", sa.String),
    sa.Column("trace_id", sa.String),
    sa.Column("workspace_root", sa.String),
    sa.Column("dedupe_key", sa.String),
    sa.Column("code_version", sa.String),
    sa.Column("claimed_code_version", sa.String),
    sa.Column(
        "version_mismatch_requeues",
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "status", sa.String, nullable=False, default="queued", server_default="queued"
    ),
    sa.Column("attempts", sa.Integer, nullable=False, default=0, server_default="0"),
    sa.Column(
        "max_attempts", sa.Integer, nullable=False, default=3, server_default="3"
    ),
    sa.Column("locked_by", sa.String),
    sa.Column("locked_at", sa.DateTime(timezone=True)),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("result", sa.Text),
    sa.Column("error", sa.Text),
    sa.Index("idx_jobs_claim", "status", "available_at", "id"),
    sa.Index("idx_jobs_lease", "status", "lease_expires_at"),
    sa.Index("idx_jobs_project_run", "kind", "project_id", "run_id"),
    sa.Index("idx_jobs_project_status_id", "project_id", "status", sa.desc("id")),
    sa.Index(
        "idx_jobs_storage_project_status_id",
        "storage_org_id",
        "project_id",
        "status",
        sa.desc("id"),
    ),
    sa.Index(
        "idx_jobs_source_status_id", "project_id", "source_id", "status", sa.desc("id")
    ),
    sa.Index(
        "idx_jobs_source_pending",
        "kind",
        "project_id",
        "source_id",
        "workspace_root",
        "status",
        sa.desc("id"),
    ),
    sa.Index(
        "idx_jobs_row_pending",
        "kind",
        "project_id",
        "sheet_id",
        "row_id",
        "workspace_root",
        "status",
        sa.desc("id"),
    ),
    sa.Index("idx_jobs_receipt", "receipt_id"),
    sa.Index("idx_jobs_dedupe", "kind", "project_id", "dedupe_key", "status"),
)

# ``-1`` groups unscoped rows because NULL index keys are distinct; real org
# ids are positive. Storage scope prevents cross-tenant dedupe collisions.
sa.Index(
    "uq_jobs_active_dedupe",
    jobs_table.c.kind,
    sa.func.coalesce(jobs_table.c.storage_org_id, -1),
    jobs_table.c.project_id,
    jobs_table.c.dedupe_key,
    unique=True,
    sqlite_where=sa.text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
    postgresql_where=sa.text(
        "status IN ('queued', 'running') AND dedupe_key IS NOT NULL"
    ),
)

worker_heartbeats_table = sa.Table(
    "worker_heartbeats",
    jobs_metadata,
    sa.Column("worker_id", sa.String, primary_key=True),
    sa.Column("queue", sa.String),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("worker_version", sa.String),
    sa.Column("kinds", sa.String),
    sa.Index("idx_worker_hb_seen", "last_heartbeat_at"),
)

# Lease recovery can leave multiple independently authoritative handlers.
job_handler_authorities_table = sa.Table(
    "job_handler_authorities",
    jobs_metadata,
    sa.Column("authority_id", sa.String, primary_key=True),
    sa.Column("job_id", sa.Integer, nullable=False),
    sa.Column("worker_id", sa.String, nullable=False),
    sa.Column("storage_org_id", sa.Integer),
    sa.Column("project_id", sa.String),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "idx_job_handler_authorities_project",
        "storage_org_id",
        "project_id",
    ),
    sa.Index("idx_job_handler_authorities_worker", "worker_id"),
)


class SqlAlchemyJobQueue(JobQueue):
    """The one SQLAlchemy-backed queue serving both backends.

    ``locator`` is either a SQLite file path (a ``Path``, or a bare string with
    no URL scheme — the local ``<workspace>/.queue.db``) or a database URL (the
    hosted run queue). Two named dialect seams and nothing else:

    - claim serialization — Postgres uses ``FOR UPDATE SKIP LOCKED``; SQLite
      takes ``BEGIN IMMEDIATE`` up front and returns ``None`` on lock
      contention so the worker polls again (preserving the local semantics);
    - timestamp coercion — ``_parse`` restores UTC on SQLite's tz-naive strings.
    """

    def __init__(
        self,
        locator: str | Path,
        *,
        schema_mode: str = "initialize",
        hosted: bool = False,
        clock: Callable[[], datetime] | None = None,
    ):
        url = _queue_engine_url(locator)
        self._clock: Callable[[], datetime] = clock or _now
        self._terminalization_hook: TerminalizationHook | None = None
        # Storage posture is explicit and independently testable from dialect.
        self._hosted_storage = hosted
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = sa.create_engine(url, future=True, connect_args=connect_args)
        self._is_sqlite = self.engine.dialect.name == "sqlite"
        if self._is_sqlite:
            _install_sqlite_begin_immediate(self.engine)
        if schema_mode == "initialize":
            from frisket.engine.jobs.queue_migrations import apply_pending_migrations

            if self._is_sqlite:
                with _sqlite_initialization_lock(self.engine):
                    _negotiate_sqlite_wal(self.engine)
                    with self._write_transaction() as cx:
                        apply_pending_migrations(cx)
            else:
                with self._write_transaction() as cx:
                    apply_pending_migrations(cx)
        elif schema_mode == "strict":
            from frisket.engine.jobs.queue_migrations import validate_queue_schema

            try:
                validate_queue_schema(self.engine)
            except BaseException:
                self.engine.dispose()
                raise
        else:
            self.engine.dispose()
            raise ValueError("queue schema_mode must be 'initialize' or 'strict'")

    def set_terminalization_hook(self, hook: TerminalizationHook | None) -> None:
        """Register the product-side terminalization hook (see the
        ``TerminalizationHook`` note above). Registered by the executor at
        open/worker init; the queue never imports the product side."""
        self._terminalization_hook = hook

    def _notify_terminal(self, event: str, job: Job | None) -> None:
        if job is None or self._terminalization_hook is None:
            return
        self._terminalization_hook(event, job, hosted=self._hosted_storage)

    @staticmethod
    def _job(row) -> Job:
        return Job(
            id=row.id,
            kind=row.kind,
            payload=json.loads(row.payload),
            status=row.status,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            locked_by=row.locked_by,
            locked_at=_parse(row.locked_at),
            lease_expires_at=_parse(row.lease_expires_at),
            available_at=_parse(row.available_at),
            created_at=_parse(row.created_at),
            started_at=_parse(row.started_at),
            finished_at=_parse(row.finished_at),
            result=json.loads(row.result) if row.result else None,
            error=row.error,
            org_id=row.org_id,
            storage_org_id=row.storage_org_id,
            project_id=row.project_id,
            run_id=row.run_id,
            source_id=row.source_id,
            sheet_id=row.sheet_id,
            row_id=row.row_id,
            receipt_id=row.receipt_id,
            action_kind=row.action_kind,
            trace_id=row.trace_id,
            workspace_root=row.workspace_root,
            dedupe_key=row.dedupe_key,
            code_version=row.code_version,
            claimed_code_version=row.claimed_code_version,
            version_mismatch_requeues=row.version_mismatch_requeues or 0,
        )

    def enqueue(
        self,
        kind,
        payload=None,
        *,
        max_attempts=3,
        delay_seconds=0.0,
    ):
        now = self._clock()
        body = payload or {}
        refs = _job_refs(kind, body)
        if self._hosted_storage:
            _validate_hosted_attribution(refs, body)
        try:
            with self._write_transaction() as cx:
                return cx.execute(
                    jobs_table.insert().values(
                        kind=kind,
                        payload=json.dumps(body),
                        org_id=refs["org_id"],
                        storage_org_id=refs["storage_org_id"],
                        project_id=refs["project_id"],
                        run_id=refs["run_id"],
                        source_id=refs["source_id"],
                        sheet_id=refs["sheet_id"],
                        row_id=refs["row_id"],
                        receipt_id=refs["receipt_id"],
                        action_kind=refs["action_kind"],
                        trace_id=refs["trace_id"],
                        workspace_root=refs["workspace_root"],
                        dedupe_key=refs["dedupe_key"],
                        code_version=code_version(),
                        status="queued",
                        attempts=0,
                        max_attempts=max_attempts,
                        available_at=now + timedelta(seconds=delay_seconds),
                        created_at=now,
                    )
                ).inserted_primary_key[0]
        except sa.exc.IntegrityError:
            # Return the concurrent winner of the active-job dedupe race.
            if refs["dedupe_key"] is None or refs["project_id"] is None:
                raise
            existing = self.find_job_by_refs(
                kind,
                statuses=("queued", "running"),
                project_id=str(refs["project_id"]),
                storage_org_id=refs["storage_org_id"],
                workspace_root=(
                    str(refs["workspace_root"])
                    if kind in WORKSPACE_SCOPED_REF_KINDS
                    and refs["workspace_root"] is not None
                    else None
                ),
                dedupe_key=str(refs["dedupe_key"]),
            )
            if existing is None:
                raise
            return existing.id

    def claim(self, worker_id, *, lease_seconds=60.0, worker_version=None):
        now = self._clock()
        authority_id = uuid.uuid4().hex
        c = jobs_table.c
        claim_filters = [c.status == "queued", c.available_at <= now]
        if self._hosted_storage:
            # Hosted project jobs without physical identity remain unclaimable.
            claim_filters.append(
                sa.or_(c.project_id.is_(None), c.storage_org_id.isnot(None))
            )
        # Postgres uses row locks; SQLite serializes claims with BEGIN IMMEDIATE.
        select_next = sa.select(c.id).where(*claim_filters).order_by(c.id).limit(1)
        if not self._is_sqlite:
            select_next = select_next.with_for_update(skip_locked=True)
        try:
            with self._write_transaction() as cx:
                jid = cx.execute(select_next).scalar_one_or_none()
                if jid is None:
                    return None
                cx.execute(
                    jobs_table.update()
                    .where(c.id == jid)
                    .values(
                        status="running",
                        attempts=c.attempts + 1,
                        locked_by=worker_id,
                        locked_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        started_at=sa.func.coalesce(c.started_at, now),
                        claimed_code_version=worker_version or code_version(),
                    )
                )
                row = cx.execute(sa.select(jobs_table).where(c.id == jid)).first()
                if row is None:
                    raise RuntimeError("claimed job disappeared")
                cx.execute(
                    job_handler_authorities_table.insert().values(
                        authority_id=authority_id,
                        job_id=jid,
                        worker_id=worker_id,
                        storage_org_id=row.storage_org_id,
                        project_id=row.project_id,
                        claimed_at=now,
                    )
                )
        except sa.exc.OperationalError:
            if self._is_sqlite:
                return None  # lock contention past busy_timeout — poll again
            raise
        job = self._job(row)
        job.handler_authority_id = authority_id
        return job

    def acknowledge_handler_exit(
        self,
        job_id,
        worker_id,
        *,
        authority_id=None,
    ):
        with self._write_transaction() as cx:
            released = self._acknowledge_handler_exit_on_connection(
                cx,
                job_id=job_id,
                worker_id=worker_id,
                authority_id=authority_id,
            )
        return released

    @staticmethod
    def _acknowledge_handler_exit_on_connection(
        connection,
        *,
        job_id,
        worker_id,
        authority_id=None,
    ):
        c = job_handler_authorities_table.c
        filters = [c.job_id == job_id, c.worker_id == worker_id]
        if authority_id is None:
            candidates = (
                connection.execute(sa.select(c.authority_id).where(*filters).limit(2))
                .scalars()
                .all()
            )
            # Tokenless acknowledgement is safe only for one exact authority.
            if len(candidates) != 1:
                return False
            authority_id = candidates[0]
        filters.append(c.authority_id == authority_id)
        result = connection.execute(
            job_handler_authorities_table.delete().where(*filters)
        )
        return result.rowcount > 0

    def has_active_project_handler(self, project_id, *, storage_org_id):
        c = job_handler_authorities_table.c
        with self.engine.connect() as cx:
            return (
                cx.execute(
                    sa.select(c.authority_id)
                    .where(
                        c.storage_org_id == storage_org_id,
                        c.project_id == project_id,
                    )
                    .limit(1)
                ).first()
                is not None
            )

    def abandon_stopped_worker_handler_authorities(
        self,
        worker_id,
        *,
        heartbeat_cutoff,
    ):
        c = job_handler_authorities_table.c
        heartbeat = worker_heartbeats_table.c
        with self._write_transaction() as cx:
            if not self._is_sqlite:
                # Lock the table to fence a concurrent first-heartbeat insert.
                cx.execute(
                    sa.text("LOCK TABLE worker_heartbeats IN SHARE ROW EXCLUSIVE MODE")
                )
            last_heartbeat_at = cx.execute(
                sa.select(heartbeat.last_heartbeat_at).where(
                    heartbeat.worker_id == worker_id
                )
            ).scalar_one_or_none()
            parsed_heartbeat = _parse(last_heartbeat_at)
            cutoff = _parse(heartbeat_cutoff)
            assert cutoff is not None
            if parsed_heartbeat is not None and parsed_heartbeat >= cutoff:
                return HandlerAuthorityAbandonResult(
                    abandoned_authorities=0,
                    live_worker=True,
                )
            result = cx.execute(
                job_handler_authorities_table.delete().where(c.worker_id == worker_id)
            )
        return HandlerAuthorityAbandonResult(
            abandoned_authorities=int(result.rowcount or 0),
            live_worker=False,
        )

    @contextmanager
    def _write_transaction(self):
        """Every read-then-write transaction runs here. On SQLite it opts in to
        ``BEGIN IMMEDIATE`` — the write lock is taken up front so a select
        followed by an update never deadlocks two workers on a shared→reserved
        lock upgrade, and the claim seam's exactly-once fencing holds. On
        Postgres it is an ordinary transaction (row locks do the fencing).
        Read-only methods use ``engine.connect()`` and stay deferred."""
        if self._is_sqlite:
            with self.engine.connect() as conn:
                conn = conn.execution_options(**{_SQLITE_BEGIN_IMMEDIATE_OPT: True})
                with conn.begin():
                    yield conn
        else:
            with self.engine.begin() as conn:
                yield conn

    def heartbeat(self, job_id, worker_id, *, lease_seconds=60.0):
        c = jobs_table.c
        with self._write_transaction() as cx:
            res = cx.execute(
                jobs_table.update()
                .where(c.id == job_id, c.locked_by == worker_id, c.status == "running")
                .values(
                    lease_expires_at=self._clock() + timedelta(seconds=lease_seconds)
                )
            )
        return res.rowcount > 0

    def complete(self, job_id, worker_id, result=None):
        c = jobs_table.c
        now = self._clock()
        with self._write_transaction() as cx:
            row = cx.execute(
                sa.select(jobs_table)
                .where(c.id == job_id, c.locked_by == worker_id, c.status == "running")
                .with_for_update()
            ).first()
            if row is None:
                # A cooperative cancel or lease recovery can reject the row
                # finalize even though this call is the old handler's explicit
                # exit acknowledgement.
                self._acknowledge_handler_exit_on_connection(
                    cx,
                    job_id=job_id,
                    worker_id=worker_id,
                )
                return False
            res = cx.execute(
                jobs_table.update()
                .where(
                    c.id == job_id,
                    c.locked_by == worker_id,
                    c.status == "running",
                )
                .values(
                    status="done",
                    result=json.dumps(result or {}),
                    error=None,
                    finished_at=now,
                    locked_by=None,
                    lease_expires_at=None,
                )
            )
            completed = res.rowcount > 0
            if completed:
                self._acknowledge_handler_exit_on_connection(
                    cx,
                    job_id=job_id,
                    worker_id=worker_id,
                )
            return completed

    def fail(self, job_id, worker_id, error, *, retry=True, retry_delay_seconds=0.0):
        stored_error = redact_stored_error(
            error,
            fallback_code="queue_job_failed",
        )
        if stored_error is None:
            stored_error = "queue_job_failed: operation failed"
        now = self._clock()
        c = jobs_table.c
        terminal_job: Job | None = None
        with self._write_transaction() as cx:
            row = cx.execute(
                sa.select(jobs_table)
                .where(c.id == job_id, c.locked_by == worker_id, c.status == "running")
                .with_for_update()
            ).first()
            if row is None:
                self._acknowledge_handler_exit_on_connection(
                    cx,
                    job_id=job_id,
                    worker_id=worker_id,
                )
                return False
            if retry and _requeue_budget_left(row):
                values = {
                    "status": "queued",
                    "error": stored_error,
                    "available_at": now + timedelta(seconds=retry_delay_seconds),
                    "locked_by": None,
                    "lease_expires_at": None,
                }
            else:
                values = {
                    "status": "failed",
                    "error": stored_error,
                    "finished_at": now,
                    "locked_by": None,
                    "lease_expires_at": None,
                }
                terminal_job = self._job(row)
            cx.execute(jobs_table.update().where(c.id == job_id).values(**values))
        self._notify_terminal("retry_exhausted", terminal_job)
        self.acknowledge_handler_exit(job_id, worker_id)
        return True

    def _cancel_impl(
        self, job_id: int, *, org_id: str | None = None, kind: str | None = None
    ) -> tuple[bool, str | None]:
        """Shared cancel implementation behind ``cancel``/``cancel_scoped``/
        ``cancel_with_report``: one atomic read-then-write transaction that
        also reports the job's PRE-cancel status — never a separate get()
        before the cancel,
        which would race a concurrent claim(). Returns
        ``(cancelled, previous_status)``; ``previous_status`` is ``None``
        when nothing matched (missing job, or a status the filter refuses)."""
        c = jobs_table.c
        scope_filters = [c.id == job_id]
        if org_id is not None:
            scope_filters.append(c.org_id == org_id)
        if kind is not None:
            scope_filters.append(c.kind == kind)
        cancellable = sa.or_(
            c.status == "queued",
            sa.and_(
                c.kind.in_(COOPERATIVELY_CANCELLABLE_RUNNING_KINDS),
                c.status == "running",
            ),
        )
        job_to_cancel: Job | None = None
        previous_status: str | None = None
        with self._write_transaction() as cx:
            row = cx.execute(
                sa.select(jobs_table)
                .where(*scope_filters, cancellable)
                .with_for_update()
            ).first()
            if row is None:
                return False, None
            job_to_cancel = self._job(row)
            previous_status = row.status
            res = cx.execute(
                jobs_table.update()
                .where(*scope_filters, cancellable)
                .values(
                    status="cancelled",
                    finished_at=self._clock(),
                    locked_by=None,
                    lease_expires_at=None,
                )
            )
            cancelled = res.rowcount > 0
        if cancelled:
            self._notify_terminal("cancelled", job_to_cancel)
        return cancelled, previous_status

    def cancel(self, job_id):
        cancelled, _previous_status = self._cancel_impl(job_id)
        return cancelled

    def _cancel_queued(self, job_id, *, require_unstarted):
        c = jobs_table.c
        filters = [c.id == job_id, c.status == "queued"]
        if require_unstarted:
            filters.append(c.attempts == 0)
        job_to_cancel: Job | None = None
        with self._write_transaction() as cx:
            row = cx.execute(
                sa.select(jobs_table).where(*filters).with_for_update()
            ).first()
            if row is None:
                return False
            job_to_cancel = self._job(row)
            result = cx.execute(
                jobs_table.update()
                .where(*filters)
                .values(
                    status="cancelled",
                    finished_at=self._clock(),
                    locked_by=None,
                    lease_expires_at=None,
                )
            )
            cancelled = result.rowcount > 0
        if cancelled:
            self._notify_terminal("cancelled", job_to_cancel)
        return cancelled

    def cancel_queued(self, job_id):
        return self._cancel_queued(job_id, require_unstarted=False)

    def cancel_unstarted(self, job_id):
        return self._cancel_queued(job_id, require_unstarted=True)

    def cancel_with_report(self, job_id):
        cancelled, previous_status = self._cancel_impl(job_id)
        return CancelResult(cancelled=cancelled, previous_status=previous_status)

    def cancel_scoped(self, job_id, *, org_id, kind):
        cancelled, _previous_status = self._cancel_impl(
            job_id, org_id=org_id, kind=kind
        )
        return cancelled

    def recover_expired(self):
        now = self._clock()
        c = jobs_table.c
        recovered = 0
        terminal_jobs: list[Job] = []
        with self._write_transaction() as cx:
            rows = cx.execute(
                sa.select(jobs_table)
                .where(c.status == "running", c.lease_expires_at < now)
                .with_for_update(skip_locked=True)
            ).fetchall()
            for r in rows:
                if _requeue_budget_left(r):
                    values = {
                        "status": "queued",
                        "available_at": now,
                        "locked_by": None,
                        "lease_expires_at": None,
                        "error": sa.func.coalesce(
                            c.error,
                            "worker_lease_expired: worker lease expired",
                        ),
                    }
                else:
                    values = {
                        "status": "failed",
                        "finished_at": now,
                        "error": (
                            "worker_lease_expired: worker lease expired"
                            " (attempts exhausted)"
                        ),
                        "locked_by": None,
                        "lease_expires_at": None,
                    }
                    terminal_jobs.append(self._job(r))
                cx.execute(jobs_table.update().where(c.id == r.id).values(**values))
                recovered += 1
        for job in terminal_jobs:
            self._notify_terminal("lease_exhausted", job)
        return recovered

    def fail_queued(self, job_id, error):
        stored_error = redact_stored_error(
            error,
            fallback_code="queue_job_failed",
        )
        if stored_error is None:
            stored_error = "queue_job_failed: operation failed"
        c = jobs_table.c
        with self._write_transaction() as cx:
            res = cx.execute(
                jobs_table.update()
                .where(c.id == job_id, c.status == "queued")
                .values(
                    status="failed",
                    error=stored_error,
                    finished_at=self._clock(),
                    locked_by=None,
                    lease_expires_at=None,
                )
            )
        return res.rowcount > 0

    def record_worker_heartbeat(
        self, worker_id, *, queue=None, worker_version=None, kinds=None, now=None
    ):
        stamp = now or self._clock()
        c = worker_heartbeats_table.c
        with self._write_transaction() as cx:
            updated = cx.execute(
                worker_heartbeats_table.update()
                .where(c.worker_id == worker_id)
                .values(
                    last_heartbeat_at=stamp,
                    queue=sa.func.coalesce(sa.literal(queue), c.queue),
                    worker_version=sa.func.coalesce(
                        sa.literal(worker_version), c.worker_version
                    ),
                    kinds=sa.func.coalesce(sa.literal(kinds), c.kinds),
                )
            )
            if updated.rowcount == 0:
                cx.execute(
                    worker_heartbeats_table.insert().values(
                        worker_id=worker_id,
                        queue=queue,
                        first_seen_at=stamp,
                        last_heartbeat_at=stamp,
                        worker_version=worker_version,
                        kinds=kinds,
                    )
                )

    def count_live_workers(self, *, within_seconds, now=None):
        cutoff = (now or self._clock()) - timedelta(seconds=within_seconds)
        c = worker_heartbeats_table.c
        with self.engine.connect() as cx:
            n = cx.execute(
                sa.select(sa.func.count()).where(c.last_heartbeat_at >= cutoff)
            ).scalar_one()
        return int(n)

    def list_worker_heartbeats(self):
        c = worker_heartbeats_table.c
        with self.engine.connect() as cx:
            rows = cx.execute(
                sa.select(worker_heartbeats_table).order_by(c.last_heartbeat_at.desc())
            ).fetchall()
        return [
            WorkerHeartbeat(
                worker_id=r.worker_id,
                queue=r.queue,
                first_seen_at=_parse(r.first_seen_at),
                last_heartbeat_at=_parse(r.last_heartbeat_at),
                worker_version=r.worker_version,
                kinds=getattr(r, "kinds", None),
            )
            for r in rows
        ]

    def unheard_handler_authorities(self, *, heartbeat_cutoff, now):
        a = job_handler_authorities_table.c
        h = worker_heartbeats_table.c
        joined = job_handler_authorities_table.outerjoin(
            worker_heartbeats_table, a.worker_id == h.worker_id
        )
        # A missing heartbeat row is quiet too, not exempt — hence the
        # outer join plus the IS NULL arm.
        quiet = sa.or_(h.worker_id.is_(None), h.last_heartbeat_at < heartbeat_cutoff)
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(sa.func.count(), sa.func.min(a.claimed_at))
                .select_from(joined)
                .where(quiet)
            ).first()
        count = int(row[0]) if row is not None else 0
        oldest = _parse(row[1]) if row is not None else None
        age = None
        if oldest is not None:
            age = max(0.0, (_parse(now) - oldest).total_seconds())
        return UnheardHandlerAuthorities(count=count, oldest_claim_age_seconds=age)

    def oldest_queued_created_at(self):
        c = jobs_table.c
        with self.engine.connect() as cx:
            oldest = cx.execute(
                sa.select(sa.func.min(c.created_at)).where(c.status == "queued")
            ).scalar_one()
        return _parse(oldest)

    def get(self, job_id):
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(jobs_table).where(jobs_table.c.id == job_id)
            ).first()
        return self._job(row) if row else None

    def get_scoped(self, job_id, *, org_id, kind):
        c = jobs_table.c
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(jobs_table).where(
                    c.id == job_id, c.org_id == org_id, c.kind == kind
                )
            ).first()
        return self._job(row) if row else None

    def list_jobs(self, status=None, limit=100):
        q = sa.select(jobs_table).order_by(jobs_table.c.id.desc()).limit(limit)
        if status:
            q = q.where(jobs_table.c.status == status)
        with self.engine.connect() as cx:
            rows = cx.execute(q).fetchall()
        return [self._job(r) for r in rows]

    def find_job_by_refs(
        self,
        kind: str,
        *,
        statuses: tuple[str, ...] = ("queued", "running"),
        project_id: str | None = None,
        storage_org_id: int | None = None,
        source_id: int | None = None,
        sheet_id: int | None = None,
        row_id: int | None = None,
        workspace_root: str | None = None,
        dedupe_key: str | None = None,
    ) -> Job | None:
        _validate_ref_lookup(
            kind,
            statuses=statuses,
            project_id=project_id,
            storage_org_id=storage_org_id,
            source_id=source_id,
            sheet_id=sheet_id,
            row_id=row_id,
            workspace_root=workspace_root,
            dedupe_key=dedupe_key,
        )
        c = jobs_table.c
        q = sa.select(jobs_table).where(c.kind == kind).order_by(c.id.desc()).limit(1)
        if statuses:
            q = q.where(c.status.in_(statuses))
        if project_id is not None:
            q = q.where(c.project_id == project_id)
        if storage_org_id is not None:
            q = q.where(c.storage_org_id == storage_org_id)
        if source_id is not None:
            q = q.where(c.source_id == source_id)
        if sheet_id is not None:
            q = q.where(c.sheet_id == sheet_id)
        if row_id is not None:
            q = q.where(c.row_id == row_id)
        if workspace_root is not None:
            q = q.where(c.workspace_root == workspace_root)
        if dedupe_key is not None:
            q = q.where(c.dedupe_key == dedupe_key)
        with self.engine.connect() as cx:
            row = cx.execute(q).first()
        return self._job(row) if row else None

    def get_project_run_job(
        self,
        project_id: str,
        run_id: int,
        *,
        storage_org_id: int | None = None,
    ) -> Job | None:
        c = jobs_table.c
        clauses = [
            c.kind == PROJECT_RUN_KIND,
            c.project_id == project_id,
            c.run_id == run_id,
        ]
        if storage_org_id is not None:
            clauses.append(c.storage_org_id == storage_org_id)
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(jobs_table).where(*clauses).order_by(c.id.desc()).limit(1)
            ).first()
        return self._job(row) if row else None

    def list_project_jobs(
        self,
        project_id: str,
        *,
        storage_org_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        kind: str | None = None,
        source_id: int | None = None,
    ) -> list[Job]:
        c = jobs_table.c
        q = (
            sa.select(jobs_table)
            .where(c.project_id == project_id)
            .order_by(c.id.desc())
            .limit(limit)
        )
        if storage_org_id is not None:
            q = q.where(c.storage_org_id == storage_org_id)
        if status:
            q = q.where(c.status == status)
        if kind:
            q = q.where(c.kind == kind)
        if source_id is not None:
            # Covered by idx_jobs_source_status_id (project_id, source_id,
            # status, id DESC).
            q = q.where(c.source_id == source_id)
        with self.engine.connect() as cx:
            rows = cx.execute(q).fetchall()
        return [self._job(r) for r in rows]

    def counts(self):
        c = jobs_table.c
        with self.engine.connect() as cx:
            rows = cx.execute(
                sa.select(c.status, sa.func.count()).group_by(c.status)
            ).fetchall()
        out = dict.fromkeys(STATUSES, 0)
        out.update(dict(rows))
        return out

    def close(self):
        self.engine.dispose()


# Compatibility aliases for backend-specific constructors.
SqliteJobQueue = SqlAlchemyJobQueue
PostgresJobQueue = SqlAlchemyJobQueue


def open_queue(
    *,
    workspace: str | Path | None = None,
    database_url: str | None = None,
    schema_mode: str | None = None,
    hosted: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> JobQueue:
    """Backend selection: an explicit database_url means the hosted run queue
    (Postgres); otherwise the queue is `<workspace>/.queue.db`.

    ``hosted`` is the DECLARED tenancy/storage posture (queue-hosted-posture-
    explicit-v1), threaded from the composition root — never inferred from the
    engine dialect. It defaults to ``False`` so local one-command paths can
    never accidentally acquire hosted posture; only the hosted app / hosted
    worker wiring declares it ``True``.

    ``clock`` is ``SqlAlchemyJobQueue``'s injected time source, threaded
    through here so compositions that only reach the
    queue via ``open_queue`` (``create_app``'s workspace) can still pin
    lease/fence/liveness decisions to a manual clock. ``None`` (production)
    keeps real time."""
    if database_url:
        selected_mode = schema_mode or os.environ.get(
            "FRISKET_RUN_QUEUE_SCHEMA_MODE", "initialize"
        )
        return SqlAlchemyJobQueue(
            database_url, schema_mode=selected_mode, hosted=hosted, clock=clock
        )
    if workspace is None:
        raise ValueError("open_queue needs a workspace or a database_url")
    return SqlAlchemyJobQueue(
        Path(workspace) / QUEUE_DB_NAME, hosted=hosted, clock=clock
    )
