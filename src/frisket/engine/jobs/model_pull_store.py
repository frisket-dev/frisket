"""Durable pull-operation record.

The generic queue (``frisket.jobs.queue``) has no progress/cancellation
context for a long-running provisioning operation and its ``dedupe_key``
lookup is project-scoped, not workspace-scoped. ``model_pulls`` is a small,
dedicated table sharing the queue's ``jobs_metadata`` (so it provisions on the
same engine as ``jobs``/``worker_heartbeats``, see ``worker_heartbeats_table``
for the precedent) that is the DEDUPE AUTHORITY for in-app model pulls: one
active (``pending``/``running``) row per ``(workspace_root, model_ref)``,
enforced by a partial unique index so a race between two concurrent requests
resolves to the SAME row rather than two queue jobs racing to write the same
weights.

Every write goes through this module; the queue job payload only ever carries
``{"pull_id", "workspace_root"}`` -- this row is the durable, user-facing
truth (phase, byte progress, resolved digest/size, sanitized error) that
outlives any single job attempt (queue retries and worker restarts re-attach
to the same row via ``pull_id``).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from frisket.engine.jobs.queue import (
    _SQLITE_BEGIN_IMMEDIATE_OPT,
    _now,
    _parse,
    jobs_metadata,
    jobs_table,
)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
# A DISTINCT terminal status for a manually uninstalled
# artifact, so the manage page can tell "never installed" / "installed (done)"
# / "uninstalled" apart cleanly, and a re-pull creates a fresh active row.
STATUS_UNINSTALLED = "uninstalled"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)
TERMINAL_STATUSES = (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_UNINSTALLED,
)

# Durable user-facing error text is capped hard: raw provider bodies (Ollama's
# error contract is an arbitrary string) never become persisted copy verbatim
# Keep errors safe for persistence and API responses.
MAX_ERROR_MESSAGE_LENGTH = 200

model_pulls_table = sa.Table(
    "model_pulls",
    jobs_metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("workspace_root", sa.String, nullable=False),
    sa.Column("model_ref", sa.String, nullable=False),
    sa.Column(
        "status",
        sa.String,
        nullable=False,
        default=STATUS_PENDING,
        server_default=STATUS_PENDING,
    ),
    sa.Column("job_id", sa.Integer),
    sa.Column("phase", sa.String),
    sa.Column("total_bytes", sa.Integer),
    sa.Column("completed_bytes", sa.Integer),
    sa.Column("error_code", sa.String),
    sa.Column("error_message", sa.String),
    # Set to the job id (as a string) once known -- an internal correlation
    # handle for support/ops, never shown as a raw provider body.
    sa.Column("correlation_id", sa.String),
    sa.Column("resolved_digest", sa.String),
    sa.Column("resolved_size", sa.Integer),
    sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    # The claim-time-resolved endpoint origin (scheme+host[:port], never a
    # path/query/credential --
    # `normalize_ollama_url`'s output) recorded at creation from the
    # resolving route. The handler compares its OWN claim-time-resolved
    # origin against this and refuses (``endpoint_changed``) on a mismatch --
    # a URL change must not silently retarget an already-queued pull.
    sa.Column("endpoint_origin", sa.String),
    # Canonical endpoint identity paired with the immutable origin snapshot.
    # Both are null for non-local artifacts.
    sa.Column("endpoint_id", sa.String),
    # Actor provenance (item 3): nullable -- the local tier has no actor
    # identity and always leaves this None; the team tier's owner-gated
    # route passes the acting user/service identity.
    sa.Column("initiated_by", sa.String),
    # Four nullable artifact columns. For a local-server model pull,
    # `artifact_kind` is NULL. The pinned manifest's provenance is
    # recorded here at pull time; `resolved_digest`/`resolved_size` (already
    # present) carry the composite digest + total size for artifact pulls too.
    sa.Column("artifact_kind", sa.String),
    sa.Column("artifact_source_url", sa.String),
    sa.Column("artifact_license", sa.String),
    sa.Column("artifact_manifest_version", sa.String),
    sa.Index("idx_model_pulls_workspace_status_id", "workspace_root", "status", "id"),
    # The dedupe authority: at most one pending/running row per (workspace,
    # ref). SQLAlchemy supports a partial unique index on both backends via
    # the dialect-specific *_where kwargs -- same index name, one definition.
    sa.Index(
        "uq_model_pulls_active_ref",
        "workspace_root",
        "model_ref",
        unique=True,
        sqlite_where=sa.text(f"status IN ('{STATUS_PENDING}', '{STATUS_RUNNING}')"),
        postgresql_where=sa.text(f"status IN ('{STATUS_PENDING}', '{STATUS_RUNNING}')"),
    ),
    # At most ONE active pull per WORKSPACE (any ref). This makes the rule
    # atomic (an INSERT conflict, not a separate non-atomic pre-check read).
    sa.Index(
        "uq_model_pulls_active_workspace",
        "workspace_root",
        unique=True,
        sqlite_where=sa.text(f"status IN ('{STATUS_PENDING}', '{STATUS_RUNNING}')"),
        postgresql_where=sa.text(f"status IN ('{STATUS_PENDING}', '{STATUS_RUNNING}')"),
    ),
)


@dataclass
class ModelPullRow:
    id: int
    workspace_root: str
    model_ref: str
    status: str
    job_id: int | None
    phase: str | None
    total_bytes: int | None
    completed_bytes: int | None
    error_code: str | None
    error_message: str | None
    correlation_id: str | None
    resolved_digest: str | None
    resolved_size: int | None
    cancel_requested_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    endpoint_origin: str | None = None
    endpoint_id: str | None = None
    initiated_by: str | None = None
    artifact_kind: str | None = None
    artifact_source_url: str | None = None
    artifact_license: str | None = None
    artifact_manifest_version: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def effective_artifact_kind(self) -> str:
        """A NULL ``artifact_kind`` denotes a local-server model pull."""
        return self.artifact_kind or "ollama"


def _truncate(text: str | None, limit: int = MAX_ERROR_MESSAGE_LENGTH) -> str | None:
    if text is None:
        return None
    clean = str(text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _row(raw: Any) -> ModelPullRow:
    return ModelPullRow(
        id=raw.id,
        workspace_root=raw.workspace_root,
        model_ref=raw.model_ref,
        status=raw.status,
        job_id=raw.job_id,
        phase=raw.phase,
        total_bytes=raw.total_bytes,
        completed_bytes=raw.completed_bytes,
        error_code=raw.error_code,
        error_message=raw.error_message,
        correlation_id=raw.correlation_id,
        resolved_digest=raw.resolved_digest,
        resolved_size=raw.resolved_size,
        cancel_requested_at=_parse(raw.cancel_requested_at),
        created_at=_parse(raw.created_at),
        started_at=_parse(raw.started_at),
        finished_at=_parse(raw.finished_at),
        endpoint_origin=raw.endpoint_origin,
        endpoint_id=raw.endpoint_id,
        initiated_by=raw.initiated_by,
        artifact_kind=raw.artifact_kind,
        artifact_source_url=raw.artifact_source_url,
        artifact_license=raw.artifact_license,
        artifact_manifest_version=raw.artifact_manifest_version,
    )


# ---------------------------------------------------------------------------
# queue-terminal row recovery
# ---------------------------------------------------------------------------
#
# A queue-side terminal failure (no handler registered for the kind, or a
# lease-exhausted job the recovery machinery itself terminal-fails -- see
# queue.py's `recover_expired`) happens ENTIRELY inside the queue, never
# routing back through the model.pull handler's own `_fail()` bookkeeping.
# Left alone, the pull row stays 'pending'/'running' forever even though its
# job is long dead -- an infinite spinner. Every read path joins the linked
# job row (same database/engine) and, when it finds exactly that mismatch,
# derives the effective terminal status and PERSISTS the repair so the next
# read (and every other reader) sees the corrected state, not just this one.

_WORKER_FAILED_MESSAGE = (
    "the worker process ended without recording a specific outcome for this "
    "pull (no handler was registered for it, or its attempts were exhausted "
    "after a lease expired)"
)

# A row that never reached ``set_job_id`` at all (an
# enqueue that crashed between ``create_or_get_active`` and ``set_job_id``)
# has no linked job for the branch below to ever read a terminal status
# from -- left alone it stays active FOREVER, permanently wedging the
# one-active-pull-per-workspace slot. Once a job-less active row
# is older than this threshold it is presumed to be exactly that failure
# mode and is terminal-failed by read-repair instead of waiting on nothing.
JOBLESS_STALE_THRESHOLD_SECONDS = 15 * 60

_JOBLESS_STALE_MESSAGE = (
    "this pull was never attached to a queued job (its creating request "
    "likely failed between creating the row and enqueueing the job) and "
    "has been treated as an enqueue failure"
)


def _job_terminal_status(engine: sa.engine.Engine, job_id: int) -> str | None:
    c = jobs_table.c
    with engine.connect() as cx:
        row = cx.execute(sa.select(c.status).where(c.id == job_id)).first()
    return str(row.status) if row is not None else None


@contextmanager
def _write_transaction(engine: sa.engine.Engine):
    """Mirrors ``JobQueue._write_transaction`` (queue.py) for this module's
    own read-then-write callers. On SQLite it opts into ``BEGIN IMMEDIATE``
    (the write lock taken up front, on the SAME engine ``JobQueue.__init__``
    already installed the listener on) so a SELECT followed by an UPDATE in
    one transaction never risks the classic deferred-to-reserved lock
    upgrade deadlock; on Postgres it is an ordinary transaction. Used by
    ``_read_repair``'s atomic recheck (below) so the linked job's status and
    the pull row's write commit as one unit."""
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn = conn.execution_options(**{_SQLITE_BEGIN_IMMEDIATE_OPT: True})
            with conn.begin():
                yield conn
    else:
        with engine.begin() as conn:
            yield conn


