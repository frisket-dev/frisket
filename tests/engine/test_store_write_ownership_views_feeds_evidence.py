"""Who owns the transaction around view, alert, feed, embedding-index and
evidence writes.

Each case is read from another connection, which sees committed state only:
an owning write refuses inside a caller's transaction and leaves the caller's
work uncommitted; a joining write refuses without one and rolls back with its
caller; and a write of several statements whose later statement fails leaves
nothing behind, even after the next commit on the same connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import alerts
from frisket.engine.store.feeds import FeedStore

CALLER_SHEET_NAME = "Caller edit"


@pytest.fixture
def project(make_project):
    project = make_project("write-ownership")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    project.add_rows(sheet_id, [{"value": "original"}], {"value": column_id})
    try:
        yield project
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def _sheet_id(project) -> int:
    return int(project.db.execute("SELECT id FROM sheets").fetchone()[0])


def _committed(project, sql, params=()):
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_count(project, table: str) -> int:
    return int(_committed(project, f"SELECT COUNT(*) FROM {table}")[0][0])


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, _sheet_id(project))
    )


def _assert_caller_work_kept_uncommitted(project) -> None:
    assert project.db.in_transaction
    assert (
        _committed(project, "SELECT name FROM sheets ORDER BY id LIMIT 1")[0][0]
        == "Source"
    )


def _assert_nothing_left_behind(project) -> None:
    """No transaction is left open, so a later commit publishes nothing."""
    assert not project.db.in_transaction
    project.db.commit()


def _refuse(project, statement: str, table: str) -> None:
    """Make the next ``statement`` on ``table`` fail on this connection only."""
    project.db.execute(
        f"CREATE TEMP TRIGGER refuse_{statement.lower()}_{table} "
        f"BEFORE {statement} ON main.{table} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


def _view_spec(project) -> dict:
    return {
        "selection": {"scope": {"kind": "sheet", "sheet_id": _sheet_id(project)}},
        "presentation": {},
    }


def _add_view(project, name: str = "Probe view") -> int:
    return project.add_view(name, _view_spec(project))


# -- views and alerts --------------------------------------------------------


def test_view_whose_row_insert_fails_leaves_no_change(project):
    before = _committed_count(project, "ops")
    _refuse(project, "INSERT", "views")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        _add_view(project)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "ops") == before


def test_view_whose_row_update_fails_leaves_no_change(project):
    view_id = _add_view(project)
    before = _committed_count(project, "ops")
    _refuse(project, "UPDATE", "views")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        project.update_view(view_id, name="Renamed")

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "ops") == before


def test_view_whose_row_delete_fails_keeps_its_alert(project):
    view_id = _add_view(project)
    project.add_alert("Probe alert", view_id=view_id)
    _refuse(project, "DELETE", "views")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        project.delete_view(view_id)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "watches") == 1


@pytest.mark.parametrize("write", ["add_alert", "update_alert"])
def test_alert_write_inside_caller_transaction_refuses_and_keeps_it(project, write):
    view_id = _add_view(project)
    alert_id = (
        project.add_alert("Probe alert", view_id=view_id)
        if write == "update_alert"
        else None
    )
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        if alert_id is None:
            project.add_alert("Probe alert", view_id=view_id)
        else:
            project.update_alert(alert_id, name="Renamed")

    _assert_caller_work_kept_uncommitted(project)


def test_alert_row_delete_joins_and_rolls_back_with_its_caller(project):
    alert_id = project.add_alert("Probe alert", view_id=_add_view(project))
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        alerts.delete_alert_rows(project, alert_id)
    _open_caller_transaction(project)

    assert alerts.delete_alert_rows(project, alert_id) is True
    project.db.rollback()

    assert _committed_count(project, "watches") == 1


# -- feeds -------------------------------------------------------------------


def _add_feed(project) -> int:
    return FeedStore(project).add_feed("Probe feed", kind="rss")


OWNING_FEED_WRITES = {
    "add_feed": lambda project, _feed_id: _add_feed(project),
    "delete_feed": lambda project, feed_id: FeedStore(project).delete_feed(feed_id),
    "record_feed_check": lambda project, feed_id: FeedStore(project).record_feed_check(
        feed_id
    ),
}


@pytest.mark.parametrize("write", sorted(OWNING_FEED_WRITES))
def test_owning_feed_write_inside_caller_transaction_refuses_and_keeps_it(
    project, write
):
    feed_id = _add_feed(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        OWNING_FEED_WRITES[write](project, feed_id)

    _assert_caller_work_kept_uncommitted(project)


JOINING_FEED_WRITES = {
    "update_feed": lambda project, feed_id: FeedStore(project).update_feed(
        feed_id, name="Renamed"
    ),
    "start_feed_check": lambda project, feed_id: FeedStore(project).start_feed_check(
        feed_id
    ),
    "finish_feed_check": lambda project, _feed_id: FeedStore(project).finish_feed_check(
        1, status="ok"
    ),
    "add_feed": lambda project, _feed_id: FeedStore(project).add_feed(
        "Joined", commit=False
    ),
    "record_feed_check": lambda project, feed_id: FeedStore(project).record_feed_check(
        feed_id, commit=False
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_FEED_WRITES))
def test_joining_feed_write_requires_caller_transaction(project, write):
    feed_id = _add_feed(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_FEED_WRITES[write](project, feed_id)

    assert not project.db.in_transaction


def test_joined_feed_check_rolls_back_with_its_caller(project):
    feed_id = _add_feed(project)
    _open_caller_transaction(project)

    store = FeedStore(project)
    store.finish_feed_check(store.start_feed_check(feed_id), status="ok")
    project.db.rollback()

    assert _committed_count(project, "source_runs") == 0
    assert _committed(project, "SELECT last_status FROM sources")[0][0] == "never"


def test_feed_check_whose_feed_update_fails_leaves_no_check(project):
    feed_id = _add_feed(project)
    _refuse(project, "UPDATE", "sources")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        FeedStore(project).record_feed_check(feed_id)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "source_runs") == 0


def test_feed_delete_whose_feed_row_delete_fails_leaves_nothing_pending(project):
    feed_id = _add_feed(project)
    _refuse(project, "DELETE", "sources")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        FeedStore(project).delete_feed(feed_id)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "sources") == 1
