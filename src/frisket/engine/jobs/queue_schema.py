"""The current run-queue schema: creation, runtime grants and strict validation.

The queue has exactly one supported shape, ``QUEUE_SCHEMA_VERSION``. A fresh
queue is created directly in that shape and stamped in the ledger; app and
worker startup in strict mode only validate it and never execute DDL. Older
queues are not converted step by step: ``frisket.upgrades.queue`` replaces them
once their user work has finished (see that module).

Validation is deliberately presence-level (table names, column names, index
names, ledger version) rather than a byte-exact structural fingerprint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from frisket.engine.jobs.queue_provision import RUN_QUEUE_RUNTIME_ROLE
from frisket.engine.worker_version import code_version


QUEUE_SCHEMA_VERSION = 12
QUEUE_SCHEMA_MIGRATION_LOCK_ID = 7_154_819_250_847_113_102
QUEUE_SCHEMA_LEDGER = "frisket_queue_schema_migrations"
STORAGE_RECONCILIATION_INDEX = "idx_jobs_storage_project_status_id"
UNIQUE_ACTIVE_DEDUPE_INDEX = "uq_jobs_active_dedupe"
HANDLER_AUTHORITY_TABLE = "job_handler_authorities"

_REQUIRED_TABLES = (
    "jobs",
    "worker_heartbeats",
    "model_pulls",
    HANDLER_AUTHORITY_TABLE,
    "search_preparation_batches",
    "search_preparation_children",
    QUEUE_SCHEMA_LEDGER,
)
_REQUIRED_COLUMNS = {
    "jobs": (
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
    ),
    # A table missing the endpoint-fingerprint/provenance columns is just as
    # unsafe to run product code against as a missing table would be.
    "model_pulls": (
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
        "artifact_kind",
        "artifact_source_url",
        "artifact_license",
        "artifact_manifest_version",
    ),
    HANDLER_AUTHORITY_TABLE: (
        "authority_id",
        "job_id",
        "worker_id",
        "storage_org_id",
        "project_id",
        "claimed_at",
    ),
    "search_preparation_batches": (
        "id",
        "workspace_root",
        "storage_org_id",
        "project_id",
        "trigger_receipt_id",
        "import_ref",
        "materialization_job_id",
        "decision_at",
        "decided_by",
        "sheet_id",
        "plan_json",
        "settled_at",
    ),
}
_REQUIRED_MODEL_PULLS_INDEXES = (
    "uq_model_pulls_active_ref",
    "uq_model_pulls_active_workspace",
)

queue_schema_metadata = sa.MetaData()
queue_schema_ledger_table = sa.Table(
    QUEUE_SCHEMA_LEDGER,
    queue_schema_metadata,
    # Versions are supplied explicitly, so the ledger has no sequence.
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
    """A sanitized queue schema creation, upgrade or validation failure."""


def queue_metadata() -> sa.MetaData:
    """``jobs_metadata`` with every queue-owned table registered on it.

    The store modules declare their tables on the shared metadata at import, so
    import them here rather than relying on whoever happened to import first.
    """
    from frisket.engine.jobs import model_pull_store, search_preparation_store  # noqa: F401
    from frisket.engine.jobs.queue import jobs_metadata

    return jobs_metadata


def queue_table_names() -> set[str]:
    """Every table the queue owns, including the ledger.

    A Standalone SQLite file also holds the team schema, so anything that drops
    or inventories queue tables must use exactly this set.
    """
    return set(queue_metadata().tables) | {QUEUE_SCHEMA_LEDGER}


def applied_queue_versions(connection: Connection) -> list[int]:
    return [
        int(value)
        for value in connection.execute(
            sa.select(queue_schema_ledger_table.c.version).order_by(
                queue_schema_ledger_table.c.version
            )
        ).scalars()
    ]


def jobs_index_names(connection: Connection) -> set[str]:
    """Index names on ``jobs`` straight from the catalogs: SQLAlchemy reflection
    skips the expression-based active-dedupe index on both dialects."""
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


def create_current_queue_schema(
    connection: Connection, *, identity: str | None = None
) -> None:
    """Create every queue table in its current shape and stamp the ledger.

    The caller owns the transaction and its serialization. Tables must not
    already exist; ``create_all`` never alters an existing table.
    """
    queue_metadata().create_all(connection)
    queue_schema_ledger_table.create(connection, checkfirst=True)
    connection.execute(
        queue_schema_ledger_table.insert().values(
            version=QUEUE_SCHEMA_VERSION,
            # Explicit, so the same insert runs on SQLite (no clock_timestamp()).
            applied_at=datetime.now(UTC),
            applying_code_identity=identity or code_version(),
        )
    )


def grant_runtime_privileges(connection: Connection) -> None:
    """Grant the hosted runtime role queue DML and schema-read access."""
    runtime = RUN_QUEUE_RUNTIME_ROLE
    tables = ", ".join(sorted(queue_table_names() - {QUEUE_SCHEMA_LEDGER}))
    connection.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {runtime}"))
    connection.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tables} TO {runtime}")
    )
    connection.execute(
        sa.text(f"GRANT SELECT ON TABLE {QUEUE_SCHEMA_LEDGER} TO {runtime}")
    )
    connection.execute(
        sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {runtime}")
    )


def validate_queue_schema(engine: sa.engine.Engine) -> None:
    """Read-only presence and version validation; never executes DDL."""
    inspector = sa.inspect(engine)
    if not inspector.has_table(QUEUE_SCHEMA_LEDGER):
        raise QueueSchemaError("run-queue schema ledger is missing")
    with engine.connect() as connection:
        versions = applied_queue_versions(connection)
        index_names = jobs_index_names(connection)
    current = max(versions, default=0)
    if current > QUEUE_SCHEMA_VERSION:
        raise QueueSchemaError("run-queue schema is newer than this code")
    if current < QUEUE_SCHEMA_VERSION:
        raise QueueSchemaError("run-queue schema is older than this code")

    table_names = set(inspector.get_table_names())
    missing_tables = [name for name in _REQUIRED_TABLES if name not in table_names]
    if missing_tables:
        raise QueueSchemaError(
            f"run-queue schema is missing tables: {', '.join(missing_tables)}"
        )
    for table, required in _REQUIRED_COLUMNS.items():
        present = {str(column["name"]) for column in inspector.get_columns(table)}
        missing = [name for name in required if name not in present]
        if missing:
            raise QueueSchemaError(
                f"run-queue {table} schema is missing columns: {', '.join(missing)}"
            )
    for index in (STORAGE_RECONCILIATION_INDEX, UNIQUE_ACTIVE_DEDUPE_INDEX):
        if index not in index_names:
            raise QueueSchemaError(f"run-queue jobs schema is missing index {index}")
    model_pulls_indexes = {
        str(index["name"]) for index in inspector.get_indexes("model_pulls")
    }
    missing_indexes = [
        name
        for name in _REQUIRED_MODEL_PULLS_INDEXES
        if name not in model_pulls_indexes
    ]
    if missing_indexes:
        raise QueueSchemaError(
            "run-queue model_pulls schema is missing indexes: "
            + ", ".join(missing_indexes)
        )