def _read_repair(
    engine: sa.engine.Engine, row: ModelPullRow | None
) -> ModelPullRow | None:
    if row is None or row.status not in ACTIVE_STATUSES:
        return row

    if row.job_id is None:
        # item 2a: a job-less active row -- either a never-claimed-job
        # cancel that only stamped `cancel_requested_at` (the caller has no
        # job_id to route a queue-side cancel through) or a plain enqueue
        # failure that never got as far as `set_job_id`. Neither will EVER
        # be repaired by the job-status branch below, since there is no
        # linked job to read a status from.
        if row.cancel_requested_at is not None:
            mark_cancelled(engine, row.id)
        elif _now() - row.created_at > timedelta(
            seconds=JOBLESS_STALE_THRESHOLD_SECONDS
        ):
            mark_failed(
                engine,
                row.id,
                error_code="enqueue_failed",
                error_message=_JOBLESS_STALE_MESSAGE,
            )
        else:
            # Fresh and not cancel-requested -- the creating route may
            # still be about to call `set_job_id`; leave it alone.
            return row
        return _get_raw(engine, row.id) or row

    job_id = row.job_id
    # Cheap fast-path probe (its own short read-only transaction): the
    # overwhelmingly common case is "job still active", which needs no
    # write at all -- avoid taking a write lock on every read of a normal
    # in-flight pull just to check.
    if _job_terminal_status(engine, job_id) not in ("cancelled", "failed"):
        return row

    # The fast-path probe above can be stale by
    # the time we get here -- e.g. an admin retry (`JobQueue.retry`)
    # reactivating this exact row's job in the gap. The SELECT that makes
    # the terminalize decision and the UPDATE that performs it must
    # therefore be the SAME check, run inside ONE transaction, never two
    # separate ones with a window for a retry to land in.
    c = model_pulls_table.c
    with _write_transaction(engine) as cx:
        # This SELECT and the UPDATE below must observe the SAME locked row --
        # under Postgres READ
        # COMMITTED, an unlocked SELECT here can read a status that a
        # concurrent transaction (e.g. an admin retry's own `mark_running`)
        # changes and commits before our UPDATE lands, racing it instead of
        # serializing against it. `with_for_update()` takes the row lock so a
        # concurrent writer on this SAME jobs row blocks until we commit.
        # SQLAlchemy's sqlite dialect silently omits FOR UPDATE entirely (no
        # error, no warning -- confirmed by compiling this exact statement
        # shape against both dialects in
        # test_read_repair_select_for_update_renders_on_postgres_and_is_a_sqlite_no_op),
        # which is harmless here: SQLite's own `_write_transaction` already
        # opts into `BEGIN IMMEDIATE`, taking a whole-database write lock for
        # this entire transaction, so no per-row lock is needed (or even
        # expressible) on that backend.
        fresh = cx.execute(
            sa.select(jobs_table.c.status)
            .where(jobs_table.c.id == job_id)
            .with_for_update()
        ).first()
        fresh_status = str(fresh.status) if fresh is not None else None
        if fresh_status == "cancelled":
            cx.execute(
                model_pulls_table.update()
                .where(
                    c.id == row.id, c.status.in_(ACTIVE_STATUSES), c.job_id == job_id
                )
                .values(status=STATUS_CANCELLED, finished_at=_now())
            )
        elif fresh_status == "failed":
            cx.execute(
                model_pulls_table.update()
                .where(
                    c.id == row.id, c.status.in_(ACTIVE_STATUSES), c.job_id == job_id
                )
                .values(
                    status=STATUS_FAILED,
                    error_code="worker_failed",
                    error_message=_truncate(_WORKER_FAILED_MESSAGE),
                    finished_at=_now(),
                )
            )
        else:
            return row
    return _get_raw(engine, row.id) or row


