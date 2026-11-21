"""Transaction ownership for reservations, output claims, attempts,
effect checkpoints and project-run terminalization.

Each case is read from a second connection, which sees committed state only:
an owning write refuses inside a caller's transaction and leaves the caller's
work uncommitted; a joining write refuses without one and rolls back with its
caller; and a multi-statement write whose later statement fails leaves nothing
behind, even after the next commit on the same connection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from frisket.contracts.action import ActionResult, Receipt
from frisket.engine.executor import action_reservations
from frisket.engine.executor.action_inventory import (
    _QueuedActionSpec,
)
from frisket.engine.executor.project_run_terminalization import (
    AbandonedRunRecoveryAuthority,
    CurrentWriterTerminalAuthority,
    terminalize_project_run,
    terminalize_project_run_in_transaction,
)
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointRef,
    EffectCheckpointStore,
)
from frisket.engine.store.generated_column_claims import GeneratedColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction
from frisket.engine.store.writer_fence import WriterFence
from frisket.execution.attempt import (
    AttemptCommitment,
    UnroutedAdmission,
    abandon_stale_dispatching_attempts,
    admit_attempt,
    claim,
    open_attempt,
    set_attempt_state,
    set_attempt_state_in_transaction,
)
from frisket.execution.attempt_authority import (
    AttemptAuthority,
    UnroutedOnlyAuthority,
)
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
)
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)

CALLER_SHEET_NAME = "Caller edit"


class _DummyParams(BaseModel):
    pass


class _DummyProgram:
    name = "test.unrouted"
    consumes_resolution = False
    defer_generation_seal = False

    @staticmethod
    def output_fields(_spec):
        return [{"name": "value", "column_type": "text"}]


@pytest.fixture
def project(make_project):
    project = make_project("reservations-claims-ownership")
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


def _column_id(project) -> int:
    return int(project.db.execute("SELECT id FROM columns").fetchone()[0])


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
    assert not project.db.in_transaction
    project.db.commit()


def _refuse(project, statement: str, table: str) -> None:
    project.db.execute(
        f"CREATE TEMP TRIGGER refuse_{statement.lower()}_{table} "
        f"BEFORE {statement} ON main.{table} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


def _action(key: str = "idem-1") -> SimpleNamespace:
    return SimpleNamespace(
        kind="map.test",
        idempotency_key=key,
        params={},
    )


def _queued_spec() -> _QueuedActionSpec:
    return _QueuedActionSpec(
        kind="map.test",
        params_model=_DummyParams,
        params_hash_fn=lambda _action: "sha256:test",
        reservation_kind="test_queued_reservation",
        queue_job_kind="test_job_enqueued",
        runner_spec_fn=lambda _params: {},
        resolve_fn=lambda _project, _params, _spec: {},
        precheck_fn=lambda *_args, **_kwargs: None,
        payload_codecs=(),
    )


def _seed_run(project) -> int:
    sheet_id = _sheet_id(project)
    op_id = project.append_change("map", {"action_kind": "map.test"})
    return RunResultStore(project).start_run(op_id, sheet_id, "map.test", total_rows=1)


def _authority(project) -> AttemptAuthority:
    return AttemptAuthority(
        project,
        composition=open_execution_composition(
            project, None, ExecutionCompositionContext.direct()
        ),
    )


def _commitment(run_id: int, attempt_id: str) -> AttemptCommitment:
    return AttemptCommitment(
        attempt_id=attempt_id,
        run_id=run_id,
        seq=0,
        identity="unprojectable",
        scope=(1,),
        admission=UnroutedAdmission(),
        cost_basis=OperatorBorneZeroCost(),
        price_card_version=None,
    )


def _running_receipt(receipt_id: str, *, run_id: int | None = None) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        project_id="proj-test",
        action_id="act-test",
        action_kind="map.test",
        idempotency_key=f"idem:{receipt_id}",
        params_hash="sha256:test",
        status="running",
        run_id=run_id,
    )


# -- join-only writes require a caller transaction ---------------------------

JOINING_WRITES = {
    "claims.acquire": lambda project: GeneratedColumnClaimStore(project).acquire(
        sheet_id=_sheet_id(project),
        output_names=["value"],
        action_kind="map.test",
    ),
    "claims.bind_to_run": lambda project: GeneratedColumnClaimStore(project).bind_to_run(
        claim_token="claim:none",
        run_id=1,
    ),
    "claims.renew": lambda project: GeneratedColumnClaimStore(project).renew(
        claim_token="claim:none",
        lease_seconds=60,
    ),
    "claims.abandon_stale": lambda project: GeneratedColumnClaimStore(
        project
    ).abandon_stale_dispatching_attempts(
        run_id=1,
        cutoff="2020-01-01T00:00:00+00:00",
        lease_cutoff="2020-01-01T00:00:00+00:00",
    ),
    "claims.release": lambda project: GeneratedColumnClaimStore(project).release(
        claim_token="claim:none",
    ),
    "claims.release_for_receipt": lambda project: GeneratedColumnClaimStore(
        project
    ).release_for_receipt(
        receipt_id="receipt:none",
        status="failed",
    ),
    "claims.release_for_run_receipt": lambda project: GeneratedColumnClaimStore(
        project
    ).release_for_run_receipt(
        run_id=1,
        receipt_id="receipt:none",
        status="failed",
    ),
    "claims.discard_for_receipt": lambda project: GeneratedColumnClaimStore(
        project
    ).discard_for_receipt(
        receipt_id="receipt:none",
    ),
    "claims.release_stale": lambda project: GeneratedColumnClaimStore(
        project
    ).release_stale(
        claim_id="claim:none",
        reason="test",
    ),
    "attempt.open_attempt": lambda project: open_attempt(
        project,
        run_id=_seed_run(project),
        identity="unprojectable",
        scope=(1,),
    ),
    "attempt.admit_attempt": lambda project: admit_attempt(
        project,
        _commitment(_seed_run(project), "attempt_missing"),
    ),
    "attempt.abandon_stale": lambda project: abandon_stale_dispatching_attempts(
        project,
        _seed_run(project),
    ),
    "attempt.set_attempt_state_in_transaction": lambda project: (
        set_attempt_state_in_transaction(
            project,
            "attempt_missing",
            "halted",
        )
    ),
    "authority.mint_in_transaction": lambda project: _authority(
        project
    ).mint_in_transaction(
        program=_DummyProgram(),
        spec={"action_kind": "map.test"},
        run_id=_seed_run(project),
        scope=(1,),
    ),
    "unrouted_authority.mint_in_transaction": lambda project: UnroutedOnlyAuthority(
        project
    ).mint_in_transaction(
        program=_DummyProgram(),
        spec={"action_kind": "map.test"},
        run_id=_seed_run(project),
        scope=(1,),
    ),
    "checkpoints.account_returned": lambda project: EffectCheckpointStore(
        project.db
    ).account_returned(
        EffectCheckpointRef(
            checkpoint_id="cp-none",
            family="test",
            group_key="1",
            unit_key="1",
            action_kind="map.test",
            identity="id-1",
        ),
        payload={},
        accrue=lambda _cp: 0.0,
    ),
    "reservations.acquire_output_claims": lambda project: (
        action_reservations._acquire_output_claims_for_runner(
            project,
            _action(),
            {"sheet_id": _sheet_id(project)},
            receipt_id="receipt-1",
            output_fields=[{"name": "value", "column_type": "text"}],
        )
    ),
    "reservations.reserve_running_in_txn": lambda project: (
        action_reservations._reserve_running_action_receipt_in_transaction(
            project,
            _action(),
            params_hash="sha256:test",
            project_id="proj-test",
            reservation_kind="test_reservation",
        )
    ),
    "reservations.reserve_queued_in_txn": lambda project: (
        action_reservations._reserve_queued_action_in_transaction(
            project,
            _action(),
            _DummyParams(),
            spec=_queued_spec(),
            project_id="proj-test",
        )
    ),
    "reservations.mark_queued_prepared": lambda project: (
        action_reservations._mark_queued_action_run_prepared(
            project,
            spec=_queued_spec(),
            receipt_id="receipt-none",
            run_id=1,
            attempt_id=None,
            output_fields=[{"name": "value", "column_type": "text"}],
        )
    ),
    "reservations.mark_running_prepared": lambda project: (
        action_reservations._mark_running_action_run_prepared(
            project,
            receipt_id="receipt-none",
            run_id=1,
            attempt_id=None,
            output_fields=[{"name": "value", "column_type": "text"}],
            program=None,
            defer_publication=False,
        )
    ),
    "reservations.finalize_in_txn": lambda project: (
        action_reservations._finalize_reserved_action_receipt_in_transaction(
            project,
            _action(),
            receipt=_running_receipt("receipt-none"),
        )
    ),
    "terminalize.in_transaction": lambda project: (
        terminalize_project_run_in_transaction(
            project,
            run_id=_seed_run(project),
            receipt_id="receipt-none",
            status="cancelled",
            authority=AbandonedRunRecoveryAuthority(require_cancel_intent=False),
        )
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_WRITES))
def test_joining_write_refuses_without_caller_transaction(project, write):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_WRITES[write](project)

    assert not project.db.in_transaction


def test_joined_claim_attempt_and_checkpoint_writes_roll_back_with_caller(project):
    sheet_id = _sheet_id(project)
    run_id = _seed_run(project)
    store = EffectCheckpointStore(project.db)
    ref = EffectCheckpointRef(
        checkpoint_id="cp-rollback",
        family="test",
        group_key=str(run_id),
        unit_key="1",
        action_kind="map.test",
        identity="id-1",
    )
    assert store.reserve(ref) is True
    store.complete(ref, payload={"ok": True})

    _open_caller_transaction(project)
    claims, conflict = GeneratedColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["value"],
        action_kind="map.test",
        claim_token="claim:rollback",
    )
    assert conflict is None and len(claims) == 1
    attempt_id, _seq = open_attempt(
        project,
        run_id=run_id,
        identity="unprojectable",
        scope=(1,),
    )
    admit_attempt(project, _commitment(run_id, attempt_id))
    store.account_returned(
        ref,
        payload={"ok": True},
        accrue=lambda _cp: 0.25,
    )
    project.db.rollback()

    assert _committed_count(project, "generated_column_claims") == 0
    assert _committed_count(project, "execution_attempts") == 0
    assert (
        _committed(
            project,
            "SELECT accounting_persisted FROM effect_checkpoints WHERE id=?",
            ("cp-rollback",),
        )[0][0]
        == 0
    )


# -- owning writes refuse inside a caller's transaction ----------------------


def test_owning_writes_refuse_inside_caller_transaction_and_keep_it(project):
    run_id = _seed_run(project)
    with write_transaction(project.db):
        attempt_id, _seq = open_attempt(
            project,
            run_id=run_id,
            identity="unprojectable",
            scope=(1,),
        )
        admit_attempt(project, _commitment(run_id, attempt_id))

    owning_raising = {
        "set_attempt_state": lambda: set_attempt_state(project, attempt_id, "halted"),
        "finish_current_writer": lambda: GeneratedColumnClaimStore(
            project
        ).finish_current_writer(
            fence=WriterFence.claimless(run_id=run_id, attempt_id=attempt_id),
            attempt_state="halted",
        ),
        "authority.mint": lambda: _authority(project).mint(
            program=_DummyProgram(),
            spec={"action_kind": "map.test"},
            run_id=run_id,
            scope=(1,),
        ),
        "unrouted_authority.mint": lambda: UnroutedOnlyAuthority(project).mint(
            program=_DummyProgram(),
            spec={"action_kind": "map.test"},
            run_id=run_id,
            scope=(1,),
        ),
        "terminalize_project_run": lambda: terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id="receipt-missing",
            status="cancelled",
            authority=AbandonedRunRecoveryAuthority(require_cancel_intent=False),
        ),
    }

    for name, fn in owning_raising.items():
        _open_caller_transaction(project)
        with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
            fn()
        _assert_caller_work_kept_uncommitted(project)
        project.db.rollback()


def test_owning_reservation_helpers_report_failure_without_ending_caller_txn(project):
    _open_caller_transaction(project)
    res_running = action_reservations._reserve_running_action_receipt(
        project,
        _action("idem-running"),
        params_hash="sha256:test",
        project_id="proj-test",
        reservation_kind="test_reservation",
    )
    assert isinstance(res_running, ActionResult)
    assert res_running.errors[0].code == "project_write_failed"
    _assert_caller_work_kept_uncommitted(project)

    res_finalize = action_reservations._finalize_reserved_action_receipt(
        project,
        _action("idem-running"),
        params_hash="sha256:test",
        project_id="proj-test",
        receipt=_running_receipt("receipt-1"),
    )
    assert isinstance(res_finalize, ActionResult)
    assert res_finalize.errors[0].code == "project_write_failed"
    _assert_caller_work_kept_uncommitted(project)
    project.db.rollback()


# -- multi-statement writes roll back atomically on mid-operation failure ----


def test_set_attempt_state_rolls_back_when_run_pointer_update_fails(project):
    run_id = _seed_run(project)
    commitment = _authority(project).mint(
        program=_DummyProgram(),
        spec={"action_kind": "map.test"},
        run_id=run_id,
        scope=(1,),
    )
    claim(project, commitment, fence=WriterFence(claimless_direct_effect=True))
    _refuse(project, "UPDATE", "runs")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        set_attempt_state(project, commitment.attempt_id, "halted")

    _assert_nothing_left_behind(project)
    assert (
        _committed(
            project,
            "SELECT state FROM execution_attempts WHERE id=?",
            (commitment.attempt_id,),
        )[0][0]
        == "dispatching"
    )
    assert (
        _committed(
            project,
            "SELECT current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        )[0][0]
        == commitment.attempt_id
    )


def test_admit_attempt_rolls_back_when_row_authorizations_insert_fails(project):
    run_id = _seed_run(project)
    with write_transaction(project.db):
        attempt_id, seq = open_attempt(
            project,
            run_id=run_id,
            identity="unprojectable",
            scope=(1,),
        )
    priced = PricedCostBasis(
        pricing_key="test:sku",
        unit_rate="0.01",
        estimated_quantity="1",
        quantity_unit="row",
        terms_version="v1",
        quantity_rounding_mode="exact",
        quantity_rounding_decimal_places=None,
        meter_key="rows",
        meter_units_per_quantity_unit="1",
        ceiling_mode="none",
        row_settlement_mode="estimated_successful_rows",
        charge_authority="platform_commercial_terms",
        row_estimate_quantities=((1, "1"),),
    )
    commitment = AttemptCommitment(
        attempt_id=attempt_id,
        run_id=run_id,
        seq=seq,
        identity="unprojectable",
        scope=(1,),
        admission=UnroutedAdmission(),
        cost_basis=priced,
        price_card_version="v1",
    )
    _refuse(project, "INSERT", "attempt_row_authorizations")

    with (
        pytest.raises(sqlite3.IntegrityError, match="refused"),
        write_transaction(project.db),
    ):
        admit_attempt(project, commitment)

    _assert_nothing_left_behind(project)
    assert (
        _committed(
            project,
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        )[0][0]
        == "created"
    )
    assert _committed_count(project, "attempt_row_authorizations") == 0


def test_finish_current_writer_rolls_back_when_claim_release_fails(project):
    sheet_id = _sheet_id(project)
    run_id = _seed_run(project)
    claim_token = "output-claim:atomic-finish"
    with write_transaction(project.db):
        GeneratedColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["value"],
            action_kind="map.test",
            run_id=run_id,
            claim_token=claim_token,
        )
        GeneratedColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
        )
    commitment = _authority(project).mint(
        program=_DummyProgram(),
        spec={"action_kind": "map.test"},
        run_id=run_id,
        scope=(1,),
    )
    claim(
        project,
        commitment,
        fence=WriterFence(run_id=run_id, claim_token=claim_token),
        output_column_ids={_column_id(project)},
    )
    _refuse(project, "UPDATE", "generated_column_claims")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        GeneratedColumnClaimStore(project).finish_current_writer(
            fence=WriterFence.claimed(
                run_id=run_id,
                writer_attempt_id=commitment.attempt_id,
                claim_token=claim_token,
            ),
            attempt_state="effected",
        )

    _assert_nothing_left_behind(project)
    assert (
        _committed(
            project,
            "SELECT state FROM execution_attempts WHERE id=?",
            (commitment.attempt_id,),
        )[0][0]
        == "dispatching"
    )
    assert (
        _committed(
            project,
            "SELECT status FROM generated_column_claims WHERE claim_token=?",
            (claim_token,),
        )[0][0]
        == "active"
    )


def test_cleanup_queued_reservation_rolls_back_claim_discard_when_receipt_delete_fails(
    project,
):
    sheet_id = _sheet_id(project)
    receipt = _running_receipt("receipt-queued-cleanup").model_copy(
        update={"status": "queued"}
    )
    with write_transaction(project.db):
        ReceiptStore(project).insert_queued(receipt)
        GeneratedColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["value"],
            action_kind="map.test",
            receipt_id=receipt.receipt_id,
            claim_token=f"output-claim:{receipt.receipt_id}",
        )
    _refuse(project, "DELETE", "receipts")

    action_reservations._cleanup_queued_action_reservation(
        project, receipt_id=receipt.receipt_id
    )

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "generated_column_claims") == 1
    assert _committed_count(project, "receipts") == 1


def test_terminalize_project_run_rolls_back_atomically_on_claim_release_failure(
    project,
):
    sheet_id = _sheet_id(project)
    run_id = _seed_run(project)
    receipt_id = "receipt-terminal-atomic"
    claim_token = f"output-claim:{receipt_id}"
    with write_transaction(project.db):
        ReceiptStore(project).insert_running_in_transaction(
            _running_receipt(receipt_id, run_id=run_id)
        )
        GeneratedColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["value"],
            action_kind="map.test",
            receipt_id=receipt_id,
            run_id=run_id,
            claim_token=claim_token,
        )
        GeneratedColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
        )
    commitment = _authority(project).mint(
        program=_DummyProgram(),
        spec={"action_kind": "map.test"},
        run_id=run_id,
        scope=(1,),
    )
    claim(
        project,
        commitment,
        fence=WriterFence(run_id=run_id, claim_token=claim_token),
        output_column_ids={_column_id(project)},
    )
    _refuse(project, "UPDATE", "generated_column_claims")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id=receipt_id,
            status="failed",
            authority=CurrentWriterTerminalAuthority(
                writer_attempt_id=commitment.attempt_id,
                claim_token=claim_token,
            ),
        )

    _assert_nothing_left_behind(project)
    assert (
        _committed(project, "SELECT status FROM runs WHERE id=?", (run_id,))[0][0]
        == "running"
    )
    assert (
        _committed(project, "SELECT status FROM receipts WHERE id=?", (receipt_id,))[0][
            0
        ]
        == "running"
    )
    assert (
        _committed(
            project,
            "SELECT state FROM execution_attempts WHERE id=?",
            (commitment.attempt_id,),
        )[0][0]
        == "dispatching"
    )
    assert (
        _committed(
            project,
            "SELECT status FROM generated_column_claims WHERE claim_token=?",
            (claim_token,),
        )[0][0]
        == "active"
    )


def test_abandon_stale_dispatching_attempts_is_atomic_on_run_pointer_failure(project):
    sheet_id = _sheet_id(project)
    run_id = _seed_run(project)
    claim_token = "output-claim:stale-atomic"
    past = datetime.now(timezone.utc) - timedelta(hours=12)
    with write_transaction(project.db):
        GeneratedColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["value"],
            action_kind="map.test",
            run_id=run_id,
            claim_token=claim_token,
            lease_seconds=1,
            now=past,
        )
        GeneratedColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
        )
    commitment = _authority(project).mint(
        program=_DummyProgram(),
        spec={"action_kind": "map.test"},
        run_id=run_id,
        scope=(1,),
    )
    claim(
        project,
        commitment,
        fence=WriterFence(run_id=run_id, claim_token=claim_token),
        output_column_ids={_column_id(project)},
    )
    with write_transaction(project.db):
        project.db.execute(
            "UPDATE execution_attempts SET created_at=? WHERE id=?",
            (past.isoformat(timespec="seconds"), commitment.attempt_id),
        )
    _refuse(project, "UPDATE", "runs")

    with (
        pytest.raises(sqlite3.IntegrityError, match="refused"),
        write_transaction(project.db),
    ):
        abandon_stale_dispatching_attempts(project, run_id)

    _assert_nothing_left_behind(project)
    assert (
        _committed(
            project,
            "SELECT state FROM execution_attempts WHERE id=?",
            (commitment.attempt_id,),
        )[0][0]
        == "dispatching"
    )


def test_acquire_rolls_back_partial_claims_on_second_output_integrity_error(project):
    """A conflict on the 2nd output must roll back the 1st output's insert via
    its savepoint and report the real conflicting claim, not the rolled-back row.
    """
    sheet_id = _sheet_id(project)
    project.add_column(sheet_id, "alpha")
    col_b = project.add_column(sheet_id, "beta_old")

    # Seed an active claim on col_b under its old name, then rename the column
    # to "beta" so acquire(["alpha", "beta"]) passes the initial active-name
    # check, inserts "alpha", and hits idx_generated_column_claims_active_column on "beta".
    with write_transaction(project.db):
        existing_claims, conflict = GeneratedColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["beta_old"],
            action_kind="map.other",
            claim_token="claim:existing-holder",
        )
    assert conflict is None and len(existing_claims) == 1
    existing_claim_id = str(existing_claims[0]["id"])
    with write_transaction(project.db):
        project.db.execute(
            "UPDATE columns SET name='beta' WHERE id=?",
            (col_b,),
        )

    # Outer caller transaction succeeds cleanly after acquire returns conflict.
    with write_transaction(project.db):
        project.db.execute(
            "UPDATE sheets SET name='Caller kept' WHERE id=?",
            (sheet_id,),
        )
        acquired, conflict = GeneratedColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["alpha", "beta"],
            action_kind="map.test",
            claim_token="claim:new-contender",
        )

    assert acquired == []
    assert conflict is not None
    assert str(conflict["id"]) == existing_claim_id
    assert str(conflict["claim_token"]) == "claim:existing-holder"
    # Outer caller work committed, and no orphaned claim for "alpha" leaked.
    assert (
        _committed(project, "SELECT name FROM sheets WHERE id=?", (sheet_id,))[0][0]
        == "Caller kept"
    )
    active = _committed(
        project,
        "SELECT id, output_name, claim_token FROM generated_column_claims WHERE status='active'",
    )
    assert active == [(existing_claim_id, "beta_old", "claim:existing-holder")]
