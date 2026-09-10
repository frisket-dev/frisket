"""Queued results and reservation interruptions preserve durable truth."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from frisket.contracts.action import ActionError, ActionResult, ActionSpec
from frisket.engine.executor import action_reservations
from frisket.engine.executor.action_jobs import run_action_run_job
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from tests.engine.test_action_queued_policy_action_job import (
    _envelope,
    _reserve_action_job_receipt,
)


class Params(BaseModel):
    pass


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_interruption_after_begin_releases_owned_transaction(
    tmp_path, monkeypatch, failure
):
    project = Project.create(tmp_path / "probe")
    project.add_sheet("Source")
    db = project.db
    original_db = Project.db
    error = failure("interrupted after successful BEGIN")
    before = tuple(db.iterdump())
    action = ActionSpec(kind="test.reserve", params={}, idempotency_key="probe")
    spec = SimpleNamespace(
        params_hash_fn=lambda action: "sha256:probe", reservation_kind="test"
    )
    monkeypatch.setattr(
        action_reservations,
        "_queued_action_reservation_payload",
        lambda *args, **kwargs: {"runner_spec": {}, "_precomputed_output_fields": []},
    )

    class InterruptedConnection:
        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, sql, *args):
            result = db.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                assert db.in_transaction
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
    try:
        with pytest.raises(failure) as caught:
            action_reservations._reserve_queued_action(
                project, action, Params(), spec=spec, project_id="p", commit=True
            )
        assert caught.value is error
        assert not db.in_transaction, (
            "BEGIN acquired a write transaction that was not rolled back"
        )
        db.commit()
        assert tuple(db.iterdump()) == before
    finally:
        db.rollback()
        project.close()


@pytest.mark.parametrize("status", ["needs_confirmation", "running", "queued"])
def test_nonterminal_registered_executor_must_not_publish_completed(tmp_path, status):
    path = tmp_path / "nonterminal.frisket"
    kind = "review.nonterminal"
    _reserve_action_job_receipt(path, "p", kind=kind, receipt_id="receipt-review")
    envelope = _envelope("p", kind=kind, receipt_id="receipt-review")
    registry = HandlerRegistry()
    calls = []

    def executor(project, actual):
        calls.append(actual)
        return ActionResult(
            action={"kind": kind, "action_id": actual.action_id},
            project_id="p",
            receipt_id=actual.receipt_id,
            status=status,
            errors=[ActionError(code="review_not_completed", message="No effect ran.")],
        )

    registry.register_action_executor(kind, executor)
    project = Project(path)
    try:
        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json()},
            executor_lookup=registry.action_executor,
        )
        stored = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        assert len(calls) == 1
        assert (result.status, stored.status) == ("failed", "failed")
        assert result.errors == stored.errors
        assert result.errors[0].code == "action_job_failed"
        assert status in result.errors[0].message
    finally:
        project.close()


@pytest.mark.parametrize(
    "interrupt", [KeyboardInterrupt, SystemExit, asyncio.CancelledError, RuntimeError]
)
@pytest.mark.parametrize("owned", [True, False])
def test_reservation_after_insert_interrupt_respects_transaction_owner(
    tmp_path, monkeypatch, interrupt, owned
):
    project = Project.create(tmp_path / "reservation.frisket")
    sheet_id = project.add_sheet("Before")
    params = Params()
    action = ActionSpec(
        kind="review.reserve", params={}, idempotency_key="review-reserve"
    )
    spec = SimpleNamespace(
        params_hash_fn=lambda action: "sha256:review", reservation_kind="review"
    )
    monkeypatch.setattr(
        action_reservations,
        "_queued_action_reservation_payload",
        lambda *args, **kwargs: {"runner_spec": {}, "_precomputed_output_fields": []},
    )
    inserted = ReceiptStore.insert_queued

    def insert_then_interrupt(self, receipt, *, commit=True):
        inserted(self, receipt, commit=commit)
        assert self.project.db.in_transaction
        raise interrupt("after actual receipt insertion")

    monkeypatch.setattr(ReceiptStore, "insert_queued", insert_then_interrupt)
    try:
        if not owned:
            project.db.execute("BEGIN IMMEDIATE")
            project.db.execute(
                "UPDATE sheets SET name='Caller pending' WHERE id=?", (sheet_id,)
            )
        if interrupt is RuntimeError and owned:
            result = action_reservations._reserve_queued_action(
                project, action, params, spec=spec, project_id="p", commit=owned
            )
            assert result.status == "failed"
            assert result.errors[0].code == "project_write_failed"
        else:
            with pytest.raises(interrupt):
                action_reservations._reserve_queued_action(
                    project, action, params, spec=spec, project_id="p", commit=owned
                )
        pending = project.db.in_transaction
        count = project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        with project.read_snapshot() as reader:
            durable = reader.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        assert durable == 0
        if owned:
            assert (pending, count) == (False, 0)
        else:
            assert (pending, count) == (True, 1)
            assert (
                project.db.execute(
                    "SELECT name FROM sheets WHERE id=?", (sheet_id,)
                ).fetchone()[0]
                == "Caller pending"
            )
    finally:
        project.db.rollback()
        project.close()


def test_reservation_does_not_own_a_transaction_when_begin_fails(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "caller.frisket")
    sheet_id = project.add_sheet("Before")
    action = ActionSpec(kind="test.reserve", params={}, idempotency_key="reserve")
    spec = SimpleNamespace(
        params_hash_fn=lambda action: "sha256:reserve", reservation_kind="test"
    )
    monkeypatch.setattr(
        action_reservations,
        "_queued_action_reservation_payload",
        lambda *args, **kwargs: {"runner_spec": {}, "_precomputed_output_fields": []},
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        project.db.execute(
            "UPDATE sheets SET name='Caller pending' WHERE id=?", (sheet_id,)
        )
        result = action_reservations._reserve_queued_action(
            project, action, Params(), spec=spec, project_id="p", commit=True
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert project.db.in_transaction
        assert (
            project.db.execute(
                "SELECT name FROM sheets WHERE id=?", (sheet_id,)
            ).fetchone()[0]
            == "Caller pending"
        )
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        with project.read_snapshot() as reader:
            assert (
                reader.db.execute(
                    "SELECT name FROM sheets WHERE id=?", (sheet_id,)
                ).fetchone()[0]
                == "Before"
            )
    finally:
        project.db.rollback()
        project.close()
