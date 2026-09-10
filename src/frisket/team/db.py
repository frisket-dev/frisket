"""Small transaction helpers for invariant-preserving team mutations."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from typing import Iterator

import sqlalchemy as sa


def _lock_id(scope: object) -> int:
    digest = hashlib.sha256(repr(scope).encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def locked_transaction(
    engine: sa.Engine, *, lock_scope: object | None = None
) -> Iterator[sa.Connection]:
    cx = engine.connect()
    transaction: sa.Transaction | None = None
    try:
        if cx.dialect.name == "sqlite":
            cx.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            transaction = cx.begin()
            if cx.dialect.name == "postgresql":
                cx.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": _lock_id(lock_scope or ("team-global",))},
                )
        yield cx
        transaction.commit() if transaction is not None else cx.commit()
    except Exception:
        transaction.rollback() if transaction is not None else cx.rollback()
        raise
    finally:
        cx.close()


def atomic_upsert(
    cx: sa.Connection,
    *,
    table: sa.Table,
    values: dict,
    conflict_columns: tuple[str, ...],
    update_values: dict,
) -> None:
    columns = [table.c[name] for name in conflict_columns]
    if cx.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        statement = (
            insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=columns,
                set_=update_values,
            )
        )
    elif cx.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        statement = (
            insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=columns,
                set_=update_values,
            )
        )
    else:
        existing = cx.execute(
            sa.select(table).where(
                *(table.c[name] == values[name] for name in conflict_columns)
            )
        ).first()
        if existing is None:
            cx.execute(table.insert().values(**values))
        else:
            cx.execute(
                table.update()
                .where(*(table.c[name] == values[name] for name in conflict_columns))
                .values(**update_values)
            )
        return
    cx.execute(statement)


__all__ = ["atomic_upsert", "locked_transaction"]
