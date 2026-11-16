"""Bring a run queue to the current schema.

There is no step-by-step conversion. A missing queue is created in the current
shape, a current queue is left exactly as it is, and an older queue is replaced:
its tables are dropped and recreated empty once no user work is left in it.

The queue holds coordination, not results. Saved results, receipts and money
live in project and control-plane stores, so finished job history can go.
Background maintenance (search indexing, file-metadata probes, freshness
checks, scheduled alert checks and digests) is re-enqueued from durable
project/workspace state, so it is discarded rather than waited for; a running
cooperative handler stops when its job row disappears. Everything else a user
started must finish or be cancelled first, by the version that queued it.

Job ids are referenced from project stores (execution attempts, output claims,
notification requests), so the replacement continues numbering above the
retired queue's high-water and an old id can never name a new job.
"""

from __future__ import annotations

from collections import Counter

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
from frisket.engine.jobs.queue_schema import (
    HANDLER_AUTHORITY_TABLE,
    QUEUE_SCHEMA_LEDGER,
    QUEUE_SCHEMA_MIGRATION_LOCK_ID,
    QUEUE_SCHEMA_VERSION,
    QueueSchemaError,
    applied_queue_versions,
    create_current_queue_schema,
    grant_runtime_privileges,
    queue_metadata,
    queue_schema_ledger_table,
    queue_table_names,
)
from frisket.engine.worker_version import code_version, require_known_code_identity


# Re-derived from durable state after the queue is replaced; never user work.
REDERIVABLE_JOB_KINDS = frozenset(
    {
        "search.index",
        "blob.metadata.backfill",
        "source.verify",
        "watch.evaluate",
        "notification.digest",
    }
)


class QueueUpgradeBlocked(QueueSchemaError):
    """An older queue still holds user work; nothing was changed."""


def ensure_current_queue_schema(
    connection: Connection, *, identity: str | None = None
) -> None:
    """Create, keep or replace the queue on an open, serialized transaction.

    The caller owns the transaction AND its serialization: hosted migration
    holds a session advisory lock; SQLite opens under the queue's
    initialization lock inside ``BEGIN IMMEDIATE``.
    """
    if connection.dialect.name == "postgresql":
        # Every process that may create or replace the queue serializes here,
        # before reading the ledger: the hosted migrator already holds this
        # key for its session (advisory locks are re-entrant), and
        # initialize-mode opens take it for their transaction.
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": QUEUE_SCHEMA_MIGRATION_LOCK_ID},
        )
    tables = set(sa.inspect(connection).get_table_names())
    present = tables & queue_table_names()
    if not present:
        create_current_queue_schema(connection, identity=identity)
        return
    if QUEUE_SCHEMA_LEDGER in tables:
        version = max(applied_queue_versions(connection), default=0)
        if version > QUEUE_SCHEMA_VERSION:
            raise QueueSchemaError("run-queue schema is newer than this code")
        if version == QUEUE_SCHEMA_VERSION:
            return
    _replace_older_queue(connection, present, identity=identity)


def _replace_older_queue(
    connection: Connection, present: set[str], *, identity: str | None
) -> None:
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        # Old apps and workers keep running until the new release replaces
        # them; stop them claiming jobs or starting pulls and preparations
        # between the user-work check and the replacement.
        connection.execute(
            sa.text(f"LOCK TABLE {', '.join(sorted(present))} IN EXCLUSIVE MODE")
        )
    blockers = _unfinished_user_work(connection, present)
    if blockers:
        detail = ", ".join(
            f"{count} {kind}" for kind, count in sorted(blockers.items())
        )
        raise QueueUpgradeBlocked(
            "This run queue was created by an older Frisket and still has "
            f"unfinished work ({detail}). Finish or cancel it with the version "
            "that started it, then upgrade again."
        )
    high_water = _job_id_high_water(connection, present, postgres=postgres)
    queue_metadata().drop_all(connection, checkfirst=True)
    queue_schema_ledger_table.drop(connection, checkfirst=True)
    create_current_queue_schema(connection, identity=identity)
    if high_water:
        _continue_job_ids_after(connection, high_water, postgres=postgres)


def _columns(connection: Connection, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(connection).get_columns(table)}


def _unfinished_user_work(connection: Connection, present: set[str]) -> Counter[str]:
    """Counts of user work that must not be dropped, by kind."""
    blockers: Counter[str] = Counter()
    if "jobs" in present:
        for kind, count in connection.execute(
            sa.text(
                "SELECT kind, COUNT(*) FROM jobs WHERE status IN ('queued', 'running') "
                "GROUP BY kind"
            )
        ):
            if str(kind) not in REDERIVABLE_JOB_KINDS:
                blockers[str(kind)] += int(count)
    if HANDLER_AUTHORITY_TABLE in present and "jobs" in present:
        # A handler can outlive its row's status (cancel, lease recovery).
        # Any authority not provably owned by a rederivable job is live work.
        kinds = ", ".join(f"'{kind}'" for kind in sorted(REDERIVABLE_JOB_KINDS))
        live = connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {HANDLER_AUTHORITY_TABLE} authority "
                "LEFT JOIN jobs ON jobs.id = authority.job_id "
                f"WHERE jobs.kind IS NULL OR jobs.kind NOT IN ({kinds})"
            )
        ).scalar_one()
        if live:
            blockers["running handler"] += int(live)
    if "model_pulls" in present and "status" in _columns(connection, "model_pulls"):
        pulls = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM model_pulls WHERE status IN ('pending', 'running')"
            )
        ).scalar_one()
        if pulls:
            blockers["model download"] += int(pulls)
    if "search_preparation_batches" in present and "settled_at" in _columns(
        connection, "search_preparation_batches"
    ):
        batches = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM search_preparation_batches WHERE settled_at IS NULL"
            )
        ).scalar_one()
        if batches:
            blockers["search preparation"] += int(batches)
    return blockers


def _job_id_high_water(
    connection: Connection, present: set[str], *, postgres: bool
) -> int:
    if "jobs" not in present:
        return 0
    high = int(
        connection.execute(
            sa.text("SELECT COALESCE(MAX(id), 0) FROM jobs")
        ).scalar_one()
    )
    if postgres:
        sequence = connection.execute(
            sa.text("SELECT pg_get_serial_sequence('jobs', 'id')")
        ).scalar_one()
        if sequence:
            issued = connection.execute(
                sa.text(
                    f"SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM {sequence}"
                )
            ).scalar_one()
            high = max(high, int(issued or 0))
    elif connection.execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'")
    ).first():
        issued = connection.execute(
            sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'")
        ).scalar_one_or_none()
        high = max(high, int(issued or 0))
    return high


def _continue_job_ids_after(
    connection: Connection, high_water: int, *, postgres: bool
) -> None:
    if postgres:
        connection.execute(
            sa.text("SELECT setval(pg_get_serial_sequence('jobs', 'id'), :high, true)"),
            {"high": high_water},
        )
        return
    # jobs is AUTOINCREMENT on SQLite, so the next id is seq + 1 even on an
    # empty table; sqlite_sequence exists once that table has been created.
    connection.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'jobs'"))
    connection.execute(
        sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('jobs', :high)"),
        {"high": high_water},
    )


def migrate_run_queue_schema(*, admin_url: str, runtime_url: str) -> None:
    """Bring the hosted run queue to the current schema once, under a session lock."""
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
                    ensure_current_queue_schema(connection, identity=identity)
                    grant_runtime_privileges(connection)
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
