"""A project write that fails never leaves the transaction it opened behind.

The per-thread project connection begins a transaction implicitly before
its first write. When that write is refused, the connection ends the
transaction the statement opened, so a later owning write on the same
connection can begin its own. A failure inside a transaction that was
already open leaves that transaction to its owner.
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store.transactions import write_transaction


@pytest.fixture
def project(make_project):
    project = make_project("failed-statement")
    project.db.execute("CREATE TABLE probe_rows (id INTEGER PRIMARY KEY)")
    project.db.execute("INSERT INTO probe_rows (id) VALUES (1)")
    project.db.commit()
    try:
        yield project
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def _committed_ids(project) -> list[int]:
    peer = sqlite3.connect(project.db_path)
    try:
        return [row[0] for row in peer.execute("SELECT id FROM probe_rows ORDER BY id")]
    finally:
        peer.close()


def _hold_write_lock(project) -> sqlite3.Connection:
    project.db.execute("PRAGMA busy_timeout=50")
    holder = sqlite3.connect(project.db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_busy_first_write_does_not_leave_a_transaction_for_the_next_owner(project):
    holder = _hold_write_lock(project)
    try:
        with pytest.raises(sqlite3.OperationalError, match="^database is locked$"):
            project.db.execute("INSERT INTO probe_rows (id) VALUES (2)")
    finally:
        holder.rollback()
        holder.close()
        project.db.execute("PRAGMA busy_timeout=10000")

    assert not project.db.in_transaction
    with write_transaction(project.db):
        project.db.execute("INSERT INTO probe_rows (id) VALUES (3)")
    assert _committed_ids(project) == [1, 3]


def test_constraint_failure_on_first_write_ends_its_transaction(project):
    with pytest.raises(
        sqlite3.IntegrityError, match="^UNIQUE constraint failed: probe_rows.id$"
    ):
        project.db.execute("INSERT INTO probe_rows (id) VALUES (1)")

    assert not project.db.in_transaction


def test_partial_executemany_failure_commits_none_of_its_rows(project):
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        project.db.executemany(
            "INSERT INTO probe_rows (id) VALUES (?)", [(2,), (3,), (1,)]
        )

    assert not project.db.in_transaction
    project.db.commit()
    assert _committed_ids(project) == [1]


def test_failure_inside_an_implicit_transaction_leaves_it_to_its_owner(project):
    project.db.execute("INSERT INTO probe_rows (id) VALUES (2)")

    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute("INSERT INTO probe_rows (id) VALUES (1)")

    assert project.db.in_transaction
    project.db.commit()
    assert _committed_ids(project) == [1, 2]


def test_failure_inside_a_caller_transaction_leaves_it_open_and_intact(project):
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("INSERT INTO probe_rows (id) VALUES (2)")

    with pytest.raises(sqlite3.IntegrityError):
        project.db.executemany("INSERT INTO probe_rows (id) VALUES (?)", [(1,)])

    assert project.db.in_transaction
    assert _committed_ids(project) == [1]
    project.db.commit()
    assert _committed_ids(project) == [1, 2]


def test_failure_inside_write_transaction_is_rolled_back_by_its_owner(project):
    with pytest.raises(sqlite3.IntegrityError):
        with write_transaction(project.db):
            project.db.execute("INSERT INTO probe_rows (id) VALUES (2)")
            project.db.execute("INSERT INTO probe_rows (id) VALUES (1)")

    assert not project.db.in_transaction
    assert _committed_ids(project) == [1]


def test_failing_read_opens_nothing(project):
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        project.db.execute("SELECT * FROM missing_table")

    assert not project.db.in_transaction
