"""Owned transaction boundaries must settle writes before propagating interruption."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from frisket.contracts.action import Receipt
from frisket.engine.executor import run_action_spec
from frisket.engine.executor import action_lifecycle as lifecycle
from frisket.engine.executor import action_reservations
from frisket.engine.executor.action_families import exports
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.execution_routes import (
    INSTANCE_PRINCIPAL_META_KEY,
    instance_principal,
)
from test_cluster_receipt_reader import seeded as seeded


FAILURES = [KeyboardInterrupt, SystemExit, asyncio.CancelledError, RuntimeError]
BOUNDARIES = ["perform", "atomic_map"]


class Params(BaseModel):
    pass


@pytest.fixture
def project(make_project):
    project = make_project("interruptions")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "value")
    project.add_rows(sheet, [{"value": "original"}], {"value": column})
    yield project


def _snapshot(project):
    return tuple(project.db.iterdump())


def _action():
    return SimpleNamespace(
        kind="derive.test", idempotency_key="interrupted", params={}, row_scope=None
    )


def _invoke_begin_boundary(project, boundary, monkeypatch):
    action = _action()
    if boundary == "perform":
        return lifecycle._run_in_txn_idempotent_perform(
            project,
            action,
            project_id="p",
            params_hash="hash",
            perform_fn=lambda *_args, **_kwargs: SimpleNamespace(status="completed"),
        )
    monkeypatch.setattr(
        lifecycle,
        "resolve_maprunner_runner_spec",
        lambda *args, **kwargs: (
            {},
            None,
            [],
            SimpleNamespace(consumes_resolution=True),
        ),
    )
    return lifecycle._run_reserved_maprunner_action(
        project,
        action,
        Params(),
        project_id="p",
        router=None,
        map_runner_factory=lambda *_: pytest.fail("runner must not run"),
        runner_spec_fn=lambda _: {},
        resolve_fn=lambda *_: None,
        precheck_fn=lambda *_: None,
        params_hash_fn=lambda _: "hash",
        reserve_fn=lambda *args, **kwargs: pytest.fail("reservation must not run"),
    )


def _interrupt_begin(project, monkeypatch, error, *, after):
    original_db = Project.db
    db = project.db

    class InterruptedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE" and not after:
                raise error
            result = self.cursor.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                assert db.in_transaction
                raise error
            return result

    class InterruptedConnection:
        def __getattr__(self, name):
            return getattr(db, name)

        def cursor(self):
            return InterruptedCursor(db.cursor())

        def execute(self, sql, *args):
            return self.cursor().execute(sql, *args)

    connection = InterruptedConnection()
    monkeypatch.setattr(
        Project,
        "db",
        property(
            lambda self: connection if self is project else original_db.fget(self)
        ),
    )


def _interrupt_savepoint(project, monkeypatch, error, *, after):
    original_db = Project.db
    db = project.db

    class InterruptedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, sql, *args):
            if sql.startswith("SAVEPOINT plain_action_write_") and not after:
                raise error
            result = self.cursor.execute(sql, *args)
            if sql.startswith("SAVEPOINT plain_action_write_"):
                assert db.in_transaction
                if after:
                    raise error
            return result

    class InterruptedConnection:
        def __getattr__(self, name):
            return getattr(db, name)

        def cursor(self):
            return InterruptedCursor(db.cursor())

        def execute(self, sql, *args):
            return self.cursor().execute(sql, *args)

    connection = InterruptedConnection()
    monkeypatch.setattr(
        Project,
        "db",
        property(
            lambda self: connection if self is project else original_db.fget(self)
        ),
    )


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize("failure", FAILURES[:3])
def test_interruption_after_begin_rolls_back_acquired_transaction(
    project, monkeypatch, boundary, failure
):
    instance_principal(project)
    before = _snapshot(project)
    error = failure("interrupted after BEGIN")
    _interrupt_begin(project, monkeypatch, error, after=True)
    with pytest.raises(failure) as caught:
        _invoke_begin_boundary(project, boundary, monkeypatch)
    assert caught.value is error
    assert not project.db.in_transaction
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before


@pytest.mark.parametrize("owner", ["reserve", "delete"])
@pytest.mark.parametrize("failure", FAILURES[:3])
def test_reservation_owner_interruption_after_begin_rolls_back(
    project, monkeypatch, owner, failure
):
    before = _snapshot(project)
    error = failure("interrupted after BEGIN")
    _interrupt_begin(project, monkeypatch, error, after=True)
    with pytest.raises(failure) as caught:
        if owner == "reserve":
            action_reservations._reserve_running_action_receipt(
                project,
                _action(),
                params_hash="hash",
                project_id="p",
                reservation_kind="test_reservation",
            )
        else:
            action_reservations._delete_reserved_action_receipt(
                project, "receipt_missing", action_kind="derive.test"
            )
    assert caught.value is error
    assert not project.db.in_transaction
    assert _snapshot(project) == before


@pytest.mark.parametrize("failure", FAILURES[:3])
def test_cold_principal_cache_insert_interruption_rolls_back(
    project, monkeypatch, failure
):
    project.db.execute("DELETE FROM meta WHERE key=?", (INSTANCE_PRINCIPAL_META_KEY,))
    project.db.commit()
    before = _snapshot(project)
    db = project.db
    original_db = Project.db
    error = failure("interrupted principal cache INSERT")
    inserts = []

    class InterruptedConnection:
        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, sql, *args):
            result = db.execute(sql, *args)
            if (
                sql.startswith("INSERT INTO meta")
                and args[0][0] == INSTANCE_PRINCIPAL_META_KEY
            ):
                assert db.in_transaction
                assert project.get_meta(INSTANCE_PRINCIPAL_META_KEY).startswith(
                    "deployment:"
                )
                inserts.append(args[0])
                raise error
            return result

    connection = InterruptedConnection()
    monkeypatch.setattr(
        Project,
        "db",
        property(
            lambda self: connection if self is project else original_db.fget(self)
        ),
    )
    with pytest.raises(failure) as caught:
        _invoke_begin_boundary(project, "atomic_map", monkeypatch)
    assert caught.value is error
    assert len(inserts) == 1
    assert not db.in_transaction
    assert _snapshot(project) == before
    db.commit()
    assert _snapshot(project) == before


def test_nested_atomic_map_does_not_populate_cold_principal_cache(project, monkeypatch):
    project.db.execute("DELETE FROM meta WHERE key=?", (INSTANCE_PRINCIPAL_META_KEY,))
    project.db.commit()
    committed = _snapshot(project)
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Caller edit'")
    pending = _snapshot(project)
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        _invoke_begin_boundary(project, "atomic_map", monkeypatch)
    assert project.db.in_transaction
    assert project.get_meta(INSTANCE_PRINCIPAL_META_KEY) is None
    assert _snapshot(project) == pending
    project.db.rollback()
    assert _snapshot(project) == committed


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize("failure", [None, *FAILURES[:3]])
def test_failed_begin_preserves_callers_pending_transaction(
    project, monkeypatch, boundary, failure
):
    instance_principal(project)
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Caller edit'")
    before = _snapshot(project)
    if failure is not None:
        error = failure("interrupted before BEGIN")
        if boundary == "perform":
            _interrupt_savepoint(project, monkeypatch, error, after=False)
        else:
            _interrupt_begin(project, monkeypatch, error, after=False)
        with pytest.raises(failure) as caught:
            _invoke_begin_boundary(project, boundary, monkeypatch)
        assert caught.value is error
    elif boundary == "atomic_map":
        with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
            _invoke_begin_boundary(project, boundary, monkeypatch)
    else:
        result = _invoke_begin_boundary(project, boundary, monkeypatch)
        assert result.status == "completed"
    assert project.db.in_transaction
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, *FAILURES])
@pytest.mark.parametrize("caller_owned", [False, True])
def test_failed_begin_cleans_already_staged_export(
    project, tmp_path, monkeypatch, failure, caller_owned
):
    destination = tmp_path / "work-log.md"
    resolved = exports._resolve_work_log(
        project,
        path=str(destination),
        include_receipts=False,
        project_id="p",
    )
    assert resolved.tmp_path.is_file()
    assert resolved.tmp_path.stat().st_size > 0
    if caller_owned:
        project.db.execute("BEGIN IMMEDIATE")
        project.db.execute("UPDATE sheets SET name='Caller edit'")
    before = _snapshot(project)
    error = failure("database is locked")
    if caller_owned:
        _interrupt_savepoint(project, monkeypatch, error, after=False)
    else:
        _interrupt_begin(project, monkeypatch, error, after=False)

    def invoke():
        return lifecycle._run_in_txn_idempotent_perform(
            project,
            _action(),
            project_id="p",
            params_hash="hash",
            perform_fn=lambda *args, **kwargs: pytest.fail(
                "failed BEGIN must not perform"
            ),
            cleanup_resolved_fn=exports._cleanup,
            resolved=resolved,
        )

    if isinstance(error, Exception):
        result = invoke()
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
    else:
        with pytest.raises(failure) as caught:
            invoke()
        assert caught.value is error
    assert not resolved.tmp_path.exists()
    assert not destination.exists()
    assert project.db.in_transaction is caller_owned
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before


@pytest.mark.parametrize("after", [False, True])
def test_failed_nested_savepoint_preserves_parent_and_cleans_staged_export(
    project, tmp_path, monkeypatch, after
):
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("SAVEPOINT plain_action_write")
    project.db.execute("UPDATE sheets SET name='Caller edit'")
    pending = _snapshot(project)
    destination = tmp_path / "nested-work-log.md"
    resolved = exports._resolve_work_log(
        project,
        path=str(destination),
        include_receipts=False,
        project_id="p",
    )
    assert resolved.tmp_path.is_file()
    error = RuntimeError("interrupted around SAVEPOINT")
    _interrupt_savepoint(project, monkeypatch, error, after=after)

    result = lifecycle._run_in_txn_idempotent_perform(
        project,
        _action(),
        project_id="p",
        params_hash="hash",
        perform_fn=lambda *args, **kwargs: pytest.fail(
            "failed SAVEPOINT must not perform"
        ),
        cleanup_resolved_fn=exports._cleanup,
        resolved=resolved,
    )

    assert result.status == "failed"
    assert result.errors[0].code == "project_write_failed"
    assert not resolved.tmp_path.exists()
    assert not destination.exists()
    assert project.db.in_transaction
    assert _snapshot(project) == pending
    project.db.rollback()


def test_nested_action_commit_that_escapes_savepoint_fails_loudly(project):
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("SAVEPOINT caller_work")
    project.db.execute("UPDATE sheets SET name='Caller edit'")

    def commit_caller_transaction(cur, **_kwargs):
        cur.execute("UPDATE rows SET position=position+1")
        project.db.commit()
        return SimpleNamespace(status="completed")

    with pytest.raises(RuntimeError, match="escaped its savepoint"):
        lifecycle._run_in_txn_idempotent_perform(
            project,
            _action(),
            project_id="p",
            params_hash="hash",
            perform_fn=commit_caller_transaction,
        )

    assert not project.db.in_transaction
    assert project.db.execute("SELECT name FROM sheets").fetchone()[0] == "Caller edit"


@pytest.mark.parametrize("failure", FAILURES)
def test_cleanup_failure_preserves_interruption_and_ordinary_error_policy(
    project, failure
):
    error = failure("original interruption")
    cleanup_error = OSError("delivery rollback failed")
    cleaned = []

    def perform(cur, **kwargs):
        cur.execute("UPDATE sheets SET name='Partial write'")
        raise error

    def cleanup(resolved):
        assert not project.db.in_transaction
        cleaned.append(resolved)
        raise cleanup_error

    before = _snapshot(project)
    expected = cleanup_error if isinstance(error, Exception) else error
    with pytest.raises(type(expected)) as caught:
        lifecycle._run_in_txn_idempotent_perform(
            project,
            _action(),
            project_id="p",
            params_hash="hash",
            perform_fn=perform,
            cleanup_resolved_fn=cleanup,
            resolved="staged",
        )
    assert caught.value is expected
    assert cleaned == ["staged"]
    assert not project.db.in_transaction
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before


@pytest.mark.parametrize("failure", FAILURES)
def test_owned_transaction_after_write_failure_settles(project, failure):
    action = _action()
    error = failure("interrupted after write")
    cleanup = []
    resolved = object()

    def perform(cur, **kwargs):
        cur.execute("UPDATE sheets SET name='changed'")
        assert project.db.in_transaction
        raise error

    def clean(value):
        assert value is resolved
        assert not project.db.in_transaction
        cleanup.append(value)

    def invoke():
        return lifecycle._run_in_txn_idempotent_perform(
            project,
            action,
            project_id="p",
            params_hash="hash",
            perform_fn=perform,
            cleanup_resolved_fn=clean,
            resolved=resolved,
        )

    before = _snapshot(project)
    if issubclass(failure, Exception):
        result = invoke()
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
    else:
        with pytest.raises(failure) as caught:
            invoke()
        assert caught.value is error
    assert not project.db.in_transaction
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before
    assert len(cleanup) == 1


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("phase", ["reserve", "claims", "precheck", "confirmation"])
def test_atomic_map_preparation_failure_rolls_back_receipt_and_claims(
    project, monkeypatch, phase, failure
):
    instance_principal(
        project
    )  # Installation identity is intentionally outside publication.
    action = _action()
    error = failure("interrupted atomic preparation")
    sheet_id = project.db.execute("SELECT id FROM sheets").fetchone()[0]
    program = SimpleNamespace(consumes_resolution=True)
    fields = [{"name": "result", "type": "text"}]
    monkeypatch.setattr(
        lifecycle,
        "resolve_maprunner_runner_spec",
        lambda *args, **kwargs: (
            {"sheet_id": sheet_id},
            None,
            fields,
            program,
        ),
    )

    def fail_at(current):
        if phase == current:
            assert project.db.in_transaction
            assert project.db.execute("SELECT 1 FROM receipts").fetchone()
            if current != "reserve":
                assert project.db.execute(
                    "SELECT 1 FROM output_column_claims"
                ).fetchone()
            raise error

    def reserve(project, action, *, params_hash, project_id, commit):
        assert commit is False
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id="receipt-interrupted",
                project_id=project_id,
                action_id="act-interrupted",
                action_kind=action.kind,
                idempotency_key=action.idempotency_key,
                params_hash=params_hash,
                status="running",
            ),
            commit=commit,
        )
        fail_at("reserve")
        return {"action_id": "act-interrupted", "receipt_id": "receipt-interrupted"}

    acquire = lifecycle._acquire_output_claims_for_runner

    def acquire_then_interrupt(*args, **kwargs):
        result = acquire(*args, **kwargs)
        assert result is None
        fail_at("claims")
        return result

    monkeypatch.setattr(
        lifecycle, "_acquire_output_claims_for_runner", acquire_then_interrupt
    )

    def no_runner(*args):
        raise AssertionError("preparation failure must not create a runner")

    before = _snapshot(project)
    with pytest.raises(failure) as caught:
        lifecycle._run_reserved_maprunner_action(
            project,
            action,
            Params(),
            project_id="p",
            router=None,
            map_runner_factory=no_runner,
            runner_spec_fn=lambda _: {},
            resolve_fn=lambda *_: None,
            precheck_fn=lambda *args, **kwargs: fail_at("precheck"),
            reserve_fn=reserve,
            params_hash_fn=lambda _: "hash",
            confirmed_fn=lambda _: fail_at("confirmation"),
        )
    assert caught.value is error
    assert not project.db.in_transaction
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before


@pytest.mark.parametrize("failure", FAILURES[:3])
def test_typed_entity_publication_interruption_rolls_back_complete_write(
    seeded, monkeypatch, failure
):
    project, _, _, _, source_receipt = seeded
    error = failure("interrupted after entity receipt")
    insert = ReceiptStore.insert_completed

    def insert_then_interrupt(self, receipt, **kwargs):
        insert(self, receipt, **kwargs)
        assert project.db.in_transaction
        raise error

    monkeypatch.setattr(ReceiptStore, "insert_completed", insert_then_interrupt)
    before = _snapshot(project)
    with pytest.raises(failure) as caught:
        run_action_spec(
            project,
            {
                "action_id": "resolve.entities",
                "scope": {"kind": "project"},
                "sheet_name": "Entities",
                "idempotency_key": "interrupted-entities",
                "params": {
                    "source": {"kind": "cluster_values", "receipt_id": source_receipt}
                },
            },
            project_id="p",
        )
    assert caught.value is error
    assert not project.db.in_transaction
    assert _snapshot(project) == before
    project.db.commit()
    assert _snapshot(project) == before
