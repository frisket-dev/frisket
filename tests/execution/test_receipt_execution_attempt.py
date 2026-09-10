"""Preview accounting uses the real attempt/route/fact stores, without cells."""

from __future__ import annotations

import pytest

from frisket.contracts.action import Receipt
from frisket.contracts.http.project_attempts import ProjectAttemptReceipt
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import (
    AttemptClaimRefused,
    StaleAttemptWriter,
    attempt_receipt,
    claim,
    set_attempt_state,
)
from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.resolve_for_action import action_identity_hash
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
from tests.execution.test_settlement_join import (
    PROMISES,
    SPEC,
    TRANSCRIPTION_PLAN,
    _execution_composition,
)
from tests.execution.test_resolve_for_action import audio_project as audio_project


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "preview.frisket")
    try:
        yield project
    finally:
        project.close()


def _reserve(project, receipt_id="preview"):
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id="preview",
        action_id="media.transcribe",
        action_kind="media.transcribe",
        params_hash=action_identity_hash(SPEC),
        status="running",
    )
    ReceiptStore(project).insert_running(receipt)
    store = RouteStore.for_receipt(project, receipt_id)
    promises = store.append_promise_set(promises=PROMISES, predecessor_id=None)
    store.record_consent(
        action_identity_hash=action_identity_hash(SPEC),
        promise_set_hash=promises.promise_set_hash,
        actor=instance_principal(project),
    )
    route = store.append_route(
        promise_set_id=promises.id,
        engine="parakeet-tdt",
        options={},
        target_snapshot={
            "target_id": "synthetic-venue",
            "capability": "transcribe",
            "transport": "frisket.transcription.v1",
            "run_scoped": False,
        },
        operator="self",
        egress_class="none",
        region=None,
        credential_source="platform_key",
        cost_posture="platform_metered",
        predecessor_id=None,
    )
    return receipt, route


def _mint(project, receipt_id="preview", **kwargs):
    return AttemptAuthority(project, composition=_execution_composition()).mint(
        recipe=TRANSCRIPTION_PLAN.program,
        spec=SPEC,
        receipt_id=receipt_id,
        scope=(1, 2),
        **kwargs,
    )


def _batch(route, *, call_id="actual-call", outcome="ok", **fact):
    return [
        {
            "row_id": 1,
            "column_id": None,
            "outcome": outcome,
            "value": "ephemeral transcription",
            "model_calls": [
                bind_fact_to_route(
                    route,
                    {
                        "id": call_id,
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "transcribe",
                        "engine": "parakeet-tdt",
                        "credential_source": "platform_key",
                        "units": {"audio_seconds": 120, "compute_seconds": 900},
                        **fact,
                    },
                )
            ],
        }
    ]


def _write(project, attempt, batch, **kwargs):
    RunResultStore(project).write_returned_call_accounting(
        None,
        batch,
        receipt_id=attempt.receipt_id,
        writer_attempt_id=attempt.attempt_id,
        **kwargs,
    )


@pytest.mark.parametrize(
    "status,remainder", [("completed", "model_error"), ("cancelled", "cancelled")]
)
def test_receipt_attempt_prices_actual_calls_and_quoted_rows_without_outputs(
    project, status, remainder
):
    receipt, route = _reserve(project)
    attempt = _mint(project)
    restored = AttemptAuthority(
        project, composition=_execution_composition()
    ).load_admitted(
        attempt_id=attempt.attempt_id,
        receipt_id=receipt.receipt_id,
        recipe=TRANSCRIPTION_PLAN.program,
        spec=SPEC,
    )
    assert restored == attempt
    claim(project, restored, claimless_direct_effect=True)
    batch = _batch(route)
    _write(project, attempt, batch)
    _write(project, attempt, batch)  # same returned call is accounted once
    store = RunResultStore(project)
    outcomes = batch + [{"row_id": 2, "column_id": None, "outcome": remainder}]
    store.write_receipt_row_outcomes(
        receipt.receipt_id, outcomes, writer_attempt_id=attempt.attempt_id
    )
    store.write_receipt_row_outcomes(
        receipt.receipt_id, outcomes, writer_attempt_id=attempt.attempt_id
    )
    set_attempt_state(project, attempt.attempt_id, "effected")
    ReceiptStore(project).update_body_status(
        receipt.model_copy(update={"status": status})
    )

    facts = store.model_calls()
    assert len(facts) == 1
    assert facts[0]["attempt_id"] == attempt.attempt_id
    assert facts[0]["run_id"] is None and facts[0]["column_id"] is None
    assert facts[0]["epoch_id"] is not None
    view = attempt_receipt(project, attempt.attempt_id)
    assert ProjectAttemptReceipt.model_validate(view).receipt_id == receipt.receipt_id
    assert view["receipt_id"] == receipt.receipt_id and view["run_id"] is None
    assert view["settlement"]["charge_usd"] == "0.2"
    assert view["settlement"]["metered_quantity"] == "120"
    assert view["settlement"]["billable_quantity"] == "2"
    assert view["settlement"]["charged_quantity"] == "10"
    for table in ("ops", "runs", "columns", "cells", "output_column_claims"):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_failed_row_keeps_actual_cost_but_is_not_a_successful_retail_row(project):
    receipt, route = _reserve(project)
    attempt = _mint(project)
    claim(project, attempt, claimless_direct_effect=True)
    _write(project, attempt, _batch(route, outcome="model_error"))
    RunResultStore(project).write_receipt_row_outcomes(
        receipt.receipt_id,
        [
            {"row_id": 1, "outcome": "model_error"},
            {"row_id": 2, "outcome": "model_error"},
        ],
        writer_attempt_id=attempt.attempt_id,
    )
    set_attempt_state(project, attempt.attempt_id, "effected")
    ReceiptStore(project).update_body_status(
        receipt.model_copy(update={"status": "failed"})
    )
    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["metered_quantity"] == "120"
    assert settlement["charge_usd"] == "0"
    assert len(RunResultStore(project).model_calls()) == 1