def _get_raw(engine: sa.engine.Engine, pull_id: int) -> ModelPullRow | None:
    c = model_pulls_table.c
    with engine.connect() as cx:
        row = cx.execute(sa.select(model_pulls_table).where(c.id == pull_id)).first()
    return _row(row) if row is not None else None


def get(engine: sa.engine.Engine, pull_id: int) -> ModelPullRow | None:
    return _read_repair(engine, _get_raw(engine, pull_id))


def find_active_for_workspace(
    engine: sa.engine.Engine, workspace_root: str
) -> ModelPullRow | None:
    """Any pending/running pull for this workspace, regardless of model ref
    -- the route's one-active-pull-per-workspace busy check."""
    c = model_pulls_table.c
    with engine.connect() as cx:
        row = cx.execute(
            sa.select(model_pulls_table)
            .where(
                c.workspace_root == workspace_root,
                c.status.in_(ACTIVE_STATUSES),
            )
            .order_by(c.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    repaired = _read_repair(engine, _row(row))
    return (
        repaired
        if repaired is not None and repaired.status in ACTIVE_STATUSES
        else None
    )


def list_recent(
    engine: sa.engine.Engine, workspace_root: str, *, limit: int = 20
) -> list[ModelPullRow]:
    c = model_pulls_table.c
    active_first = sa.case((c.status.in_(ACTIVE_STATUSES), 0), else_=1)
    with engine.connect() as cx:
        rows = cx.execute(
            sa.select(model_pulls_table)
            .where(c.workspace_root == workspace_root)
            .order_by(active_first, c.id.desc())
            .limit(limit)
        ).fetchall()
    return [_read_repair(engine, _row(r)) for r in rows]


class ModelPullBusyError(RuntimeError):
    """Raised by :func:`create_or_get_active` when the workspace already has
    an active pull for a DIFFERENT ref (the ``uq_model_pulls_active_workspace``
    conflict). ``.active`` is the row that is currently occupying the
    workspace slot; callers (the local route) translate this into a 409
    ``pull_busy``. Deliberately a raised exception rather than a third return
    shape: every SUCCESSFUL outcome (created or same-ref dedupe) keeps the
    existing ``tuple[ModelPullRow, bool]`` contract byte-for-byte, so a
    caller unaware of this error (the team route's own call site, fixed by
    its owning agent) keeps compiling/running exactly as before -- it simply
    surfaces the same conflict as an unhandled exception it already tolerated
    (the prior code re-raised a bare ``IntegrityError`` in this situation)."""

    def __init__(self, active: "ModelPullRow") -> None:
        super().__init__(
            f"an active pull already exists for this workspace (ref={active.model_ref!r})"
        )
        self.active = active


def _active_row_for_ref(
    engine: sa.engine.Engine, *, workspace_root: str, model_ref: str
) -> ModelPullRow | None:
    c = model_pulls_table.c
    with engine.connect() as cx:
        row = cx.execute(
            sa.select(model_pulls_table)
            .where(
                c.workspace_root == workspace_root,
                c.model_ref == model_ref,
                c.status.in_(ACTIVE_STATUSES),
            )
            .order_by(c.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    repaired = _read_repair(engine, _row(row))
    return (
        repaired
        if repaired is not None and repaired.status in ACTIVE_STATUSES
        else None
    )


def _any_active_row_for_workspace(
    engine: sa.engine.Engine, *, workspace_root: str
) -> ModelPullRow | None:
    c = model_pulls_table.c
    with engine.connect() as cx:
        row = cx.execute(
            sa.select(model_pulls_table)
            .where(c.workspace_root == workspace_root, c.status.in_(ACTIVE_STATUSES))
            .order_by(c.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    repaired = _read_repair(engine, _row(row))
    return (
        repaired
        if repaired is not None and repaired.status in ACTIVE_STATUSES
        else None
    )


def create_or_get_active(
    engine: sa.engine.Engine,
    *,
    workspace_root: str,
    model_ref: str,
    endpoint_id: str | None = None,
    endpoint_origin: str | None = None,
    initiated_by: str | None = None,
) -> tuple[ModelPullRow, bool]:
    """Fully atomic find-or-create against BOTH partial-unique indexes.

    An INSERT either succeeds (a fresh ``pending`` row, ``created=True``) or
    raises ``IntegrityError`` on one of the two partial unique indexes:
    ``uq_model_pulls_active_ref`` (an active row for this EXACT ref already
    exists -- dedupe, ``created=False``) or ``uq_model_pulls_active_workspace``
    (an active row for a DIFFERENT ref already occupies the workspace --
    :class:`ModelPullBusyError`). ``engine.begin()`` rolls back the failed
    INSERT's transaction before the except block runs another statement --
    required on Postgres, where a failed statement aborts the transaction.

    Endpoint identity/origin and actor provenance are stamped at creation only.

    A read-repair pass (see the module docstring section above) may resolve
    what LOOKED like a conflicting active row into a terminal one (its job
    died queue-side without ever updating this row) -- in that case the slot
    is actually free, so the insert is retried exactly once.
    """
    now = _now()
    for _attempt in range(2):
        try:
            with engine.begin() as cx:
                result = cx.execute(
                    model_pulls_table.insert().values(
                        workspace_root=workspace_root,
                        model_ref=model_ref,
                        status=STATUS_PENDING,
                        created_at=now,
                        endpoint_id=endpoint_id,
                        endpoint_origin=endpoint_origin,
                        initiated_by=initiated_by,
                    )
                )
                new_id = result.inserted_primary_key[0]
        except sa.exc.IntegrityError:
            same_ref = _active_row_for_ref(
                engine, workspace_root=workspace_root, model_ref=model_ref
            )
            if same_ref is not None:
                return same_ref, False
            other_active = _any_active_row_for_workspace(
                engine, workspace_root=workspace_root
            )
            if other_active is not None:
                raise ModelPullBusyError(other_active) from None
            # Read-repair freed the slot (or this was a genuine transient
            # race with a since-terminalized row) -- one retry, then give up
            # cleanly rather than looping.
            continue
        created = get(engine, new_id)
        assert created is not None  # just inserted under the same transaction boundary
        return created, True
    raise RuntimeError(
        "create_or_get_active: persistent unique-index conflict with no "
        "resolvable active row after a read-repair retry"
    )


def set_job_id(engine: sa.engine.Engine, pull_id: int, *, job_id: int) -> None:
    c = model_pulls_table.c
    with engine.begin() as cx:
        cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id)
            .values(job_id=job_id, correlation_id=str(job_id))
        )


def mark_running(engine: sa.engine.Engine, pull_id: int, *, job_id: int) -> bool:
    """Idempotent across retries: a resumed attempt on the SAME row (item 1b
    -- the row stays 'running' across a retryable failure) re-enters this
    with status already 'running', not 'pending'; the WHERE guard accepts
    either active status so the retry's re-claim keeps working, while a
    terminal row (already done/failed/cancelled by a race) is left alone.

    Also allows exactly ONE reactivation transition, ``failed -> running``
    after an admin retry: a generic queue retry
    (``JobQueue.retry``) requeues the JOB row with no knowledge of
    ``model_pulls`` at all, so a job whose last attempt already finalized
    this row to 'failed' can be admin-retried and run to completion while
    the row stays terminal forever -- unless this same job_id reclaiming
    the row is allowed to reactivate it. Scoped tightly: only a job_id that
    matches the row's OWN already-recorded ``job_id`` may do this (the
    SAME job re-attaching, never a different/fresh job stealing a failed
    row). ``cancelled`` is deliberately excluded from this OR -- an
    operator's cancel is a permanent terminal decision that a job-level
    retry must never reopen.

    Returns whether the transition
    actually applied. ``False`` means this pull_id is terminal in a way this
    job_id may NOT reactivate (already 'done'/'cancelled', or 'failed' under
    a DIFFERENT job_id) -- the caller (the ``model.pull`` handler) MUST treat
    that as a hard fence and abort with ``NonRetryableJobError`` before
    contacting any upstream server, rather than silently proceeding as if it
    owned the row. This is what closes the gap a migration-superseded row
    (or any other terminal row whose job somehow re-runs, including an
    admin retry of a cancelled job) would otherwise reopen.

    A successful reactivation also clears whatever error_code/error_message
    the PRIOR attempt left on the row (item 2: retry state hygiene) -- a
    freshly (re)claimed row must not carry a stale terminal error into a
    fresh attempt that may go on to succeed (``mark_done`` clears the same
    fields on the completing side of that same guarantee)."""
    c = model_pulls_table.c
    with engine.begin() as cx:
        res = cx.execute(
            model_pulls_table.update()
            .where(
                c.id == pull_id,
                sa.or_(
                    c.status.in_(ACTIVE_STATUSES),
                    sa.and_(
                        c.status == STATUS_FAILED,
                        c.job_id == job_id,
                        # A migration-superseded loser is PERMANENTLY fenced:
                        # its cancelled job can be resurrected by an admin
                        # queue retry (JobQueue.retry accepts cancelled jobs),
                        # and without this exclusion the retried job would
                        # reopen the row and contact the daemon. Null-safe: a failed
                        # row with no error_code must stay reactivatable.
                        sa.or_(
                            c.error_code.is_(None),
                            c.error_code != "superseded_by_migration",
                        ),
                    ),
                ),
            )
            .values(
                status=STATUS_RUNNING,
                job_id=job_id,
                correlation_id=str(job_id),
                started_at=sa.func.coalesce(c.started_at, _now()),
                finished_at=None,
                error_code=None,
                error_message=None,
            )
        )
    return res.rowcount > 0


def set_artifact_metadata(
    engine: sa.engine.Engine,
    pull_id: int,
    *,
    artifact_kind: str,
    artifact_source_url: str | None,
    artifact_license: str | None,
    artifact_manifest_version: str | None,
) -> None:
    """Record the artifact manifest provenance on a pull row.

    Stamped by the artifact pull backend once it resolves the ref against the
    pinned manifest (or, for an unpinned free-form ``hf:`` pull, with
    ``artifact_license=None``/``artifact_manifest_version=None`` and the raw
    source URL). Idempotent -- a resumed attempt re-stamps the same values.
    """
    c = model_pulls_table.c
    with engine.begin() as cx:
        cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id)
            .values(
                artifact_kind=artifact_kind,
                artifact_source_url=artifact_source_url,
                artifact_license=artifact_license,
                artifact_manifest_version=artifact_manifest_version,
            )
        )


def update_progress(
    engine: sa.engine.Engine,
    pull_id: int,
    *,
    phase: str | None = None,
    total_bytes: int | None = None,
    completed_bytes: int | None = None,
) -> None:
    """Throttling (how often this is called) is the handler's job; this is a
    plain durable write."""
    c = model_pulls_table.c
    with engine.begin() as cx:
        cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id)
            .values(
                phase=phase,
                total_bytes=total_bytes,
                completed_bytes=completed_bytes,
            )
        )


