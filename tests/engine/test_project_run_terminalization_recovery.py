"""Dead-writer recovery regressions for the project-run terminal kernel."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.contracts.action import ActionError, Receipt, ReceiptEvidence
from frisket.engine.executor import project_run_terminalization as terminalizer_module
from frisket.engine.executor.project_run_terminalization import (
    AbandonedAttemptRecoveryAuthority,
    CurrentWriterTerminalAuthority,
    NeverDispatchedRunTerminalAuthority,
    UnclaimedRunTerminalAuthority,
    terminalize_project_run,
)
from frisket.engine.executor.queued_actions import (
    queued_v1_terminal_receipt_transition,
)
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import (
    abandon_stale_dispatching_attempts,
    open_attempt,
)
from frisket.server.run_status import cancel_project_run


PROJECT_ID = "project-terminal-recovery"
ACTION_KIND = "map.regex_extract"


class _NoJobQueue:
    def get(self, _job_id):
        return None

    def get_project_run_job(self, *_args, **_kwargs):
        return None


@dataclass(frozen=True)
class _OrphanedRun:
    run_id: int
    attempt_id: str
    receipt_id: str
    claim_token: str
    checkpoint_id: str | None


@dataclass(frozen=True)
class _NeverDispatchedRun:
    run_id: int
    receipt_id: str
    claim_token: str | None
    attempt_ids: tuple[str, ...]


@pytest.fixture()
def project(tmp_path):
    value = Project.create(
        tmp_path / "terminal-recovery.frisket",
        name="terminal-recovery",
    )
    try:
        yield value
    finally:
        value.close()


def _seed_orphaned_run(
    project: Project,
    *,
    reserved_checkpoint: bool = False,
    include_attempt_evidence: bool = True,
) -> _OrphanedRun:
    sheet_id = project.add_sheet("Source")
    source_column_id = project.add_column(sheet_id, "source", type="text")
    project.add_column(
        sheet_id,
        "extracted",
        type="text",
        ai_generated=True,
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"source": "alpha"}],
        {"source": source_column_id},
    )
    op_id = project.append_op(ACTION_KIND, {"action_kind": "map.regex_extract"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        ACTION_KIND,
        total_rows=1,
        row_ids=[row_id],
    )
    attempt_id = "attempt_dead_writer"
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 0, 'dispatching', 'dead-writer-fixture', ?, "
        "datetime('now', '-7 hours'))",
        (attempt_id, run_id, f"[{row_id}]"),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run_id),
    )
    project.db.commit()

    receipt_id = "receipt_dead_writer"
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id=receipt_id,
            project_id=PROJECT_ID,
            action_id="action_dead_writer",
            action_kind=ACTION_KIND,
            run_id=run_id,
            idempotency_key="dead-writer@sha256:stable",
            params_hash="sha256:dead-writer",
            status="queued",
            evidence=(
                [
                    ReceiptEvidence(
                        ref={
                            "kind": "queued_action_run_prepared",
                            "queue_kind": "project.run",
                            "run_id": run_id,
                            "attempt_id": attempt_id,
                        }
                    )
                ]
                if include_attempt_evidence
                else []
            ),
        )
    )

    claim_token = "custom:opaque-dead-writer-claim"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["extracted"],
        action_kind=ACTION_KIND,
        receipt_id=receipt_id,
        run_id=run_id,
        op_id=op_id,
        claim_token=claim_token,
        lease_seconds=None,
        details={"fixture": "dead-writer-terminal-recovery"},
    )
    assert conflict is None
    assert len(claims) == 1

    checkpoint_id = None
    if reserved_checkpoint:
        checkpoint_id = "checkpoint_dead_writer_reserved"
        assert EffectCheckpointStore(project.db).reserve(
            checkpoint_id,
            family="row_effect",
            group_key=f"run:{run_id}",
            unit_key=f"row:{row_id}",
            action_kind=ACTION_KIND,
            identity="dead-writer-paid-effect",
            authorized_attempt_id=attempt_id,
            run_id=run_id,
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
            payload={
                "possible_external_effect": True,
                "run_id": run_id,
                "attempt_id": attempt_id,
            },
        )

    # Exercise the real sweeper transition: the seven-hour-old dispatch has
    # an active but expired claim, so it becomes abandoned and the run's
    # current-writer pointer is cleared without releasing the orphan claim.
    # Reserving a paid checkpoint renews the claim as part of its fenced
    # transaction, so expire it after that step to model the later dead writer.
    project.db.execute(
        "UPDATE output_column_claims SET lease_expires_at="
        "datetime('now', '-1 hour') WHERE claim_token=?",
        (claim_token,),
    )
    project.db.commit()
    assert abandon_stale_dispatching_attempts(project, run_id) == 1
    assert RunResultStore(project).request_cancel(run_id)
    return _OrphanedRun(
        run_id=run_id,
        attempt_id=attempt_id,
        receipt_id=receipt_id,
        claim_token=claim_token,
        checkpoint_id=checkpoint_id,
    )


def _durable_snapshot(project: Project, orphan: _OrphanedRun) -> dict[str, object]:
    return {
        "run": tuple(
            project.db.execute(
                "SELECT status, finished_at, current_attempt_id, "
                "cancel_requested_at FROM runs WHERE id=?",
                (orphan.run_id,),
            ).fetchone()
        ),
        "attempt": tuple(
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (orphan.attempt_id,),
            ).fetchone()
        ),
        "receipt": tuple(
            project.db.execute(
                "SELECT run_id, status, body FROM receipts WHERE id=?",
                (orphan.receipt_id,),
            ).fetchone()
        ),
        "claim": tuple(
            project.db.execute(
                "SELECT status, released_at, claim_token FROM output_column_claims "
                "WHERE run_id=? AND receipt_id=?",
                (orphan.run_id, orphan.receipt_id),
            ).fetchone()
        ),
        "checkpoints": [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, state, authorized_attempt_id, payload "
                "FROM effect_checkpoints ORDER BY id"
            ).fetchall()
        ],
    }


def _seed_never_dispatched_run(
    project: Project,
    *,
    attempt_count: int,
    attempt_state: str = "created",
    include_claim: bool = True,
    mismatched_claim_receipt: bool = False,
) -> _NeverDispatchedRun:
    sheet_id = project.add_sheet("Never dispatched")
    source_column_id = project.add_column(sheet_id, "source", type="text")
    project.add_column(
        sheet_id,
        "extracted",
        type="text",
        ai_generated=True,
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"source": "alpha"}],
        {"source": source_column_id},
    )
    op_id = project.append_op(ACTION_KIND, {"action_kind": "map.regex_extract"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        ACTION_KIND,
        total_rows=1,
        row_ids=[row_id],
    )
    receipt_id = "receipt_never_dispatched"
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id=receipt_id,
            project_id=PROJECT_ID,
            action_id="action_never_dispatched",
            action_kind=ACTION_KIND,
            run_id=run_id,
            idempotency_key="never-dispatched@sha256:stable",
            params_hash="sha256:never-dispatched",
            status="queued",
        )
    )
    claim_receipt_id = receipt_id
    if mismatched_claim_receipt:
        claim_receipt_id = "receipt_other_producer"
        ReceiptStore(project).insert_queued(
            Receipt(
                receipt_id=claim_receipt_id,
                project_id=PROJECT_ID,
                action_id="action_other_producer",
                action_kind=ACTION_KIND,
                run_id=run_id,
                status="queued",
            )
        )
    claim_token = "output-claim:never-dispatched"
    if include_claim:
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["extracted"],
            action_kind=ACTION_KIND,
            receipt_id=claim_receipt_id,
            run_id=run_id,
            op_id=op_id,
            claim_token=claim_token,
            lease_seconds=None,
            details={"fixture": "never-dispatched-terminalization"},
        )
        assert conflict is None
        assert len(claims) == 1
    else:
        claim_token = None
    attempt_ids = tuple(
        open_attempt(
            project,
            run_id=run_id,
            identity=f"never-dispatched-{seq}",
            scope=(row_id,),
        )[0]
        for seq in range(attempt_count)
    )
    if attempt_state != "created":
        project.db.executemany(
            "UPDATE execution_attempts SET state=? WHERE id=?",
            [(attempt_state, attempt_id) for attempt_id in attempt_ids],
        )
        project.db.commit()
    return _NeverDispatchedRun(
        run_id=run_id,
        receipt_id=receipt_id,
        claim_token=claim_token,
        attempt_ids=attempt_ids,
    )


def _never_dispatched_snapshot(
    project: Project,
    run: _NeverDispatchedRun,
) -> dict[str, object]:
    return {
        "run": tuple(
            project.db.execute(
                "SELECT status, finished_at, current_attempt_id, "
                "cancel_requested_at FROM runs WHERE id=?",
                (run.run_id,),
            ).fetchone()
        ),
        "attempts": [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, state FROM execution_attempts WHERE run_id=? ORDER BY seq",
                (run.run_id,),
            ).fetchall()
        ],
        "receipt": tuple(
            project.db.execute(
                "SELECT run_id, status, body FROM receipts WHERE id=?",
                (run.receipt_id,),
            ).fetchone()
        ),
        "claims": [
            tuple(row)
            for row in project.db.execute(
                "SELECT run_id, receipt_id, action_kind, status, released_at, "
                "claim_token FROM output_column_claims "
                "WHERE run_id=? OR receipt_id=? ORDER BY id",
                (run.run_id, run.receipt_id),
            ).fetchall()
        ],
        "checkpoints": [
            tuple(row)
            for row in project.db.execute(
                "SELECT ec.id, ec.state, ec.authorized_attempt_id, ec.payload "
                "FROM effect_checkpoints ec JOIN execution_attempts a "
                "ON a.id=ec.authorized_attempt_id WHERE a.run_id=? ORDER BY ec.id",
                (run.run_id,),
            ).fetchall()
        ],
    }


def _never_dispatched_consent_error() -> ActionError:
    return ActionError(
        code="consent_missing",
        message="the confirmed source changed before dispatch",
        action_kind=ACTION_KIND,
    )


def test_never_dispatched_created_attempt_converges_failed_tuple(project) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    [attempt_id] = run.attempt_ids

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        action_kind=ACTION_KIND,
        status="failed",
        error=_never_dispatched_consent_error(),
        never_dispatched=True,
    )

    result = transition.terminalization
    assert result.disposition == "terminalized"
    assert result.receipt_disposition == "updated"
    assert result.run_status == "failed"
    assert result.receipt_status == "failed"
    assert transition.action_result.status == "failed"
    assert [error.code for error in transition.action_result.errors] == [
        "consent_missing"
    ]
    run_row = project.db.execute(
        "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
        (run.run_id,),
    ).fetchone()
    assert run_row is not None
    assert run_row["status"] == "failed"
    assert run_row["finished_at"] is not None
    assert run_row["current_attempt_id"] is None
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()[0]
        == "halted"
    )
    stored_receipt = ReceiptStore(project).find_by_id(run.receipt_id)
    assert stored_receipt is not None
    assert stored_receipt.status == "failed"
    parsed_receipt = stored_receipt.parsed()
    assert parsed_receipt.status == "failed"
    assert [error.code for error in parsed_receipt.errors] == ["consent_missing"]
    claim = project.db.execute(
        "SELECT status, released_at, claim_token FROM output_column_claims "
        "WHERE run_id=?",
        (run.run_id,),
    ).fetchone()
    assert claim is not None
    assert claim["claim_token"] == run.claim_token
    assert claim["status"] == "failed"
    assert claim["released_at"] is not None

    converged = _never_dispatched_snapshot(project, run)
    repeated = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        action_kind=ACTION_KIND,
        status="failed",
        error=_never_dispatched_consent_error(),
        never_dispatched=True,
    )
    assert repeated.terminalization.disposition == "already_terminal"
    assert _never_dispatched_snapshot(project, run) == converged


def test_never_dispatched_admitted_attempt_is_halted_without_dispatch(project) -> None:
    run = _seed_never_dispatched_run(
        project,
        attempt_count=1,
        attempt_state="admitted",
    )
    [attempt_id] = run.attempt_ids

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "terminalized"
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()[0]
        == "halted"
    )


def test_never_dispatched_zero_attempts_converges_failed_tuple(project) -> None:
    run = _seed_never_dispatched_run(
        project,
        attempt_count=0,
        include_claim=False,
    )

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "terminalized"
    run_row = project.db.execute(
        "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
        (run.run_id,),
    ).fetchone()
    assert run_row is not None
    assert tuple(run_row) == ("failed", run_row["finished_at"], None)
    assert run_row["finished_at"] is not None
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE run_id=?",
            (run.run_id,),
        ).fetchone()[0]
        == 0
    )
    stored_receipt = ReceiptStore(project).find_by_id(run.receipt_id)
    assert stored_receipt is not None
    assert stored_receipt.status == "failed"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE run_id=?",
            (run.run_id,),
        ).fetchone()[0]
        == 0
    )


def test_never_dispatched_refuses_a_live_current_writer(project) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    [attempt_id] = run.attempt_ids
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run.run_id),
    )
    project.db.commit()
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.receipt_disposition == "write_refused"
    assert result.reason == "never_dispatched_authority_not_proven"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_refuses_dispatching_attempt_without_owner_pointer(
    project,
) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    [attempt_id] = run.attempt_ids
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
        (attempt_id,),
    )
    project.db.commit()
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.reason == "never_dispatched_authority_not_proven"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_refuses_a_dangling_current_writer_pointer(project) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    [attempt_id] = run.attempt_ids
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run.run_id),
    )
    project.db.commit()
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.reason == "never_dispatched_authority_not_proven"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_refuses_ambiguous_open_attempts(project) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=2)
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.receipt_disposition == "write_refused"
    assert result.reason == "never_dispatched_authority_not_proven"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_refuses_a_mismatched_active_claim(project) -> None:
    run = _seed_never_dispatched_run(
        project,
        attempt_count=1,
        mismatched_claim_receipt=True,
    )
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.receipt_disposition == "write_refused"
    assert result.reason == "active_claim_identity_mismatch"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_refuses_an_unbound_active_claim(project) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    project.db.execute(
        "UPDATE output_column_claims SET run_id=NULL WHERE claim_token=?",
        (run.claim_token,),
    )
    project.db.commit()
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.reason == "never_dispatched_claim_run_mismatch"
    assert _never_dispatched_snapshot(project, run) == before


@pytest.mark.parametrize("historical_state", ("effected", "halted", "abandoned"))
def test_never_dispatched_refuses_dispatch_history(
    project,
    historical_state: str,
) -> None:
    run = _seed_never_dispatched_run(
        project,
        attempt_count=1,
        attempt_state=historical_state,
    )
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.reason == "never_dispatched_authority_not_proven"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_reserved_effect_requires_reconciliation(project) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    [attempt_id] = run.attempt_ids
    assert run.claim_token is not None
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run.run_id),
    )
    project.db.commit()
    checkpoint_id = "checkpoint_never_dispatched_reserved"
    assert EffectCheckpointStore(project.db).reserve(
        checkpoint_id,
        family="row_effect",
        group_key=f"run:{run.run_id}",
        unit_key="row:1",
        action_kind=ACTION_KIND,
        identity="never-dispatched-reserved",
        authorized_attempt_id=attempt_id,
        run_id=run.run_id,
        writer_attempt_id=attempt_id,
        claim_token=run.claim_token,
        payload={"possible_external_effect": True},
    )
    project.db.execute(
        "UPDATE execution_attempts SET state='created' WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=NULL WHERE id=?",
        (run.run_id,),
    )
    project.db.commit()
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "reconciliation_required"
    assert result.reason == "reserved_effect_checkpoint"
    assert result.reserved_checkpoint_ids == (checkpoint_id,)
    assert _never_dispatched_snapshot(project, run) == before


@pytest.mark.parametrize("effect_state", ("returned", "consumed"))
def test_never_dispatched_refuses_effect_history(
    project,
    effect_state: str,
) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    [attempt_id] = run.attempt_ids
    assert run.claim_token is not None
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run.run_id),
    )
    project.db.commit()
    checkpoint_id = f"checkpoint_never_dispatched_{effect_state}"
    checkpoints = EffectCheckpointStore(project.db)
    assert checkpoints.reserve(
        checkpoint_id,
        family="row_effect",
        group_key=f"run:{run.run_id}",
        unit_key="row:1",
        action_kind=ACTION_KIND,
        identity=f"never-dispatched-{effect_state}",
        authorized_attempt_id=attempt_id,
        run_id=run.run_id,
        writer_attempt_id=attempt_id,
        claim_token=run.claim_token,
        payload={"possible_external_effect": True},
    )
    project.db.execute(
        "UPDATE effect_checkpoints SET state=?, payload='{}' WHERE id=?",
        (effect_state, checkpoint_id),
    )
    project.db.execute(
        "UPDATE execution_attempts SET state='superseded' WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=NULL WHERE id=?",
        (run.run_id,),
    )
    project.db.commit()
    before = _never_dispatched_snapshot(project, run)

    result = terminalize_project_run(
        project,
        run_id=run.run_id,
        receipt_id=run.receipt_id,
        status="failed",
        authority=NeverDispatchedRunTerminalAuthority(),
        errors=[_never_dispatched_consent_error()],
    )

    assert result.disposition == "conflict"
    assert result.reason == "never_dispatched_effect_history"
    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_rolls_back_when_receipt_update_refuses(
    project,
    monkeypatch,
) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    before = _never_dispatched_snapshot(project, run)
    monkeypatch.setattr(
        ReceiptStore,
        "update_if_status_in",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="receipt postcondition failed"):
        terminalize_project_run(
            project,
            run_id=run.run_id,
            receipt_id=run.receipt_id,
            status="failed",
            authority=NeverDispatchedRunTerminalAuthority(),
            errors=[_never_dispatched_consent_error()],
        )

    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_rolls_back_when_claim_release_refuses(
    project,
    monkeypatch,
) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=1)
    before = _never_dispatched_snapshot(project, run)
    monkeypatch.setattr(
        OutputColumnClaimStore,
        "release_for_run_receipt",
        lambda *_args, **_kwargs: 0,
    )

    with pytest.raises(
        RuntimeError,
        match="terminal claim release refused: expected 1, released 0",
    ):
        terminalize_project_run(
            project,
            run_id=run.run_id,
            receipt_id=run.receipt_id,
            status="failed",
            authority=NeverDispatchedRunTerminalAuthority(),
            errors=[_never_dispatched_consent_error()],
        )

    assert _never_dispatched_snapshot(project, run) == before


def test_never_dispatched_authority_requires_owned_immediate_transaction(
    project,
) -> None:
    run = _seed_never_dispatched_run(project, attempt_count=0)
    before = _never_dispatched_snapshot(project, run)
    project.db.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="owned BEGIN IMMEDIATE"):
            terminalize_project_run(
                project,
                run_id=run.run_id,
                receipt_id=run.receipt_id,
                status="failed",
                authority=NeverDispatchedRunTerminalAuthority(),
                errors=[_never_dispatched_consent_error()],
                commit=False,
            )
    finally:
        project.db.rollback()
    assert _never_dispatched_snapshot(project, run) == before


def test_cancelled_insert_mode_uses_finished_receipt_contract(project) -> None:
    sheet_id = project.add_sheet("Cancelled insert")
    op_id = project.append_op(ACTION_KIND, {"fixture": "cancelled-insert"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        ACTION_KIND,
        total_rows=0,
    )
    assert RunResultStore(project).request_cancel(run_id)
    receipt_id = "receipt_cancelled_insert"
    terminal_receipt = Receipt(
        receipt_id=receipt_id,
        project_id=PROJECT_ID,
        action_id="action_cancelled_insert",
        action_kind=ACTION_KIND,
        run_id=run_id,
        idempotency_key="cancelled-insert:stable",
        params_hash="sha256:cancelled-insert",
        status="cancelled",
    )

    result = terminalize_project_run(
        project,
        run_id=run_id,
        receipt_id=receipt_id,
        status="cancelled",
        authority=UnclaimedRunTerminalAuthority(
            claimless_direct_effect=True,
            require_cancel_intent=True,
        ),
        terminal_receipt=terminal_receipt,
        receipt_source_statuses=None,
    )

    assert result.disposition == "terminalized"
    assert result.receipt_disposition == "inserted"
    stored = ReceiptStore(project).find_by_id(receipt_id)
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.parsed() == terminal_receipt


def test_abandoned_writer_cancel_recovery_converges_terminal_tuple(project) -> None:
    orphan = _seed_orphaned_run(project)

    result = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )

    assert result.disposition == "terminalized"
    assert result.receipt_disposition == "updated"
    assert result.run_status == "cancelled"
    assert result.receipt_status == "cancelled"

    run = project.db.execute(
        "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
        "FROM runs WHERE id=?",
        (orphan.run_id,),
    ).fetchone()
    assert run is not None
    assert run["status"] == "cancelled"
    assert run["finished_at"] is not None
    assert run["current_attempt_id"] is None
    assert run["cancel_requested_at"] is not None
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (orphan.attempt_id,),
        ).fetchone()[0]
        == "abandoned"
    )

    stored_receipt = ReceiptStore(project).find_by_id(orphan.receipt_id)
    assert stored_receipt is not None
    assert stored_receipt.status == "cancelled"
    assert stored_receipt.run_id == orphan.run_id
    assert stored_receipt.parsed().status == "cancelled"
    claim = project.db.execute(
        "SELECT status, released_at, claim_token FROM output_column_claims "
        "WHERE run_id=? AND receipt_id=?",
        (orphan.run_id, orphan.receipt_id),
    ).fetchone()
    assert claim is not None
    assert claim["claim_token"] == orphan.claim_token
    assert claim["status"] == "cancelled"
    assert claim["released_at"] is not None

    converged = _durable_snapshot(project, orphan)
    repeated = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )
    assert repeated.disposition == "already_terminal"
    assert repeated.receipt_disposition == "already_terminal"
    assert _durable_snapshot(project, orphan) == converged


def test_current_writer_retry_refuses_terminal_tuple_with_pending_attempt(
    project,
) -> None:
    orphan = _seed_orphaned_run(project)
    terminalized = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )
    assert terminalized.disposition == "terminalized"

    pending_attempt_id = "attempt_pending_after_terminal_split"
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 1, 'admitted', 'pending-after-terminal', '[]', "
        "datetime('now'))",
        (pending_attempt_id, orphan.run_id),
    )
    project.db.commit()

    repeated = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=CurrentWriterTerminalAuthority(
            writer_attempt_id=orphan.attempt_id,
            claim_token=orphan.claim_token,
        ),
    )

    assert repeated.disposition == "conflict"
    assert repeated.reason is not None
    assert "attempt" in repeated.reason
    pending = project.db.execute(
        "SELECT state FROM execution_attempts WHERE id=?",
        (pending_attempt_id,),
    ).fetchone()
    assert pending is not None
    assert pending["state"] == "admitted"


def test_abandoned_writer_reserved_effect_requires_reconciliation(project) -> None:
    orphan = _seed_orphaned_run(project, reserved_checkpoint=True)
    before = _durable_snapshot(project, orphan)

    result = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )

    assert result.disposition == "reconciliation_required"
    assert result.receipt_disposition == "write_refused"
    assert result.run_status == "running"
    assert result.receipt_status == "queued"
    assert result.reserved_checkpoint_ids == (orphan.checkpoint_id,)
    assert result.reason == "reserved_effect_checkpoint"
    assert _durable_snapshot(project, orphan) == before

    repeated = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )
    assert repeated == result
    assert _durable_snapshot(project, orphan) == before


def test_swept_run_without_attempt_receipt_evidence_converges(project) -> None:
    orphan = _seed_orphaned_run(project, include_attempt_evidence=False)

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="cancelled",
        receipt_id=orphan.receipt_id,
        action_kind=ACTION_KIND,
        require_cancel_intent=True,
    )

    assert transition.terminalization.disposition == "terminalized"
    snapshot = _durable_snapshot(project, orphan)
    assert snapshot["run"][0] == "cancelled"
    assert snapshot["attempt"][0] == "abandoned"
    assert snapshot["receipt"][1] == "cancelled"
    assert snapshot["claim"][0] == "cancelled"


def test_swept_run_without_attempt_evidence_retains_reserved_effect(project) -> None:
    orphan = _seed_orphaned_run(
        project,
        reserved_checkpoint=True,
        include_attempt_evidence=False,
    )
    before = _durable_snapshot(project, orphan)

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="cancelled",
        receipt_id=orphan.receipt_id,
        action_kind=ACTION_KIND,
        require_cancel_intent=True,
        raise_on_conflict=False,
    )

    assert transition.terminalization.disposition == "reconciliation_required"
    assert transition.terminalization.reserved_checkpoint_ids == (orphan.checkpoint_id,)
    assert _durable_snapshot(project, orphan) == before


def test_reserved_effect_still_requires_reconciliation_after_claim_is_lost(
    project,
) -> None:
    orphan = _seed_orphaned_run(project, reserved_checkpoint=True)
    project.db.execute(
        "UPDATE output_column_claims SET status='expired', "
        "released_at=datetime('now') WHERE run_id=?",
        (orphan.run_id,),
    )
    project.db.commit()
    before = _durable_snapshot(project, orphan)

    result = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )

    assert result.disposition == "reconciliation_required"
    assert result.reserved_checkpoint_ids == (orphan.checkpoint_id,)
    assert result.reason == "reserved_effect_checkpoint"
    assert _durable_snapshot(project, orphan) == before


def test_run_terminalizes_when_existing_receipt_refuses_a_new_outcome(project) -> None:
    """Admin synthesis preserves a prior terminal receipt monotonically."""

    orphan = _seed_orphaned_run(project)
    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(orphan.receipt_id)
    assert stored is not None
    refused_receipt = stored.parsed().model_copy(update={"status": "failed"})
    assert receipts.update_if_status_in(refused_receipt, {"queued"})
    receipt_before = tuple(
        project.db.execute(
            "SELECT status, body FROM receipts WHERE id=?",
            (orphan.receipt_id,),
        ).fetchone()
    )

    result = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=AbandonedAttemptRecoveryAuthority(),
    )

    assert result.disposition == "terminalized"
    assert result.receipt_disposition == "already_terminal"
    assert result.run_status == "cancelled"
    assert result.receipt_status == "failed"
    run = project.db.execute(
        "SELECT status, finished_at FROM runs WHERE id=?",
        (orphan.run_id,),
    ).fetchone()
    assert run is not None
    assert run["status"] == "cancelled"
    assert run["finished_at"] is not None
    assert (
        tuple(
            project.db.execute(
                "SELECT status, body FROM receipts WHERE id=?",
                (orphan.receipt_id,),
            ).fetchone()
        )
        == receipt_before
    )
    assert (
        project.db.execute(
            "SELECT status FROM output_column_claims WHERE run_id=?",
            (orphan.run_id,),
        ).fetchone()[0]
        == "cancelled"
    )


def test_receipt_only_orphan_repair_is_idempotent_after_receipt_turns_terminal(
    project,
) -> None:
    orphan = _seed_orphaned_run(project)

    first = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="failed",
        action_kind=ACTION_KIND,
        receipt_only=True,
    )
    assert first.terminalization.disposition == "terminalized"
    assert first.terminalization.receipt_status == "failed"
    after_first = _durable_snapshot(project, orphan)
    assert after_first["run"][0] == "running"
    assert after_first["receipt"][1] == "failed"
    assert after_first["claim"][0] == "failed"

    repeated = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="failed",
        action_kind=ACTION_KIND,
        receipt_only=True,
    )
    assert repeated.terminalization.disposition == "terminalized"
    assert repeated.terminalization.receipt_disposition == "already_terminal"
    assert _durable_snapshot(project, orphan) == after_first


def test_prepared_unclaimed_cancel_supersedes_attempt_and_closes_tuple(project) -> None:
    orphan = _seed_orphaned_run(project)
    project.db.execute(
        "UPDATE execution_attempts SET state='admitted' WHERE id=?",
        (orphan.attempt_id,),
    )
    project.db.commit()

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="cancelled",
        action_kind=ACTION_KIND,
        require_cancel_intent=True,
    )

    assert transition.terminalization.disposition == "terminalized"
    snapshot = _durable_snapshot(project, orphan)
    assert snapshot["run"][0] == "cancelled"
    assert snapshot["run"][2] is None
    assert snapshot["attempt"][0] == "superseded"
    assert snapshot["receipt"][1] == "cancelled"
    assert snapshot["claim"][0] == "cancelled"


def test_prepared_replacement_cancel_ignores_historical_abandoned_attempt(
    project,
) -> None:
    orphan = _seed_orphaned_run(project)
    replacement_attempt_id = "attempt_replacement_admitted"
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 1, 'admitted', 'replacement-fixture', '[]', datetime('now'))",
        (replacement_attempt_id, orphan.run_id),
    )
    stored = ReceiptStore(project).find_by_id(orphan.receipt_id)
    assert stored is not None
    replacement_receipt = stored.parsed().model_copy(
        update={
            "evidence": [
                ReceiptEvidence(
                    ref={
                        "kind": "queued_action_run_prepared",
                        "queue_kind": "project.run",
                        "run_id": orphan.run_id,
                        "attempt_id": replacement_attempt_id,
                    }
                )
            ]
        }
    )
    assert ReceiptStore(project).update_if_status_in(
        replacement_receipt,
        {"queued"},
    )

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="cancelled",
        receipt_id=orphan.receipt_id,
        action_kind=ACTION_KIND,
        require_cancel_intent=True,
    )

    assert transition.terminalization.disposition == "terminalized"
    states = {
        row["id"]: row["state"]
        for row in project.db.execute(
            "SELECT id, state FROM execution_attempts WHERE run_id=?",
            (orphan.run_id,),
        ).fetchall()
    }
    assert states == {
        orphan.attempt_id: "abandoned",
        replacement_attempt_id: "superseded",
    }
    snapshot = _durable_snapshot(project, orphan)
    assert snapshot["run"][0] == "cancelled"
    assert snapshot["receipt"][1] == "cancelled"
    assert snapshot["claim"][0] == "cancelled"


def test_recovery_without_job_evidence_ignores_newer_auxiliary_receipt(project) -> None:
    orphan = _seed_orphaned_run(project)
    auxiliary_receipt_id = "receipt_newer_auxiliary_action"
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id=auxiliary_receipt_id,
            project_id=PROJECT_ID,
            action_id="action_newer_auxiliary",
            action_kind="run.backfill",
            run_id=orphan.run_id,
            status="queued",
        )
    )

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="cancelled",
        action_kind=ACTION_KIND,
        require_cancel_intent=True,
    )

    assert transition.action_result.receipt_id == orphan.receipt_id
    assert transition.terminalization.disposition == "terminalized"
    assert transition.terminalization.receipt_status == "cancelled"
    auxiliary = ReceiptStore(project).find_by_id(auxiliary_receipt_id)
    assert auxiliary is not None
    assert auxiliary.status == "queued"


def test_missing_job_recovery_preserves_custom_producer_action_kind(project) -> None:
    orphan = _seed_orphaned_run(project)
    custom_kind = "demo.plugin.op.clean"
    project.db.execute(
        "UPDATE runs SET action_kind=? WHERE id=?",
        (custom_kind, orphan.run_id),
    )
    stored = ReceiptStore(project).find_by_id(orphan.receipt_id)
    assert stored is not None
    custom_receipt = stored.parsed().model_copy(update={"action_kind": custom_kind})
    assert ReceiptStore(project).update_if_status_in(custom_receipt, {"queued"})
    project.db.execute(
        "UPDATE output_column_claims SET action_kind=? WHERE run_id=?",
        (custom_kind, orphan.run_id),
    )
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id="receipt_custom_kind_auxiliary",
            project_id=PROJECT_ID,
            action_id="action_custom_kind_auxiliary",
            action_kind="run.backfill",
            run_id=orphan.run_id,
            status="queued",
        )
    )
    project.db.commit()

    result = cancel_project_run(
        project=project,
        queue=_NoJobQueue(),  # type: ignore[arg-type]
        active_runs={},
        run_jobs={},
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
    )

    assert result.disposition == "terminalized"
    assert result.terminalization is not None
    assert result.terminalization.receipt_status == "cancelled"
    auxiliary = ReceiptStore(project).find_by_id("receipt_custom_kind_auxiliary")
    assert auxiliary is not None
    assert auxiliary.status == "queued"
    terminal_snapshot = _durable_snapshot(project, orphan)

    repeated = cancel_project_run(
        project=project,
        queue=_NoJobQueue(),  # type: ignore[arg-type]
        active_runs={},
        run_jobs={},
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
    )
    assert repeated.disposition == "already_terminal"
    assert _durable_snapshot(project, orphan) == terminal_snapshot


def test_terminal_run_receipt_repair_releases_a_run_bound_claim(project) -> None:
    orphan = _seed_orphaned_run(project)
    project.db.execute(
        "UPDATE runs SET status='completed', finished_at=datetime('now') WHERE id=?",
        (orphan.run_id,),
    )
    project.db.commit()

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="failed",
        receipt_id=orphan.receipt_id,
        action_kind=ACTION_KIND,
        repair_terminal_run_receipt=True,
        terminal_run_status="completed",
    )

    assert transition.terminalization.disposition == "terminalized"
    assert transition.terminalization.run_status == "completed"
    assert transition.terminalization.receipt_status == "failed"
    snapshot = _durable_snapshot(project, orphan)
    assert snapshot["run"][0] == "completed"
    assert snapshot["receipt"][1] == "failed"
    assert snapshot["claim"][0] == "failed"


def test_malformed_receipt_is_a_typed_conflict_without_partial_writes(project) -> None:
    orphan = _seed_orphaned_run(project)
    project.db.execute(
        "UPDATE receipts SET body='not-json' WHERE id=?",
        (orphan.receipt_id,),
    )
    project.db.commit()
    before = _durable_snapshot(project, orphan)

    transition = queued_v1_terminal_receipt_transition(
        project,
        project_id=PROJECT_ID,
        run_id=orphan.run_id,
        status="cancelled",
        action_kind=ACTION_KIND,
        require_cancel_intent=True,
        raise_on_conflict=False,
    )

    assert transition.terminalization.disposition == "conflict"
    assert transition.terminalization.reason == "receipt_body_invalid"
    assert _durable_snapshot(project, orphan) == before


def test_missing_receipt_refuses_before_any_terminal_projection(project) -> None:
    orphan = _seed_orphaned_run(project)
    project.db.execute("DELETE FROM receipts WHERE id=?", (orphan.receipt_id,))
    project.db.commit()
    before = {
        "run": tuple(
            project.db.execute(
                "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
                (orphan.run_id,),
            ).fetchone()
        ),
        "attempt": tuple(
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (orphan.attempt_id,),
            ).fetchone()
        ),
        "claim": tuple(
            project.db.execute(
                "SELECT receipt_id, status, released_at FROM output_column_claims "
                "WHERE run_id=?",
                (orphan.run_id,),
            ).fetchone()
        ),
    }

    result = terminalize_project_run(
        project,
        run_id=orphan.run_id,
        receipt_id=orphan.receipt_id,
        status="cancelled",
        authority=UnclaimedRunTerminalAuthority(),
    )

    assert result.disposition == "receipt_missing"
    assert result.reason == "project_run_receipt_missing"
    after = {
        "run": tuple(
            project.db.execute(
                "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
                (orphan.run_id,),
            ).fetchone()
        ),
        "attempt": tuple(
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (orphan.attempt_id,),
            ).fetchone()
        ),
        "claim": tuple(
            project.db.execute(
                "SELECT receipt_id, status, released_at FROM output_column_claims "
                "WHERE run_id=?",
                (orphan.run_id,),
            ).fetchone()
        ),
    }
    assert after == before


def test_terminalization_rolls_back_when_receipt_update_refuses(
    project,
    monkeypatch,
) -> None:
    orphan = _seed_orphaned_run(project)
    before = _durable_snapshot(project, orphan)
    monkeypatch.setattr(
        ReceiptStore,
        "update_if_status_in",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="receipt postcondition failed"):
        terminalize_project_run(
            project,
            run_id=orphan.run_id,
            receipt_id=orphan.receipt_id,
            status="cancelled",
            authority=AbandonedAttemptRecoveryAuthority(),
        )

    assert _durable_snapshot(project, orphan) == before


def test_abandoned_writer_terminalization_rolls_back_when_claim_release_refuses(
    project,
    monkeypatch,
) -> None:
    orphan = _seed_orphaned_run(project)
    before = _durable_snapshot(project, orphan)

    monkeypatch.setattr(
        OutputColumnClaimStore,
        "release_for_run_receipt",
        lambda *_args, **_kwargs: 0,
    )

    with pytest.raises(
        RuntimeError,
        match="terminal claim release refused: expected 1, released 0",
    ):
        terminalize_project_run(
            project,
            run_id=orphan.run_id,
            receipt_id=orphan.receipt_id,
            status="cancelled",
            authority=AbandonedAttemptRecoveryAuthority(),
        )

    assert _durable_snapshot(project, orphan) == before


def test_current_writer_terminalization_rolls_back_when_attempt_close_refuses(
    project,
    monkeypatch,
) -> None:
    orphan = _seed_orphaned_run(project)
    project.db.execute(
        "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
        (orphan.attempt_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (orphan.attempt_id, orphan.run_id),
    )
    project.db.commit()
    before = _durable_snapshot(project, orphan)

    monkeypatch.setattr(
        terminalizer_module,
        "set_attempt_state",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        RuntimeError,
        match="terminal attempt closure refused",
    ):
        terminalize_project_run(
            project,
            run_id=orphan.run_id,
            receipt_id=orphan.receipt_id,
            status="cancelled",
            authority=CurrentWriterTerminalAuthority(
                writer_attempt_id=orphan.attempt_id,
                claim_token=orphan.claim_token,
            ),
        )

    assert _durable_snapshot(project, orphan) == before


def test_abandoned_writer_terminalization_rolls_back_when_claim_release_fails(
    project,
    monkeypatch,
) -> None:
    orphan = _seed_orphaned_run(project)
    before = _durable_snapshot(project, orphan)
    release_reached = False

    def fail_claim_release(
        self,
        *,
        run_id: int,
        receipt_id: str,
        status: str,
        commit: bool = True,
    ) -> int:
        nonlocal release_reached
        release_reached = True
        assert self.db.in_transaction
        assert run_id == orphan.run_id
        assert receipt_id == orphan.receipt_id
        assert status == "cancelled"
        assert commit is False

        run = self.db.execute(
            "SELECT status, finished_at FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "cancelled"
        assert run["finished_at"] is not None
        receipt = ReceiptStore(project).find_by_id(receipt_id)
        assert receipt is not None
        assert receipt.status == "cancelled"
        assert receipt.parsed().status == "cancelled"
        raise RuntimeError("injected claim release failure")

    monkeypatch.setattr(
        OutputColumnClaimStore,
        "release_for_run_receipt",
        fail_claim_release,
    )

    with pytest.raises(RuntimeError, match="injected claim release failure"):
        terminalize_project_run(
            project,
            run_id=orphan.run_id,
            receipt_id=orphan.receipt_id,
            status="cancelled",
            authority=AbandonedAttemptRecoveryAuthority(),
        )

    assert release_reached
    assert _durable_snapshot(project, orphan) == before