def test_receipt_writer_reuses_atomic_project_key_spend_and_replay(project):
    _, route = _reserve(project)
    attempt = _mint(project)
    claim(project, attempt, claimless_direct_effect=True)
    batch = _batch(route, credential_source="project_key", provider_cost_usd=0.012)
    # Persist the actual credential, including a divergence from the route pin.
    actual = batch[0]["model_calls"][0]
    actual["credential_source"] = "project_key"
    actual[ROUTE_OBSERVATION_KEY]["observed"]["credential_source"] = "project_key"
    provider = batch[0]["model_calls"][0]["provider"]
    project.set_provider_key(
        provider=provider, encrypted="test-encrypted", hint="test", spend_cap_micro=None
    )
    _write(project, attempt, batch)
    _write(project, attempt, batch)
    assert project.provider_spend_state(provider).spent_micro == 12_000
    invalid = _batch(
        route, call_id="second", credential_source="project_key", provider_cost_usd=0.2
    )
    invalid += _batch(route, call_id="bad", provider_cost_usd=-1)
    with pytest.raises((RuntimeError, ValueError)):
        _write(project, attempt, invalid)
    project.db.commit()
    assert project.provider_spend_state(provider).spent_micro == 12_000
    assert len(RunResultStore(project).model_calls()) == 1


@pytest.mark.parametrize(
    "change",
    ["row", "nested_row", "column", "nested_column", "run", "attempt", "route"],
)
def test_receipt_fact_writer_refuses_scope_and_owner_drift(project, change):
    _, route = _reserve(project)
    _, other_route = _reserve(project, "other")
    attempt = _mint(project)
    claim(project, attempt, claimless_direct_effect=True)
    batch = _batch(route)
    call = batch[0]["model_calls"][0]
    if change == "row":
        batch[0]["row_id"] = 999
    elif change == "nested_row":
        call["row_id"] = 999
    elif change == "column":
        batch[0]["column_id"] = 1
    elif change == "nested_column":
        call["column_id"] = 1
    elif change == "run":
        call["run_id"] = 1
    elif change == "attempt":
        call["attempt_id"] = "other"
    else:
        batch = _batch(other_route)
    with pytest.raises(RuntimeError):
        _write(project, attempt, batch)
    project.db.commit()
    assert RunResultStore(project).model_calls() == []


def test_receipt_requires_active_claim_and_exact_reservation(project):
    receipt, route = _reserve(project)
    attempt = _mint(project)
    with pytest.raises(StaleAttemptWriter):
        _write(project, attempt, _batch(route))
    claim(project, attempt, claimless_direct_effect=True)
    with pytest.raises(StaleAttemptWriter):
        RunResultStore(project).write_model_calls(
            None,
            _batch(route),
            receipt_id="wrong",
            writer_attempt_id=attempt.attempt_id,
        )
    ReceiptStore(project).update_body_status(
        receipt.model_copy(update={"status": "failed"})
    )
    with pytest.raises(StaleAttemptWriter):
        _write(project, attempt, _batch(route))
    assert RunResultStore(project).model_calls() == []


def test_receipt_claim_cannot_duplicate_dispatch_or_claim_output_columns(project):
    _reserve(project)
    first, second = _mint(project), _mint(project)
    with pytest.raises(AttemptClaimRefused):
        claim(project, first, claimless_direct_effect=True, output_column_ids={1})
    claim(project, first, claimless_direct_effect=True)
    with pytest.raises(AttemptClaimRefused):
        claim(project, second, claimless_direct_effect=True)
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE state='dispatching'"
        ).fetchone()[0]
        == 1
    )