def request_cancel(engine: sa.engine.Engine, pull_id: int) -> bool:
    """Stamp ``cancel_requested_at`` on an active row. Returns False if the
    row is missing or already terminal (nothing to cancel)."""
    c = model_pulls_table.c
    now = _now()
    with engine.begin() as cx:
        res = cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status.in_(ACTIVE_STATUSES))
            .values(cancel_requested_at=sa.func.coalesce(c.cancel_requested_at, now))
        )
    return res.rowcount > 0


def mark_done(
    engine: sa.engine.Engine,
    pull_id: int,
    *,
    resolved_digest: str | None = None,
    resolved_size: int | None = None,
) -> bool:
    """Guarded by ``status IN ACTIVE_STATUSES`` (like every other terminal
    writer here) so a race against a cancel/fail from another path never
    clobbers whichever terminal state wins first -- 'tolerant of an already-
    terminal row' throughout the family, not just cancel. Returns whether the
    write actually applied.

    Also clears error_code/error_message. A row that reached 'done' —
    including via an admin
    retry that reactivates a previously-'failed' row through ``mark_running``
    and then completes -- must never publish a stale terminal error
    alongside a 'done' status; the DTO's ``error`` field must be null
    whenever the pull actually succeeded."""
    c = model_pulls_table.c
    with engine.begin() as cx:
        res = cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status.in_(ACTIVE_STATUSES))
            .values(
                status=STATUS_DONE,
                phase="done",
                resolved_digest=resolved_digest,
                resolved_size=resolved_size,
                finished_at=_now(),
                error_code=None,
                error_message=None,
            )
        )
    return res.rowcount > 0


