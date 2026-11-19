"""The one owner of SQLite transaction boundaries for project stores."""

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from sqlite3 import Connection, Cursor, Error
from typing import TypeAlias

_log = logging.getLogger(__name__)

SQLParameters: TypeAlias = Sequence[object] | Mapping[str, object]


def inserted_rowid(cursor: Cursor) -> int:
    """The rowid of the one row the INSERT just executed on ``cursor`` added."""
    rowid = cursor.lastrowid
    if rowid is None:
        raise RuntimeError("the INSERT reported no inserted rowid")
    return rowid


@contextmanager
def read_transaction(db: Connection) -> Iterator[None]:
    """Keep reads consistent without ending a caller-owned transaction."""
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction:
            db.rollback()


def require_transaction(db: Connection, what: str) -> None:
    """Refuse a write that must land inside its caller's open transaction."""
    if not db.in_transaction:
        raise RuntimeError(f"{what} requires a caller transaction")


@contextmanager
def write_transaction(
    db: Connection, *, commit: bool = True, what: str = "this write"
) -> Iterator[None]:
    """Run one block as a write transaction on ``db``.

    Nesting is explicit: ``commit=True`` owns a new ``BEGIN IMMEDIATE`` and
    refuses to nest (SQLite rejects the BEGIN and an already-open transaction
    is left untouched), while ``commit=False`` joins the caller's open
    transaction, which must exist, and never begins, commits or rolls back.

    An owned transaction commits on clean exit and rolls back on any
    exception, including ``KeyboardInterrupt``, ``SystemExit`` and
    ``CancelledError``, before re-raising.  A block may end an owned
    transaction early with ``db.commit()`` or ``db.rollback()``; the closing
    commit is then a no-op.
    """
    if not commit:
        require_transaction(db, what)
        yield
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def roll_back_failed_work(db: Connection, *, before: str) -> None:
    """End the transaction a failure handler inherited, before its own write.

    A plain statement refused mid-way (``database is locked``) leaves the
    implicit ``BEGIN`` that preceded it open, so the handler's owning write
    would fail at its own ``BEGIN``. Failed work is rolled back, never
    published. Only a handler whose work did not run inside a caller's
    transaction may call this; one that did joins that transaction instead.
    """
    if db.in_transaction:
        _log.warning(
            "failed_work_rolled_back",
            extra={"event": "failed_work_rolled_back", "before": before},
        )
        db.rollback()


def _end_transaction_the_failure_opened(connection: Connection, was_open: bool) -> None:
    # The statement that failed is the only work in a transaction it opened
    # itself, so ending that transaction discards nothing a caller wrote.
    if was_open or not connection.in_transaction:
        return
    try:
        connection.rollback()
    except Error:
        _log.exception(
            "failed_statement_rollback_failed",
            extra={"event": "failed_statement_rollback_failed"},
        )


class StatementScopedConnection(Connection):
    """A connection whose failed statement never leaves its own implicit BEGIN open.

    Python's implicit transaction control issues ``BEGIN`` before the first
    write of a transaction. When that write fails (``database is locked``, a
    constraint) the transaction stays open with nothing in it, and every
    later owning write on the connection fails at its own ``BEGIN``. A
    failing statement that found no transaction open ends the one it began;
    one that ran inside an open transaction leaves it to its owner.
    """

    def execute(self, sql: str, parameters: SQLParameters = (), /) -> Cursor:
        was_open = self.in_transaction
        try:
            return super().execute(sql, parameters)
        except BaseException:
            _end_transaction_the_failure_opened(self, was_open)
            raise

    def executemany(self, sql: str, parameters: Iterable[SQLParameters], /) -> Cursor:
        was_open = self.in_transaction
        try:
            return super().executemany(sql, parameters)
        except BaseException:
            _end_transaction_the_failure_opened(self, was_open)
            raise

    def executescript(self, sql_script: str, /) -> Cursor:
        was_open = self.in_transaction
        try:
            return super().executescript(sql_script)
        except BaseException:
            _end_transaction_the_failure_opened(self, was_open)
            raise
