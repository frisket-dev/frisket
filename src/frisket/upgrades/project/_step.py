"""Transaction owner for one fixed-endpoint historical schema step."""

from __future__ import annotations
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from frisket.engine.store.disk_capacity import require_disk_headroom
from frisket.engine.store.schema import SCHEMA_DIGEST_META_KEY


_DIGEST_QUERY = "SELECT value FROM meta WHERE key=?"

# Either complete statements executed in order, or a function that changes data
# inside the step's transaction. Neither stamps the digest itself.
StepPayload = tuple[str, ...] | Callable[[sqlite3.Connection], None]
SchemaStep = tuple[str, str, StepPayload]


def schema_digest(db: sqlite3.Connection) -> object | None:
    """Read the stamp outside a transaction; unreadable metadata reads as none.

    The schema fence after the chain supplies the ordinary unknown-bundle refusal.
    """
    try:
        row = db.execute(_DIGEST_QUERY, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    return None if row is None else row[0]


@contextmanager
def schema_step(
    db: sqlite3.Connection, from_digest: str, to_digest: str
) -> Iterator[bool]:
    """Yield whether ``from_digest`` still holds inside ``BEGIN IMMEDIATE``.

    A clean exit stamps ``to_digest`` (only when the step was pending) and
    commits; any exception, including ``BaseException``, rolls back the step.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have completed this step while we waited.
        row = db.execute(_DIGEST_QUERY, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        pending = row is not None and row[0] == from_digest
        yield pending
        if pending:
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (to_digest, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def run_step(
    db: sqlite3.Connection, from_digest: str, to_digest: str, payload: StepPayload
) -> None:
    """Apply one step whose work fits entirely inside its transaction."""
    if schema_digest(db) != from_digest:
        return
    with schema_step(db, from_digest, to_digest) as pending:
        if not pending:
            return
        if callable(payload):
            payload(db)
        else:
            for statement in payload:
                db.execute(statement)


def run_rebuild_step(
    db: sqlite3.Connection,
    from_digest: str,
    to_digest: str,
    payload: Callable[[sqlite3.Connection], None],
    *,
    bundle_path: str | Path | None = None,
) -> None:
    """Apply one step that replaces tables other tables or triggers reference.

    SQLite requires FK enforcement disabled outside the transaction when
    replacing a referenced table. Never rename the old table: that rewrites
    child references. Legacy ALTER permits the brief gap in trigger references.
    With ``bundle_path`` the step first requires room for one old+new copy plus
    the rollback journal/WAL, the conservative peak of a full table rewrite.
    """
    if schema_digest(db) != from_digest:
        return
    if bundle_path is not None:
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        require_disk_headroom(Path(bundle_path).parent, 2 * page_count * page_size)
    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    legacy_alter = int(db.execute("PRAGMA legacy_alter_table").fetchone()[0])
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    try:
        with schema_step(db, from_digest, to_digest) as pending:
            if pending:
                payload(db)
    finally:
        db.execute(f"PRAGMA legacy_alter_table={legacy_alter}")
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")


def ddl_statements(sql: str) -> tuple[str, ...]:
    """Split a DDL script into complete statements, each run with ``execute``.

    ``Connection.executescript`` would commit the caller's open transaction, so
    migration scripts are never run through it.
    """
    statements: list[str] = []
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            statements.append(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete migration DDL")
    return tuple(statements)


def execute_ddl(db: sqlite3.Connection, sql: str) -> None:
    for statement in ddl_statements(sql):
        db.execute(statement)