def record_attempt_error(
    engine: sa.engine.Engine,
    pull_id: int,
    *,
    error_code: str,
    error_message: str | None,
) -> None:
    """Item 1b: records the latest attempt's error WITHOUT finalizing the
    row -- status stays whatever active status it already was ('running').
    Used when this attempt is retryable and NOT the job's final attempt, so
    the requeued job resumes the SAME row instead of a duplicate active row
    getting created for the same ref while this one still shows 'failed'."""
    c = model_pulls_table.c
    with engine.begin() as cx:
        cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status.in_(ACTIVE_STATUSES))
            .values(
                error_code=error_code,
                error_message=_truncate(error_message),
            )
        )


def mark_failed(
    engine: sa.engine.Engine,
    pull_id: int,
    *,
    error_code: str,
    error_message: str | None,
) -> bool:
    c = model_pulls_table.c
    with engine.begin() as cx:
        res = cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status.in_(ACTIVE_STATUSES))
            .values(
                status=STATUS_FAILED,
                error_code=error_code,
                error_message=_truncate(error_message),
                finished_at=_now(),
            )
        )
    return res.rowcount > 0


def mark_uninstalled(engine: sa.engine.Engine, pull_id: int) -> bool:
    """Transition a completed (``done``) artifact pull to ``uninstalled`` after
    its on-disk bytes are removed. Guarded on ``done`` so an active
    or already-terminal row is left alone. Returns whether it applied."""
    c = model_pulls_table.c
    with engine.begin() as cx:
        res = cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status == STATUS_DONE)
            .values(status=STATUS_UNINSTALLED, finished_at=_now())
        )
    return res.rowcount > 0


