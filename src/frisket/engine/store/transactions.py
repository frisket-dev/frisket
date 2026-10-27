"""Shared transaction boundaries for synchronous store reads."""

from collections.abc import Iterator
from contextlib import contextmanager
from sqlite3 import Connection


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
