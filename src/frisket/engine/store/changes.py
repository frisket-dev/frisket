"""Ordered project mutations. SQLite transactions own every state transition.

An operation may be prepared without changing the project. Its first write
allocates its order, which never changes when a worker publishes later. Closing
publication is irreversible, independently of Undo/Restore visibility.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from statemachine import State, StateMachine


class ChangePublicationClosed(ValueError):
    pass


class ChangeLifecycle(StateMachine):
    writing = State(initial=True)
    applied = State()
    inactive = State()

    close = writing.to(applied) | applied.to.itself() | inactive.to.itself()
    revert = writing.to(inactive) | applied.to(inactive) | inactive.to.itself()
    restore = inactive.to(applied) | applied.to.itself()


def start_change(db: sqlite3.Connection, op_id: int, *, writing: bool = False) -> int:
    """Allocate the order at the first actual mutation, never at admission."""
    if not db.in_transaction:
        raise RuntimeError("Change commands require a caller-owned transaction")
    row = db.execute(
        "SELECT status,change_order,publication_closed FROM ops WHERE id=?", (op_id,)
    ).fetchone()
    if row is None or row[0] != "applied":
        raise ChangePublicationClosed(f"Change {op_id} is not applied")
    if row[1] is not None:
        if writing and row[2]:
            raise ChangePublicationClosed(f"Change {op_id} publication is closed")
        return int(row[1])
    db.execute("UPDATE ops SET redo_eligible=0 WHERE status='undone'")
    db.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    db.execute(
        "INSERT INTO meta(key,value) VALUES ('change_sequence','1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
    )
    order = int(
        db.execute("SELECT value FROM meta WHERE key='change_sequence'").fetchone()[0]
    )
    db.execute(
        "UPDATE ops SET change_order=?,publication_closed=? WHERE id=?",
        (order, int(not writing), op_id),
    )
    return order


class ChangeStore:
    def __init__(self, project: Any):
        self.project = project
        self.db = project.db

    def start(self, op_id: int, *, writing: bool = True) -> int:
        return start_change(self.db, op_id, writing=writing)

    def require_publication(self, op_id: int) -> None:
        row = self.db.execute(
            "SELECT status,publication_closed,change_order FROM ops WHERE id=?",
            (op_id,),
        ).fetchone()
        if row is None or row[0] != "applied" or row[1] or row[2] is None:
            raise ChangePublicationClosed(f"Change {op_id} publication is closed")

    def _transition(self, op_id: int, event: str) -> None:
        if not self.db.in_transaction:
            raise RuntimeError("Change commands require a caller-owned transaction")
        row = self.db.execute(
            "SELECT status,publication_closed FROM ops WHERE id=?", (op_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown Change {op_id}")
        state = (
            "inactive" if row[0] != "applied" else ("applied" if row[1] else "writing")
        )
        machine = ChangeLifecycle(start_value=state)
        machine.send(event)
        target = next(iter(machine.configuration)).id
        self.db.execute(
            "UPDATE ops SET status=?,publication_closed=? WHERE id=?",
            (
                "undone" if target == "inactive" else "applied",
                int(target != "writing"),
                op_id,
            ),
        )

    def close(self, op_id: int) -> None:
        self._transition(op_id, "close")

    def revert(self, op_id: int) -> None:
        self._transition(op_id, "revert")
        # A running producer may already be outside SQLite in a provider
        # request. Undo closes its publication fence above and asks the worker
        # to stop at its next cooperative checkpoint. Any call which has
        # already returned is still accounted independently by RunResultStore.
        self.db.execute(
            "UPDATE runs SET cancel_requested_at="
            "COALESCE(cancel_requested_at,datetime('now')) "
            "WHERE op_id=? AND status='running'",
            (int(op_id),),
        )

    def restore(self, op_id: int) -> None:
        self._transition(op_id, "restore")