def find_done_for_ref(
    engine: sa.engine.Engine, *, workspace_root: str, model_ref: str
) -> ModelPullRow | None:
    """The most recent ``done`` pull for a ref in a workspace -- the uninstall
    target."""
    c = model_pulls_table.c
    with engine.connect() as cx:
        row = cx.execute(
            sa.select(model_pulls_table)
            .where(
                c.workspace_root == workspace_root,
                c.model_ref == model_ref,
                c.status == STATUS_DONE,
            )
            .order_by(c.id.desc())
            .limit(1)
        ).first()
    return _row(row) if row is not None else None


class UninstallInFlightError(RuntimeError):
    """Raised by :func:`mark_ref_uninstalled` when an active (pending/running)
    pull exists for the ref -- uninstalling out from under an in-flight install
    would race the worker's atomic promote. The route surfaces this as 409."""


def mark_ref_uninstalled(
    engine: sa.engine.Engine, *, workspace_root: str, model_ref: str
) -> ModelPullRow | None:
    """Atomically fence and tombstone an uninstall.

    In ONE write transaction: (1) refuse if any active pull exists for the ref
    (:class:`UninstallInFlightError` -- never uninstall bytes an in-flight pull
    is about to promote); (2) flip EVERY ``done`` row for (workspace, ref) to
    ``uninstalled`` (not just one -- a duplicate historical ``done`` row must
    not resurrect the artifact as "installed"). Returns the latest flipped row
    (for the DTO), or ``None`` when nothing was installed. The caller removes
    the on-disk bytes AFTER this returns; translator-cache invalidation covers
    the brief in-flight-load window."""
    c = model_pulls_table.c
    with _write_transaction(engine) as cx:
        active = cx.execute(
            sa.select(sa.func.count())
            .select_from(model_pulls_table)
            .where(
                c.workspace_root == workspace_root,
                c.model_ref == model_ref,
                c.status.in_(ACTIVE_STATUSES),
            )
        ).scalar()
        if active:
            raise UninstallInFlightError(
                f"a pull is in flight for {model_ref!r}; cannot uninstall"
            )
        res = cx.execute(
            model_pulls_table.update()
            .where(
                c.workspace_root == workspace_root,
                c.model_ref == model_ref,
                c.status == STATUS_DONE,
            )
            .values(status=STATUS_UNINSTALLED, finished_at=_now())
        )
        if res.rowcount == 0:
            return None
        latest = cx.execute(
            sa.select(model_pulls_table)
            .where(
                c.workspace_root == workspace_root,
                c.model_ref == model_ref,
                c.status == STATUS_UNINSTALLED,
            )
            .order_by(c.id.desc())
            .limit(1)
        ).first()
    return _row(latest) if latest is not None else None


