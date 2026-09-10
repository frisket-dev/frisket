"""Ordered migrations and presence-level validation for the Postgres queue.

The migrator runs under the single ``FRISKET_DATABASE_ADMIN_URL`` owner
principal; the app and worker validate the resulting schema at strict startup
without executing DDL.  Validation is deliberately presence-level (table names,
column names, index names, ledger version) rather than a byte-exact structural
fingerprint.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from frisket.engine.jobs.queue_provision import (
    RUN_QUEUE_DATABASE_NAME,
    RUN_QUEUE_RUNTIME_ROLE,
    QueueProvisionError,
    _endpoint,
    _validate_admin_database_url,
    validate_queue_role_url,
)
from frisket.engine.worker_version import code_version, require_known_code_identity


QUEUE_SCHEMA_VERSION = 9
QUEUE_SCHEMA_MIGRATION_LOCK_ID = 7_154_819_250_847_113_102
QUEUE_SCHEMA_LEDGER = "frisket_queue_schema_migrations"
STORAGE_RECONCILIATION_INDEX = "idx_jobs_storage_project_status_id"
UNIQUE_ACTIVE_DEDUPE_INDEX = "uq_jobs_active_dedupe"

# Presence-level expectations validated at strict startup.
HANDLER_AUTHORITY_TABLE = "job_handler_authorities"
_REQUIRED_TABLES = (
    "jobs",
    "worker_heartbeats",
    "model_pulls",
    HANDLER_AUTHORITY_TABLE,
    QUEUE_SCHEMA_LEDGER,
)
_REQUIRED_JOBS_COLUMNS = (
    "id",
    "kind",
    "payload",
    "status",
    "attempts",
    "max_attempts",
    "available_at",
    "created_at",
    "storage_org_id",
    "project_id",
)
# v3: strict startup must
# verify the model_pulls table's own required shape, not just its presence
# -- a table missing the endpoint-fingerprint/provenance columns or either
# dedupe index is just as unsafe to run product code against as a missing
# table would be.
_REQUIRED_MODEL_PULLS_COLUMNS = (
    "id",
    "workspace_root",
    "model_ref",
    "status",
    "job_id",
    "phase",
    "total_bytes",
    "completed_bytes",
    "error_code",
    "error_message",
    "correlation_id",
    "resolved_digest",
    "resolved_size",
    "cancel_requested_at",
    "created_at",
    "started_at",
    "finished_at",
    "endpoint_id",
    "endpoint_origin",
    "initiated_by",
    # v4: four nullable artifact columns.
    "artifact_kind",
    "artifact_source_url",
    "artifact_license",
    "artifact_manifest_version",
)
_REQUIRED_MODEL_PULLS_INDEXES = (
    "uq_model_pulls_active_ref",
    "uq_model_pulls_active_workspace",
)
_REQUIRED_HANDLER_AUTHORITY_COLUMNS = (
    "authority_id",
    "job_id",
    "worker_id",
    "storage_org_id",
    "project_id",
    "claimed_at",
)

queue_schema_metadata = sa.MetaData()
queue_schema_ledger_table = sa.Table(
    QUEUE_SCHEMA_LEDGER,
    queue_schema_metadata,
    # Migration versions are supplied explicitly, so the ledger has no sequence.
    sa.Column("version", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column(
        "applied_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.clock_timestamp(),
    ),
    sa.Column("applying_code_identity", sa.Text, nullable=False),
)


class QueueSchemaError(RuntimeError):
    """A sanitized queue migration or strict-validation failure."""


@dataclass(frozen=True)
class QueueMigration:
    version: int
    apply: Callable[[Connection], None]


# Columns whose value is derived from a job's payload (the retired
# _ensure_ref_schema's REF_COLUMNS). When v1 adds any of these to a pre-ref
# .queue.db it backfills them from each existing row's payload.
_PAYLOAD_REF_COLUMNS = (
    "org_id",
    "storage_org_id",
    "project_id",
    "run_id",
    "source_id",
    "sheet_id",
    "row_id",
    "receipt_id",
    "action_kind",
    "trace_id",
    "workspace_root",
    "dedupe_key",
)


def _jobs_index_names(connection: Connection) -> set[str]:
    """Index names on ``jobs`` straight from the catalogs. Used instead of
    ``inspector.get_indexes``/``checkfirst`` wherever an index may need
    creating: SQLAlchemy reflection SKIPS expression-based indexes (v7's
    COALESCE dedupe key) on both dialects — with a warning — so
    reflection-based existence checks both misreport and spam logs."""
    if connection.dialect.name == "sqlite":
        rows = connection.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
            )
        ).fetchall()
    else:
        rows = connection.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename='jobs'")
        ).fetchall()
    return {str(row[0]) for row in rows}


def _apply_initial_schema(connection: Connection) -> None:
    # Imported lazily so queue.py can validate without an import cycle.
    from sqlalchemy.schema import CreateColumn

    from frisket.engine.jobs.queue import _job_refs, jobs_metadata, jobs_table

    jobs_metadata.create_all(connection)

    # create_all(checkfirst) never ALTERs an existing table, so an on-disk
    # pre-ledger .queue.db keeps whatever column/index shape it had — anywhere
    # from the original base jobs schema (no ref/version columns) through the
    # full shape the retired _ensure_ref_schema produced. v1 brings any of those
    # up to the current jobs_metadata definition in place: add every missing
    # column (typed exactly as jobs_metadata declares it), create every missing
    # index, and backfill the payload-derived ref columns for existing rows.
    inspector = sa.inspect(connection)
    existing = {str(column["name"]) for column in inspector.get_columns("jobs")}
    added_payload_ref_column = False
    for column in jobs_table.columns:
        if column.name in existing:
            continue
        column_ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.execute(sa.text(f"ALTER TABLE jobs ADD COLUMN {column_ddl}"))
        if column.name in _PAYLOAD_REF_COLUMNS:
            added_payload_ref_column = True
    existing_indexes = _jobs_index_names(connection)
    for index in jobs_table.indexes:
        if index.name == UNIQUE_ACTIVE_DEDUPE_INDEX:
            # v5 creates this one AFTER reconciling any pre-existing duplicate
            # active rows; creating it here against a legacy table could raise
            # on exactly the duplicates v5 exists to clean up. (A brand-new
            # queue still gets it immediately via create_all above.)
            continue
        if index.name not in existing_indexes:
            index.create(bind=connection, checkfirst=False)
    if added_payload_ref_column:
        c = jobs_table.c
        rows = connection.execute(sa.select(c.id, c.kind, c.payload)).fetchall()
        for row in rows:
            try:
                payload = json.loads(row.payload or "{}")
            except (TypeError, ValueError):
                payload = {}
            refs = _job_refs(row.kind, payload if isinstance(payload, dict) else {})
            connection.execute(jobs_table.update().where(c.id == row.id).values(**refs))


def _apply_model_pulls_table(connection: Connection) -> None:
    """v2: the durable ``model_pulls`` table.

    Lazily imported so a genuinely fresh queue's v1 ``create_all`` (which
    shares ``jobs_metadata``) is unaffected by whether this module has been
    imported yet -- the explicit ``.create(checkfirst=True)`` below creates
    the table (and its partial-unique dedupe index) regardless of import
    order, so it is safe whether this runs right after v1 on a brand-new
    queue or, months later, as the only outstanding migration on an existing
    one.
    """
    from frisket.engine.jobs.model_pull_store import model_pulls_table

    model_pulls_table.create(bind=connection, checkfirst=True)


_SUPERSEDED_BY_MIGRATION_MESSAGE = (
    "this pull was automatically failed during a schema upgrade: it was one "
    "of more than one active pull recorded for its workspace, which the "
    "upgraded schema no longer permits"
)

# Terminalizing the LOSER model_pulls
# row alone is not enough -- if its linked job stays 'queued'/'running', a
# worker can still claim and run it, and `mark_running`'s own admin-retry
# allowance (a job_id reclaiming ITS OWN row is allowed 'failed' -> 'running')
# means that job re-attaching to the very row this migration just
# terminalized would reopen it: 'superseded_by_migration' -> 'running' again,
# racing (or outright colliding with) whatever active row actually survived.
# The fix is symmetric with the row-side fence in `mark_running`:
# the job is cancelled here, in the SAME migration transaction, so
# it is never claimable again regardless of which status ('queued' -- never
# claimed yet -- or 'running' -- mid-upgrade, nothing is actually executing
# since the whole migration runs before any worker process using the new
# schema can start) it was in when the migration ran.
_SUPERSEDED_BY_MIGRATION_JOB_MESSAGE = (
    "this job's linked model pull was automatically failed during a schema "
    "upgrade migration (it was one of more than one active pull recorded "
    "for its workspace); the job is cancelled so it can never reopen the "
    "superseded row"
)


def _reconcile_multiple_active_pulls_per_workspace(connection: Connection) -> None:
    """v3 migration pre-step: a legal v2 database
    could have more than one ACTIVE (pending/running) row for the SAME
    workspace with DIFFERENT refs -- v2's TOCTOU (`find_active_for_workspace`
    was a non-atomic pre-check, fixed by v3's own `create_or_get_active`
    atomicity, but that fix does not retroactively repair rows a v2-era
    build already wrote). Creating ``uq_model_pulls_active_workspace``
    (immediately after this function returns) against pre-existing duplicate
    active rows would raise an ``IntegrityError`` and abort the whole
    migration -- an installation with such a database could never start.

    Reconcile FIRST: for every workspace with more than one active row, the
    OLDEST (by ``created_at``, ties broken by the lowest ``id``) stays
    active; every other active row for that workspace is marked failed with
    a canonical, machine-readable reason. Runs against the model_pulls Core
    Table -- by this point in ``_apply_model_pull_hardening`` every column it
    references (``status``/``error_code``/``error_message``/``finished_at``)
    already exists on-disk, whether this is a fresh v2 table or one just
    ALTERed up to shape by the column-adding loop above.

    It also fences the loser's linked job
    (see ``_SUPERSEDED_BY_MIGRATION_JOB_MESSAGE`` above) -- cancelling any
    'queued'/'running' job in the SAME migration transaction so it can never
    later reactivate the row it just lost. Runs against the ``jobs`` Core
    Table, which every migration up to and including v1 has already brought
    to its current column shape by the time v3 runs.
    """
    from frisket.engine.jobs.model_pull_store import ACTIVE_STATUSES, model_pulls_table
    from frisket.engine.jobs.queue import jobs_table

    c = model_pulls_table.c
    active_rows = connection.execute(
        sa.select(c.id, c.workspace_root, c.job_id)
        .where(c.status.in_(ACTIVE_STATUSES))
        .order_by(c.workspace_root, c.created_at, c.id)
    ).fetchall()

    ids_by_workspace: dict[str, list[tuple[int, int | None]]] = {}
    for row in active_rows:
        ids_by_workspace.setdefault(row.workspace_root, []).append((row.id, row.job_id))

    now = datetime.now(UTC)
    jc = jobs_table.c
    for entries in ids_by_workspace.values():
        # `entries` is already ordered oldest-first (the query's ORDER BY)
        # -- entries[0] is the survivor; everything else in this workspace
        # loses, pull row AND its linked job alike.
        for superseded_id, superseded_job_id in entries[1:]:
            connection.execute(
                model_pulls_table.update()
                .where(c.id == superseded_id)
                .values(
                    status="failed",
                    error_code="superseded_by_migration",
                    error_message=_SUPERSEDED_BY_MIGRATION_MESSAGE,
                    finished_at=now,
                )
            )
            if superseded_job_id is not None:
                connection.execute(
                    jobs_table.update()
                    .where(
                        jc.id == superseded_job_id,
                        jc.status.in_(("queued", "running")),
                    )
                    .values(
                        status="cancelled",
                        finished_at=now,
                        locked_by=None,
                        lease_expires_at=None,
                        error=_SUPERSEDED_BY_MIGRATION_JOB_MESSAGE,
                    )
                )


def _apply_model_pull_hardening(connection: Connection) -> None:
    """Migration v3 retrofits an existing
    ``model_pulls``/``worker_heartbeats`` table exactly like v1 retrofits
    ``jobs`` -- add every missing column (typed exactly as the current Core
    Table declares it), reconcile any legal-under-v2 data the new
    workspace-wide uniqueness would reject, then create every missing
    index, checkfirst. A no-op on a brand-new queue, where v2's
    ``model_pulls_table.create()`` already bound the SAME, now-updated Core
    Table object and produced this exact shape in one shot.

    Adds: ``model_pulls.endpoint_origin`` + ``model_pulls.initiated_by``
    (item 3), the ``uq_model_pulls_active_workspace`` partial unique index
    (item 2, reconciled against pre-existing duplicates first -- see
    ``_reconcile_multiple_active_pulls_per_workspace``), and
    ``worker_heartbeats.kinds`` (item 6).
    """
    from sqlalchemy.schema import CreateColumn

    from frisket.engine.jobs.model_pull_store import model_pulls_table
    from frisket.engine.jobs.queue import worker_heartbeats_table

    inspector = sa.inspect(connection)

    existing_pull_columns = {
        str(column["name"]) for column in inspector.get_columns("model_pulls")
    }
    for column in model_pulls_table.columns:
        if column.name in existing_pull_columns:
            continue
        column_ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.execute(sa.text(f"ALTER TABLE model_pulls ADD COLUMN {column_ddl}"))
    _reconcile_multiple_active_pulls_per_workspace(connection)
    for index in model_pulls_table.indexes:
        index.create(bind=connection, checkfirst=True)

    existing_hb_columns = {
        str(column["name"]) for column in inspector.get_columns("worker_heartbeats")
    }
    for column in worker_heartbeats_table.columns:
        if column.name in existing_hb_columns:
            continue
        column_ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.execute(
            sa.text(f"ALTER TABLE worker_heartbeats ADD COLUMN {column_ddl}")
        )


def _apply_artifact_columns(connection: Connection) -> None:
    """Migration v4 adds four nullable artifact-provenance columns
    on ``model_pulls``, retrofitted exactly like v3 added
    ``endpoint_origin``/``initiated_by`` -- add every missing column (typed
    exactly as the current Core Table declares it), checkfirst + additive. A
    no-op on a brand-new queue, where v2's ``model_pulls_table.create()`` bound
    the SAME, now-updated Core Table object and produced this shape in one shot.

    Pure additive: no data reconciliation is needed. A pre-v4 row reads back
    with ``artifact_kind`` NULL, which the store's read convention treats as an
    Ollama pull (``ModelPullRow.effective_artifact_kind``).
    """
    from sqlalchemy.schema import CreateColumn

    from frisket.engine.jobs.model_pull_store import model_pulls_table

    inspector = sa.inspect(connection)
    existing = {str(column["name"]) for column in inspector.get_columns("model_pulls")}
    for column in model_pulls_table.columns:
        if column.name in existing:
            continue
        column_ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.execute(sa.text(f"ALTER TABLE model_pulls ADD COLUMN {column_ddl}"))


_DUPLICATE_ACTIVE_DEDUPE_JOB_MESSAGE = (
    "this job was automatically cancelled during a schema upgrade: it was one "
    "of more than one active job recorded for its (kind, project, dedupe_key), "
    "which the upgraded schema's unique dedupe index no longer permits; the "
    "oldest active job for the key survives"
)


def _apply_unique_active_dedupe_index(connection: Connection) -> None:
    """v5: dedupe_key dedupe becomes enforced.

    Callers dedupe active jobs by (kind, project_id, dedupe_key) with a
    check-then-insert on the non-unique ``idx_jobs_dedupe`` — racy under
    concurrent enqueue. This creates the partial UNIQUE index
    ``uq_jobs_active_dedupe`` (active statuses only, NULL keys exempt) so the
    race loses at insert time; ``enqueue()`` maps the conflict to the existing
    active job's id.

    Reconcile FIRST, exactly like v3's active-pull reconciliation: a legal v4
    database may already hold duplicate active rows for the same key (the
    very race this closes). For each duplicate set the OLDEST row (lowest id)
    stays; every newer active duplicate is cancelled with a canonical,
    machine-readable reason — cancelling a duplicate of still-active work, not
    discarding unique work. Additive and replayable: index creation is
    checkfirst, and a brand-new queue already gets the index from v1's
    ``create_all`` against the current Core Table.
    """
    from frisket.engine.jobs.queue import jobs_table

    c = jobs_table.c
    active = connection.execute(
        sa.select(c.id, c.kind, c.project_id, c.dedupe_key)
        .where(
            c.status.in_(("queued", "running")),
            c.dedupe_key.isnot(None),
            c.project_id.isnot(None),
        )
        .order_by(c.kind, c.project_id, c.dedupe_key, c.id)
    ).fetchall()

    now = datetime.now(UTC)
    survivors: set[tuple[str, str, str]] = set()
    for row in active:
        key = (row.kind, row.project_id, row.dedupe_key)
        if key not in survivors:
            survivors.add(key)
            continue
        connection.execute(
            jobs_table.update()
            .where(c.id == row.id)
            .values(
                status="cancelled",
                finished_at=now,
                locked_by=None,
                lease_expires_at=None,
                error=_DUPLICATE_ACTIVE_DEDUPE_JOB_MESSAGE,
            )
        )

    _recreate_unique_active_dedupe_index(connection)


def _recreate_unique_active_dedupe_index(connection: Connection) -> None:
    """Drop-and-create the enforced dedupe index from the current Core
    definition. Deliberately NOT ``checkfirst``: since v7 the index is
    expression-based (``COALESCE(storage_org_id, -1)``), and SQLAlchemy's
    inspector skips expression indexes during reflection on both dialects —
    ``checkfirst`` would report it absent and then fail on the CREATE.
    ``DROP INDEX IF EXISTS`` + CREATE is idempotent and valid DDL on both
    backends."""
    from frisket.engine.jobs.queue import jobs_table

    connection.execute(sa.text(f"DROP INDEX IF EXISTS {UNIQUE_ACTIVE_DEDUPE_INDEX}"))
    for index in jobs_table.indexes:
        if index.name == UNIQUE_ACTIVE_DEDUPE_INDEX:
            index.create(bind=connection, checkfirst=False)


def _apply_claim_accounting_columns(connection: Connection) -> None:
    """v6, retired: resume-claim accounting (``claim_kind`` /
    ``resume_claims``) on ``jobs``.

    The only writer of ``claim_kind='resume'`` was the run.reconsent
    re-enqueue; ruling 4 ("a retry is a resume") deleted that lane, and the
    dimension went with its producer. This step is now inert.

    It keeps its version NUMBER rather than leaving the tuple: the ledger is
    validated as a contiguous prefix (``_validate_applied_prefix``), so
    dropping an entry would renumber v7/v8 and make every already-migrated
    deployment read as "not contiguous". Deployments that ran the original
    v6 keep two unused, defaulted columns — harmless, since nothing selects
    or inserts them.
    """
    return


def _apply_org_scoped_active_dedupe_index(connection: Connection) -> None:
    """v7: tenant scope for the enforced dedupe key.

    Hosted project ids are per-org slugs (each org's Workspace mints slugs
    inside its own directory), so on the shared hosted run queue two tenants
    can legally hold the same ``(kind, project_id, dedupe_key)`` whenever the
    dedupe key is deterministic (source polls, digests, watches, ...). v5's
    index made that a cross-tenant collision: one tenant's active job blocked
    — or deduped onto — the other's. This rebuilds
    ``uq_jobs_active_dedupe`` as ``(kind, COALESCE(storage_org_id, -1),
    project_id, dedupe_key)`` (see the Core definition in ``queue.py`` for
    why COALESCE rather than the raw nullable column: NULL index keys are
    DISTINCT on both backends, so a bare column would stop enforcing dedupe
    for local/unscoped rows entirely; -1 is unmintable as a real org id).

    No data reconciliation is needed: the new key is a strict superset of the
    old one, so every row set legal under v5/v6 is legal under v7. Drop +
    recreate is replayable — on a brand-new queue v1's ``create_all`` already
    produced the new shape, and recreating it from the same Core Table is a
    no-op in effect.
    """
    _recreate_unique_active_dedupe_index(connection)


def _apply_handler_authority_table(connection: Connection) -> None:
    """v8: active handler authority survives queue-row status/lease changes.

    A cooperative cancel or expired-lease recovery can make a job row look
    terminal/requeued while the original process still executes its handler.
    One row per claim allows overlapping stale and replacement claims to drain
    independently. The worker deletes its exact row only after handler exit.

    A v7 queue can already contain a RUNNING project-scoped row when this
    migration starts. That row is the only durable evidence its old worker may
    still be executing, so v8 conservatively backfills one authority for it.
    The old worker cannot acknowledge the new token; ordinary status/lease
    changes therefore retain it until the explicit stopped-worker recovery
    path abandons that worker's authorities.
    """
    from frisket.engine.jobs.queue import (
        job_handler_authorities_table,
        jobs_table,
    )

    if connection.dialect.name == "postgresql":
        # Prevent a v7 worker from claiming a project job between the backfill
        # snapshot and the migration commit. Existing claim transactions drain
        # first; their now-RUNNING rows are then included below. SQLite's outer
        # BEGIN IMMEDIATE migration transaction supplies the equivalent fence.
        connection.execute(sa.text("LOCK TABLE jobs IN EXCLUSIVE MODE"))

    job_handler_authorities_table.create(bind=connection, checkfirst=True)
    jobs = jobs_table.c
    authorities = job_handler_authorities_table.c
    running = connection.execute(
        sa.select(
            jobs.id,
            jobs.locked_by,
            jobs.storage_org_id,
            jobs.project_id,
            jobs.locked_at,
            jobs.started_at,
            jobs.created_at,
        ).where(
            jobs.status == "running",
            jobs.project_id.isnot(None),
        )
    ).fetchall()
    migrated_at = datetime.now(UTC)
    for row in running:
        authority_id = f"v8-migration-job-{row.id}"
        if connection.execute(
            sa.select(authorities.authority_id).where(
                authorities.authority_id == authority_id
            )
        ).scalar_one_or_none():
            continue
        worker_id = str(row.locked_by or f"v8-unknown-worker-job-{row.id}")
        claimed_at = row.locked_at or row.started_at or row.created_at or migrated_at
        connection.execute(
            job_handler_authorities_table.insert().values(
                authority_id=authority_id,
                job_id=row.id,
                worker_id=worker_id,
                storage_org_id=row.storage_org_id,
                project_id=row.project_id,
                claimed_at=claimed_at,
            )
        )


_LEGACY_LOCAL_PULL_MESSAGE = (
    "this pull was queued before Frisket recorded stable local endpoint "
    "identities; retry the pull against an explicitly selected endpoint"
)
_LEGACY_LOCAL_PULL_JOB_MESSAGE = (
    "this job was cancelled during a schema upgrade because its local model "
    "endpoint identity cannot be recovered safely; retry the pull"
)


def _apply_model_pull_endpoint_identity(connection: Connection) -> None:
    """v9: add and recover durable local-endpoint identity.

    The a29/v8 table recorded an endpoint origin but no stable endpoint id.
    Terminal history remains valid with a null id. Active legacy local pulls
    cannot be rebound safely: an origin is not an identity, and configuration
    may now contain multiple endpoints. Fail those rows and cancel their
    claimable jobs so the user can retry explicitly.

    A queue created after multi-endpoint support but before this migration may
    already contain endpoint-qualified refs while still carrying a v8 ledger.
    Backfill those ids from their canonical refs. The schema change itself is
    additive and replayable on both states.
    """
    from sqlalchemy.schema import CreateColumn

    from frisket.engine.jobs.model_pull_store import (
        ACTIVE_STATUSES,
        model_pulls_table,
    )
    from frisket.engine.jobs.queue import jobs_table
    from frisket.local_model_ids import parse_local_model_id

    existing = {
        str(column["name"])
        for column in sa.inspect(connection).get_columns("model_pulls")
    }
    endpoint_id_column = model_pulls_table.c.endpoint_id
    if endpoint_id_column.name not in existing:
        column_ddl = str(
            CreateColumn(endpoint_id_column).compile(dialect=connection.dialect)
        )
        connection.execute(sa.text(f"ALTER TABLE model_pulls ADD COLUMN {column_ddl}"))

    c = model_pulls_table.c
    legacy_active_rows: list[tuple[int, int | None, str]] = []
    rows = connection.execute(
        sa.select(
            c.id,
            c.workspace_root,
            c.model_ref,
            c.status,
            c.job_id,
            c.endpoint_id,
            c.endpoint_origin,
        ).with_for_update()
    ).fetchall()
    for row in rows:
        if row.endpoint_id is not None or row.endpoint_origin is None:
            continue
        try:
            endpoint_id, _model = parse_local_model_id(row.model_ref)
        except ValueError:
            if row.status in ACTIVE_STATUSES:
                legacy_active_rows.append((row.id, row.job_id, row.workspace_root))
            continue
        connection.execute(
            model_pulls_table.update()
            .where(c.id == row.id, c.endpoint_id.is_(None))
            .values(endpoint_id=endpoint_id)
        )

    now = datetime.now(UTC)
    jc = jobs_table.c
    for pull_id, job_id, workspace_root in legacy_active_rows:
        connection.execute(
            model_pulls_table.update()
            .where(c.id == pull_id, c.status.in_(ACTIVE_STATUSES))
            .values(
                # a29's mark_running permanently fences cancelled rows; failed
                # rows are deliberately reopenable by the same job on retry.
                status="cancelled",
                error_code="endpoint_identity_missing",
                error_message=_LEGACY_LOCAL_PULL_MESSAGE,
                finished_at=now,
            )
        )
        if job_id is None:
            continue
        job = connection.execute(
            sa.select(jc.id, jc.kind, jc.payload, jc.status)
            .where(jc.id == job_id)
            .with_for_update()
        ).first()
        if job is None or job.kind != "model.pull":
            continue
        try:
            payload = json.loads(job.payload or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or (
            payload.get("pull_id") != pull_id
            or payload.get("workspace_root") != workspace_root
        ):
            continue
        connection.execute(
            jobs_table.update()
            .where(jc.id == job_id, jc.status.in_(("queued", "running")))
            .values(
                status="cancelled",
                finished_at=now,
                locked_by=None,
                lease_expires_at=None,
                error=_LEGACY_LOCAL_PULL_JOB_MESSAGE,
            )
        )


MIGRATIONS = (
    QueueMigration(version=1, apply=_apply_initial_schema),
    QueueMigration(version=2, apply=_apply_model_pulls_table),
    QueueMigration(version=3, apply=_apply_model_pull_hardening),
    QueueMigration(version=4, apply=_apply_artifact_columns),
    QueueMigration(version=5, apply=_apply_unique_active_dedupe_index),
    QueueMigration(version=6, apply=_apply_claim_accounting_columns),
    QueueMigration(version=7, apply=_apply_org_scoped_active_dedupe_index),
    QueueMigration(version=8, apply=_apply_handler_authority_table),
    QueueMigration(version=9, apply=_apply_model_pull_endpoint_identity),
)


def _applied_versions(connection: Connection) -> list[int]:
    return [
        int(value)
        for value in connection.execute(
            sa.select(queue_schema_ledger_table.c.version).order_by(
                queue_schema_ledger_table.c.version
            )
        ).scalars()
    ]


def _validate_applied_prefix(versions: list[int]) -> None:
    if any(version > QUEUE_SCHEMA_VERSION for version in versions):
        raise QueueSchemaError("run-queue schema is newer than this code")
    if versions != list(range(1, len(versions) + 1)):
        raise QueueSchemaError("run-queue migration ledger is not contiguous")


def _grant_runtime_privileges(connection: Connection) -> None:
    """Grant the runtime role queue DML and schema-read access."""
    runtime = RUN_QUEUE_RUNTIME_ROLE
    connection.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {runtime}"))
    connection.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
            "jobs, worker_heartbeats, model_pulls, "
            f"{HANDLER_AUTHORITY_TABLE} TO {runtime}"
        )
    )
    connection.execute(
        sa.text(f"GRANT SELECT ON TABLE {QUEUE_SCHEMA_LEDGER} TO {runtime}")
    )
    connection.execute(
        sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {runtime}")
    )


def apply_pending_migrations(connection: Connection) -> None:
    """Apply outstanding queue migrations on an already-open transaction.

    The caller owns the transaction AND its serialization: the Postgres
    ``migrate`` CLI wraps this in a session advisory lock; the SQLite backend
    runs it inline at ``open_queue()`` inside a ``BEGIN IMMEDIATE`` write
    transaction (SQLite's advisory-lock equivalent). Both backends therefore
    share ONE ordered version ledger — a real pre-ledger ``.queue.db`` is
    upgraded in place (``create_all`` is checkfirst; existing rows survive) and
    each applied version is recorded in ``frisket_queue_schema_migrations``.

    ``applied_at`` is stamped explicitly (never the Postgres-only
    ``clock_timestamp()`` server default) so the same insert runs on SQLite.
    Identity is best-effort ``code_version()`` — the local tier has no operator
    and must never fail to open its queue just because git identity is absent.
    """
    identity = code_version()
    queue_schema_ledger_table.create(connection, checkfirst=True)
    applied = _applied_versions(connection)
    _validate_applied_prefix(applied)
    for migration in MIGRATIONS[len(applied) :]:
        migration.apply(connection)
        connection.execute(
            queue_schema_ledger_table.insert().values(
                version=migration.version,
                applied_at=datetime.now(UTC),
                applying_code_identity=identity,
            )
        )


def migrate_run_queue_schema(*, admin_url: str, runtime_url: str) -> None:
    """Apply every outstanding queue migration once under a session lock."""
    admin = _validate_admin_database_url(admin_url)
    runtime = validate_queue_role_url(runtime_url, required_role=RUN_QUEUE_RUNTIME_ROLE)
    if _endpoint(admin) != _endpoint(runtime):
        raise QueueProvisionError(
            "database admin and run-queue locators identify different servers"
        )
    identity = require_known_code_identity(
        code_version(), runtime="run-queue schema migration"
    )
    engine = sa.create_engine(admin.set(database=RUN_QUEUE_DATABASE_NAME), future=True)
    try:
        with engine.connect() as connection:
            connection.execute(
                sa.text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": QUEUE_SCHEMA_MIGRATION_LOCK_ID},
            )
            connection.commit()
            try:
                with connection.begin():
                    queue_schema_ledger_table.create(connection, checkfirst=True)
                with connection.begin():
                    applied = _applied_versions(connection)
                _validate_applied_prefix(applied)
                for migration in MIGRATIONS[len(applied) :]:
                    with connection.begin():
                        migration.apply(connection)
                        connection.execute(
                            queue_schema_ledger_table.insert().values(
                                version=migration.version,
                                applying_code_identity=identity,
                            )
                        )
                with connection.begin():
                    _grant_runtime_privileges(connection)
            finally:
                if connection.in_transaction():
                    connection.rollback()
                connection.execute(
                    sa.text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": QUEUE_SCHEMA_MIGRATION_LOCK_ID},
                )
                connection.commit()
    finally:
        engine.dispose()


def validate_queue_schema(engine: sa.engine.Engine) -> None:
    """Read-only presence and version validation; never executes DDL."""
    inspector = sa.inspect(engine)
    if not inspector.has_table(QUEUE_SCHEMA_LEDGER):
        raise QueueSchemaError("run-queue migration ledger is missing")

    with engine.connect() as connection:
        versions = _applied_versions(connection)
    if any(version > QUEUE_SCHEMA_VERSION for version in versions):
        raise QueueSchemaError("run-queue schema is newer than this code")
    if not versions or max(versions) < QUEUE_SCHEMA_VERSION:
        raise QueueSchemaError("run-queue schema is older than this code")
    if versions != list(range(1, QUEUE_SCHEMA_VERSION + 1)):
        raise QueueSchemaError("run-queue migration ledger is not contiguous")

    table_names = set(inspector.get_table_names())
    missing_tables = [name for name in _REQUIRED_TABLES if name not in table_names]
    if missing_tables:
        raise QueueSchemaError(
            f"run-queue schema is missing tables: {', '.join(missing_tables)}"
        )

    jobs_columns = {str(column["name"]) for column in inspector.get_columns("jobs")}
    missing_columns = [
        name for name in _REQUIRED_JOBS_COLUMNS if name not in jobs_columns
    ]
    if missing_columns:
        raise QueueSchemaError(
            f"run-queue jobs schema is missing columns: {', '.join(missing_columns)}"
        )

    # Index presence via the catalogs (see _jobs_index_names): reflection
    # would report v7's expression-based dedupe index missing on a perfectly
    # healthy schema.
    with engine.connect() as connection:
        index_names = _jobs_index_names(connection)
    if STORAGE_RECONCILIATION_INDEX not in index_names:
        raise QueueSchemaError(
            "run-queue jobs schema is missing the storage reconciliation index"
        )
    if UNIQUE_ACTIVE_DEDUPE_INDEX not in index_names:
        raise QueueSchemaError(
            "run-queue jobs schema is missing the unique active dedupe index"
        )

    # v3: model_pulls' own required shape, not just its presence.
    model_pulls_columns = {
        str(column["name"]) for column in inspector.get_columns("model_pulls")
    }
    missing_model_pulls_columns = [
        name
        for name in _REQUIRED_MODEL_PULLS_COLUMNS
        if name not in model_pulls_columns
    ]
    if missing_model_pulls_columns:
        raise QueueSchemaError(
            "run-queue model_pulls schema is missing columns: "
            + ", ".join(missing_model_pulls_columns)
        )

    model_pulls_index_names = {
        str(index["name"]) for index in inspector.get_indexes("model_pulls")
    }
    missing_model_pulls_indexes = [
        name
        for name in _REQUIRED_MODEL_PULLS_INDEXES
        if name not in model_pulls_index_names
    ]
    if missing_model_pulls_indexes:
        raise QueueSchemaError(
            "run-queue model_pulls schema is missing indexes: "
            + ", ".join(missing_model_pulls_indexes)
        )

    handler_authority_columns = {
        str(column["name"]) for column in inspector.get_columns(HANDLER_AUTHORITY_TABLE)
    }
    missing_handler_authority_columns = [
        name
        for name in _REQUIRED_HANDLER_AUTHORITY_COLUMNS
        if name not in handler_authority_columns
    ]
    if missing_handler_authority_columns:
        raise QueueSchemaError(
            "run-queue handler authority schema is missing columns: "
            + ", ".join(missing_handler_authority_columns)
        )
