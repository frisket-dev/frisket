"""Store write transactions settle before any interruption propagates."""

import asyncio
import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.leases import acquire_lease
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.transactions import write_transaction


INTERRUPTS = [KeyboardInterrupt, SystemExit, asyncio.CancelledError]


class _InterruptAfter:
    """Delegate to a connection; raise right after one matching write lands."""

    def __init__(self, db, prefix, error, target=None):
        self._db = db
        self._target = db if target is None else target
        self._prefix = prefix
        self._error = error

    def __getattr__(self, name):
        return getattr(self._target, name)

    def cursor(self):
        return _InterruptAfter(self._db, self._prefix, self._error, self._db.cursor())

    def execute(self, sql, *args):
        result = self._target.execute(sql, *args)
        if sql.startswith(self._prefix):
            assert self._db.in_transaction
            raise self._error
        return result


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "interruptions.frisket")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "value")
    project.add_rows(sheet, [{"value": "original"}], {"value": column})
    try:
        yield project
    finally:
        project.close()


def _snapshot(db):
    return tuple(db.iterdump())


def _interrupt_project_db(project, monkeypatch, prefix, error):
    original_db = Project.db
    db = project.db
    connection = _InterruptAfter(db, prefix, error)
    monkeypatch.setattr(
        Project,
        "db",
        property(
            lambda self: connection if self is project else original_db.fget(self)
        ),
    )
    return db


@pytest.mark.parametrize("failure", INTERRUPTS)
def test_owned_transaction_rolls_back_interrupted_lease(tmp_path, failure):
    db = sqlite3.connect(tmp_path / "leases.db")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE work (id INTEGER PRIMARY KEY, refresh_claim_token TEXT, "
        "refresh_lease_expires_at TEXT)"
    )
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    before = _snapshot(db)
    error = failure("interrupted after the lease UPDATE")

    with pytest.raises(failure) as caught:
        acquire_lease(
            _InterruptAfter(db, "UPDATE work", error),
            table="work",
            id_value=1,
            token_prefix="test",
        )

    assert caught.value is error
    assert not db.in_transaction
    assert _snapshot(db) == before
    db.close()


@pytest.mark.parametrize("failure", INTERRUPTS)
def test_commit_flag_transaction_rolls_back_interrupted_claim(
    project, monkeypatch, failure
):
    sheet_id = project.db.execute("SELECT id FROM sheets").fetchone()[0]
    before = _snapshot(project.db)
    error = failure("interrupted after the claim INSERT")
    db = _interrupt_project_db(
        project, monkeypatch, "INSERT INTO output_column_claims", error
    )

    with pytest.raises(failure) as caught:
        OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id, output_names=["value"], action_kind="derive.test"
        )

    assert caught.value is error
    assert not db.in_transaction
    assert _snapshot(db) == before


@pytest.mark.parametrize("failure", INTERRUPTS)
def test_commit_false_leaves_interrupted_caller_transaction_to_its_owner(
    project, monkeypatch, failure
):
    sheet_id = project.db.execute("SELECT id FROM sheets").fetchone()[0]
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Caller edit'")
    error = failure("interrupted after the claim INSERT")
    db = _interrupt_project_db(
        project, monkeypatch, "INSERT INTO output_column_claims", error
    )

    with pytest.raises(failure):
        OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["value"],
            action_kind="derive.test",
            commit=False,
        )

    assert db.in_transaction
    db.rollback()


@pytest.mark.parametrize("failure", INTERRUPTS)
def test_joined_transaction_rolls_back_interrupted_owned_row(
    project, monkeypatch, failure
):
    sheet_id, column_id = project.db.execute(
        "SELECT sheet_id, id FROM columns"
    ).fetchone()
    before = _snapshot(project.db)
    error = failure("interrupted after the row INSERT")
    db = _interrupt_project_db(project, monkeypatch, "INSERT INTO rows", error)

    with pytest.raises(failure) as caught:
        project.add_row_with_undo(sheet_id, {"value": "new"}, {"value": column_id})

    assert caught.value is error
    assert not db.in_transaction
    assert _snapshot(db) == before


@pytest.mark.parametrize("failure", INTERRUPTS)
def test_project_transaction_scope_rolls_back_on_interruption(project, failure):
    before = _snapshot(project.db)
    error = failure("interrupted inside the scope")

    with pytest.raises(failure) as caught:
        with project.transaction_scope():
            project.db.execute("UPDATE sheets SET name='Partial'")
            raise error

    assert caught.value is error
    assert not project.db.in_transaction
    assert _snapshot(project.db) == before


def test_owned_transaction_refuses_to_nest_and_keeps_callers_work(project):
    db = project.db
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE sheets SET name='Caller edit'")
    pending = _snapshot(db)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        with write_transaction(db):
            pytest.fail("a nested owned transaction must not run")

    assert db.in_transaction
    assert _snapshot(db) == pending
    db.rollback()


def test_joined_transaction_requires_and_never_ends_the_callers(project):
    db = project.db
    with pytest.raises(
        RuntimeError, match="widget write requires a caller transaction"
    ):
        with write_transaction(db, commit=False, what="widget write"):
            pytest.fail("a joined write must not run without a transaction")

    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError):
        with write_transaction(db, commit=False):
            db.execute("UPDATE sheets SET name='Joined'")
            raise ValueError("ordinary failure")
    assert db.in_transaction
    assert db.execute("SELECT name FROM sheets").fetchone()[0] == "Joined"
    db.rollback()


def _running_run(project):
    from frisket.engine.store.runs import RunResultStore

    sheet_id = project.db.execute("SELECT id FROM sheets").fetchone()[0]
    op_id = project.append_op("map", {"recipe": "template"})
    return RunResultStore(project).start_run(op_id, sheet_id, "map.template")


@pytest.mark.parametrize("failure", INTERRUPTS)
def test_finish_run_rolls_back_interrupted_terminal_write(
    project, monkeypatch, failure
):
    from frisket.engine.store.runs import RunResultStore

    run_id = _running_run(project)
    before = _snapshot(project.db)
    error = failure("interrupted after the terminal status UPDATE")
    db = _interrupt_project_db(project, monkeypatch, "UPDATE runs SET status", error)

    with pytest.raises(failure) as caught:
        RunResultStore(project).finish_run(run_id, "completed")

    assert caught.value is error
    assert not db.in_transaction
    assert _snapshot(db) == before