def mark_cancelled(engine: sa.engine.Engine, pull_id: int) -> bool:
    """Tolerant of an already-terminal row (item 1a: the cancel route may
    mark a never-claimed pull cancelled directly; the handler's own
    cooperative cancel check must not then error or double-finalize if it
    somehow still runs)."""
    c = model_pulls_table.c
    with engine.begin() as cx:
        res = cx.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status.in_(ACTIVE_STATUSES))
            .values(status=STATUS_CANCELLED, finished_at=_now())
        )
    return res.rowcount > 0


# Wire version 3 carries both parts of immutable local-endpoint provenance:
# stable endpoint identity plus the enqueue-time origin snapshot.
PULL_DTO_SCHEMA_VERSION = "frisket.model_pull.v3"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def to_dto(row: ModelPullRow) -> dict[str, Any]:
    """The ONE ``frisket.model_pull.v3`` wire shape -- shared by the local
    tier's ``/api/providers/models/pull*`` routes and the team tier's
    ``/api/org/models/pull*`` routes, so the surfaces cannot drift on field
    names/types even though they are gated and scoped differently.

    The nested ``artifact`` object is null for an Ollama pull and, for an
    artifact pull, carries the pinned manifest provenance recorded at pull
    time. Local pulls carry both endpoint id and origin; artifact-only pulls
    carry null for both."""
    error = None
    if row.error_code or row.error_message:
        error = {"code": row.error_code, "message": row.error_message}
    artifact = None
    if row.artifact_kind is not None and row.artifact_kind != "ollama":
        artifact = {
            "kind": row.artifact_kind,
            "source_url": row.artifact_source_url,
            "license": row.artifact_license,
            "manifest_version": row.artifact_manifest_version,
        }
    return {
        "schemaVersion": PULL_DTO_SCHEMA_VERSION,
        "id": row.id,
        "model": row.model_ref,
        "status": row.status,
        "phase": row.phase,
        "total_bytes": row.total_bytes,
        "completed_bytes": row.completed_bytes,
        "error": error,
        "resolved_digest": row.resolved_digest,
        "resolved_size": row.resolved_size,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "cancel_requested": row.cancel_requested_at is not None,
        "endpoint_id": row.endpoint_id,
        # item 3: origins never carry credentials/paths (`normalize_ollama_url`
        # strips everything but scheme+host[:port]), so the full origin is
        # safe to publish verbatim -- unlike a full URL, which could smuggle a
        # path or query string.
        "endpoint_origin": row.endpoint_origin,
        "initiated_by": row.initiated_by,
        # Null for local-server pulls.
        "artifact": artifact,
    }