def test_receipt_row_outcome_cannot_be_rewritten(project):
    _reserve(project)
    attempt = _mint(project)
    claim(project, attempt, claimless_direct_effect=True)
    store = RunResultStore(project)
    store.write_receipt_row_outcomes(
        "preview",
        [{"row_id": 1, "outcome": "model_error"}],
        writer_attempt_id=attempt.attempt_id,
    )
    with pytest.raises(RuntimeError, match="immutable"):
        store.write_receipt_row_outcomes(
            "preview",
            [{"row_id": 1, "outcome": "ok"}],
            writer_attempt_id=attempt.attempt_id,
        )
    assert (
        project.db.execute(
            "SELECT terminal_outcome FROM attempt_row_authorizations WHERE row_id=1"
        ).fetchone()[0]
        == "failed"
    )


@pytest.mark.parametrize("owners", [{}, {"run_id": 1, "receipt_id": "preview"}])
def test_attempt_requires_exactly_one_owner(project, owners):
    with pytest.raises(ValueError, match="exactly one"):
        AttemptAuthority(project, composition=_execution_composition()).mint(
            recipe=TRANSCRIPTION_PLAN.program,
            spec=SPEC,
            scope=(1, 2),
            **owners,
        )


def test_receipt_cannot_mint_for_missing_or_finished_reservation(project):
    from frisket.execution.attempt_authority import ExecutionRouteVerificationFailed

    with pytest.raises(ExecutionRouteVerificationFailed):
        _mint(project)
    receipt, _ = _reserve(project)
    ReceiptStore(project).update_body_status(
        receipt.model_copy(update={"status": "failed"})
    )
    with pytest.raises(ExecutionRouteVerificationFailed):
        _mint(project)
    assert (
        project.db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0
    )


def test_receipt_route_recording_reuses_durable_resolution(audio_project):
    from frisket.execution.resolve_for_action import (
        record_resolved_execution,
        resolve_for_action,
    )
    from tests.execution.test_resolve_for_action import (
        _composition,
        _spec,
        transcription_program,
    )

    project, sheet_id = audio_project
    spec = _spec(sheet_id)
    program = transcription_program(spec)
    composition = _composition(project)
    resolved = resolve_for_action(project, spec, program, composition=composition)
    ReceiptStore(project).insert_running(
        Receipt(
            receipt_id="local-preview",
            project_id="preview",
            action_id="media.transcribe",
            action_kind="media.transcribe",
            params_hash=action_identity_hash(spec),
            status="running",
        )
    )
    promises, route = record_resolved_execution(
        project,
        None,
        resolved,
        receipt_id="local-preview",
        spec=spec,
        consent_required=False,
    )
    assert RouteStore.for_receipt(project, "local-preview").head() == (route, promises)
    attempt = AttemptAuthority(project, composition=composition).mint(
        receipt_id="local-preview",
        recipe=program,
        spec=spec,
        scope=(1,),
    )
    claim(project, attempt, claimless_direct_effect=True)
    assert (
        attempt_receipt(project, attempt.attempt_id)["target"]["target_id"] == "local"
    )
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_receipt_claim_rechecks_durable_route_head(project):
    _, route = _reserve(project)
    attempt = _mint(project)
    RouteStore.for_receipt(project, "preview").append_route(
        promise_set_id=route.promise_set_id,
        engine=route.engine,
        options=route.options,
        target_snapshot=route.target_snapshot,
        operator=route.operator,
        egress_class=route.egress_class,
        region=route.region,
        credential_source=route.credential_source,
        cost_posture=route.cost_posture,
        predecessor_id=route.id,
    )
    with pytest.raises(AttemptClaimRefused, match="advanced"):
        claim(project, attempt, claimless_direct_effect=True)
    assert (
        project.db.execute("SELECT state FROM execution_attempts").fetchone()[0]
        == "admitted"
    )


def test_returned_fact_cannot_be_reparented_to_another_receipt(project):
    _, route = _reserve(project)
    _, other_route = _reserve(project, "other")
    first, second = _mint(project), _mint(project, "other")
    claim(project, first, claimless_direct_effect=True)
    claim(project, second, claimless_direct_effect=True)
    _write(project, first, _batch(route))
    with pytest.raises(RuntimeError, match="attempt_id changed"):
        _write(project, second, _batch(other_route))
    project.db.commit()
    facts = RunResultStore(project).model_calls()
    assert len(facts) == 1 and facts[0]["attempt_id"] == first.attempt_id


def test_receipt_accounting_and_row_outcomes_can_share_callers_transaction(project):
    _, route = _reserve(project)
    attempt = _mint(project)
    claim(project, attempt, claimless_direct_effect=True)
    batch = _batch(route)
    project.db.execute("BEGIN IMMEDIATE")
    _write(project, attempt, batch, commit=False)
    RunResultStore(project).write_receipt_row_outcomes(
        "preview",
        batch,
        writer_attempt_id=attempt.attempt_id,
        commit=False,
    )
    project.db.rollback()
    assert RunResultStore(project).model_calls() == []
    assert (
        project.db.execute(
            "SELECT terminal_outcome FROM attempt_row_authorizations WHERE row_id=1"
        ).fetchone()[0]
        is None
    )
