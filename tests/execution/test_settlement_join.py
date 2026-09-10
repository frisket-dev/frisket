"""Pinned-term settlement acceptance: "what did it cost", answered by joining
metering rows on ``attempt_id`` under the attempt's consented terms.

its shape, and what it deliberately is NOT: no settlement table, no second
write path, exactly one new column per repo. Authorization
(``execution_attempts``: identity, scope, consent, cost basis,
``price_card_version``) meets settlement (``model_calls``: the measured
quantity) at receipt time, because a measured quantity only exists at the
per-model-call grain.

The rate-change rule is the same join read backwards: settlement reads the
terms the ATTEMPT pinned, so a deployment change after consent cannot reach a
run the user already agreed to.
"""

from __future__ import annotations

import json

import pytest

from frisket.contracts.action import Receipt
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport
from frisket.engine.executor.project_run_terminalization import (
    CurrentWriterTerminalAuthority,
    terminalize_project_run,
)
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import StaleAttemptWriter, attempt_receipt, claim
from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.price_book import PlatformMetered
from frisket.execution.provider import CompositionFacts, ExecutionComposition
from frisket.execution.resolve_for_action import action_identity_hash
from frisket.execution.targets import ExecutionTarget, TargetEngineSupport

TRANSCRIPTION_PLAN = _typed_map_rows_plan(
    BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("media.transcribe"),
        ActionRequest(
            action_id="media.transcribe",
            scope=SheetRows(sheet_id=1),
            params={"source": "media", "engine": "parakeet-tdt"},
            output_names={"text": "transcript"},
            idempotency_key="settlement-transcription",
        ),
    )
)
SPEC = TRANSCRIPTION_PLAN.spec_dict()

#: A consented synthetic-offering promise set: 10 audio-minutes at the test
#: rate. Base owns the settlement machinery, but no concrete offering.
PROMISES = [
    {
        "field": "operator",
        "op": "eq",
        "value": "self",
        "basis": None,
        "order_ref": None,
        "audience": "system_promise",
    },
    {
        "field": "egress_class",
        "op": "eq",
        "value": "none",
        "basis": None,
        "order_ref": None,
        "audience": "user_claim",
    },
    {
        "field": "cost",
        "op": "le",
        "value": "0.20",
        "basis": {
            "pricing_key": "test.synthetic.transcription.audio_minute",
            "unit_rate": "0.02",
            "estimated_quantity": "10",
            "terms_version": "test.synthetic.terms.v1",
            "quantity_unit": "audio_minute",
            "quantity_rounding_mode": "exact",
            "quantity_rounding_decimal_places": None,
            "meter_key": "audio_seconds",
            "meter_units_per_quantity_unit": "60",
            "ceiling_mode": "consented_quantity",
            "row_settlement_mode": "quoted_successful_rows",
            "charge_authority": "test.synthetic.authority",
            "hardware_class": None,
            "throughput_ref": None,
            # The fixture's first row carries the ten-minute quote; row two is
            # explicit zero so the allocation still covers its exact scope.
            "row_quote_quantities": [
                {"row_id": 1, "quantity": "10"},
                {"row_id": 2, "quantity": "0"},
            ],
        },
        "order_ref": None,
        "audience": "user_claim",
    },
]


def _test_offering(*, unit_rate: str = "0.02") -> CommercialOffering:
    return CommercialOffering(
        match=CommercialOfferingMatch(
            target_id="synthetic-venue",
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.synthetic.transcription.audio_minute",
            terms_version="test.synthetic.terms.v1",
            unit_rate=unit_rate,
            quantity_unit="audio_minute",
            rounding=CommercialQuoteRounding(),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode="quoted_successful_rows",
        ),
        charge_authority="test.synthetic.authority",
        presentation=CommercialPresentation(
            venue_label="Synthetic venue",
            billing_label="Synthetic usage billing",
        ),
    )


class _SyntheticProvider:
    def targets(self):
        return (
            ExecutionTarget(
                id="synthetic-venue",
                operator="self",
                egress_class="none",
                engines=(
                    TargetEngineSupport(
                        engine="parakeet-tdt",
                        transport="frisket.transcription.v1",
                        capability="transcribe",
                        options=TranscriptionOptionSupport(
                            diarization_mode="none",
                            speaker_hint="none",
                            language=False,
                            model_size=False,
                            vad=False,
                            context=False,
                        ),
                    ),
                ),
            ),
        )

    def connection(self, target_id: str):
        from frisket.execution.provider import ConnectionConfig

        return ConnectionConfig(base_url="http://gw:9000", token="secret")


def _execution_composition() -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            funding=PlatformMetered(),
        ),
        provider=_SyntheticProvider(),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=(_test_offering(),),
    )


@pytest.fixture()
def project(tmp_path):
    p = Project.create(tmp_path / "t.frisket", name="t")
    try:
        yield p
    finally:
        p.close()


@pytest.fixture()
def consented_run(project):
    sheet_id = project.add_sheet("S")
    project.add_column(sheet_id, "transcript", ai_generated=True)
    row_ids = project.add_rows(sheet_id, [{}, {}], {})
    op_id = project.db.execute(
        "INSERT INTO ops (kind, spec) VALUES (?,?)", ("transcribe", "{}")
    ).lastrowid
    project.db.commit()
    run_id = RunResultStore(project).start_run(
        int(op_id),
        sheet_id,
        "media.transcribe",
        params=SPEC,
        row_ids=row_ids,
    )
    claim_token = f"output-claim:settlement:{run_id}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["transcript"],
        action_kind="media.transcribe",
        run_id=run_id,
        op_id=int(op_id),
        claim_token=claim_token,
    )
    assert conflict is None
    assert len(claims) == 1
    OutputColumnClaimStore(project).bind_to_run(
        claim_token=claim_token,
        run_id=run_id,
        expected_output_names=["transcript"],
    )

    store = RouteStore.for_run(project, run_id)
    promise_set = store.append_promise_set(promises=PROMISES, predecessor_id=None)
    store.record_consent(
        action_identity_hash=action_identity_hash(SPEC),
        promise_set_hash=promise_set.promise_set_hash,
        actor=instance_principal(project),
    )
    store.append_route(
        promise_set_id=promise_set.id,
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
    return run_id


def _mint(project, run_id, *, scope=(1, 2)):
    return AttemptAuthority(project, composition=_execution_composition()).mint(
        recipe=TRANSCRIPTION_PLAN.program, spec=SPEC, run_id=run_id, scope=scope
    )


def _claim_output_attempt(project, commitment) -> None:
    claims = project.db.execute(
        "SELECT claim_token, column_id FROM output_column_claims "
        "WHERE run_id=? AND status='active' ORDER BY id",
        (commitment.run_id,),
    ).fetchall()
    assert claims
    assert len({str(row["claim_token"]) for row in claims}) == 1
    claim(
        project,
        commitment,
        claimless_direct_effect=False,
        claim_token=str(claims[0]["claim_token"]),
        output_column_ids={
            int(row["column_id"]) for row in claims if row["column_id"] is not None
        },
    )


def _writer_authority(
    project,
    run_id: int,
    *,
    attempt_id: str | None = None,
) -> dict[str, str | None]:
    row = project.db.execute(
        "SELECT r.current_attempt_id, c.claim_token "
        "FROM runs r JOIN output_column_claims c ON c.run_id=r.id "
        "WHERE r.id=? AND c.status='active' LIMIT 1",
        (run_id,),
    ).fetchone()
    assert row is not None
    writer = attempt_id or row["current_attempt_id"]
    return {
        "writer_attempt_id": str(writer) if writer is not None else None,
        "authorized_attempt_id": str(writer) if writer is not None else None,
        "claim_token": str(row["claim_token"]),
    }


def _record_call(
    project,
    run_id: int,
    *,
    row_id: int,
    compute_seconds: float,
    audio_seconds: float | None = None,
    authorized_attempt_id: str | None = None,
) -> None:
    """One metered model call, written through the REAL fact writer so the
    attempt stamp comes from the production path, not from the test.

    The two units are INDEPENDENT and default to clearly different numbers on
    purpose. This fixture used to write ``audio_seconds=compute_seconds``, so
    every settlement assertion below held in a world where the meter's unit
    and the audio duration were equal BY CONSTRUCTION — which is why an
    unbounded-quantity defect survived because any test that
    accidentally read the wrong unit still got the right number. A settlement
    answer that depends on which of the two it reads must now say so.

    The synthetic offering bills ``audio_seconds``, so the tests below pass
    the AUDIO quantity as the billing quantity and leave ``compute_seconds`` at a
    value the expected charge proves is ignored.
    """
    if audio_seconds is None:
        audio_seconds = compute_seconds * 3.0 + 7.0
    authority = _writer_authority(
        project,
        run_id,
        attempt_id=authorized_attempt_id,
    )
    RunResultStore(project).write_model_calls(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": 1,
                "model_calls": [
                    {
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "llm.complete",
                        "engine": "parakeet-tdt",
                        "provider": "synthetic-venue",
                        "provider_kind": "sidecar",
                        "credential_source": "platform_key",
                        "units": {
                            "compute_seconds": compute_seconds,
                            "audio_seconds": audio_seconds,
                        },
                    }
                ],
            }
        ],
        **authority,
    )
    attempt_id = authorized_attempt_id
    if attempt_id is None:
        current = project.db.execute(
            "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        attempt_id = current["current_attempt_id"] if current is not None else None
    if attempt_id is not None:
        # These settlement-join tests isolate metering, so their direct fact
        # helper supplies the already-proved terminal-success side of the new
        # row join. End-to-end result writes are covered by the 38-of-40 test.
        project.db.execute(
            "UPDATE attempt_row_authorizations SET terminal_outcome="
            "CASE WHEN row_id=? THEN 'succeeded' ELSE 'failed' END "
            "WHERE attempt_id=? AND terminal_outcome IS NULL",
            (row_id, attempt_id),
        )
        project.db.commit()


def _peer_run(project, source_run_id: int) -> int:
    """A second run in the same project, for horizontal-ownership attacks."""
    source = project.db.execute(
        "SELECT op_id, sheet_id FROM runs WHERE id=?", (source_run_id,)
    ).fetchone()
    assert source is not None
    return RunResultStore(project).start_run(
        int(source["op_id"]),
        int(source["sheet_id"]),
        "media.transcribe",
        params=SPEC,
    )


def _unstamped_llm_fact(call_id: str, **overrides) -> dict:
    fact = {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "openai/gpt-4.1-mini",
        "provider": "openai",
        "provider_kind": "chat_api",
        "credential_source": "none",
        "units": {"tokens_in": 1, "tokens_out": 1},
    }
    fact.update(overrides)
    return fact


def test_fact_writer_refuses_a_nested_cross_run_override(project, consented_run):
    """The method argument owns the fact's run; nested payload cannot retarget it.

    Row executors pass one authoritative ``run_id`` to the sink and then hand
    it translated provider payloads.  A stale/cross-wired ``call["run_id"]``
    must be loud, not turn a fact produced for run A into run B's metering and
    receipt evidence.
    """
    run_a = consented_run
    run_b = _peer_run(project, run_a)
    attempt = _mint(project, run_a)
    _claim_output_attempt(project, attempt)
    call_id = "cross-run-payload-override"

    with pytest.raises(RuntimeError, match="run_id"):
        RunResultStore(project).write_model_calls(
            run_a,
            [
                {
                    "row_id": 1,
                    "column_id": 1,
                    "model_calls": [
                        _unstamped_llm_fact(call_id, run_id=run_b),
                    ],
                }
            ],
            **_writer_authority(
                project,
                run_a,
                attempt_id=attempt.attempt_id,
            ),
        )

    assert (
        project.db.execute(
            "SELECT run_id, attempt_id FROM model_calls WHERE id=?", (call_id,)
        ).fetchone()
        is None
    )


def test_fact_writer_accepts_a_matching_nested_run_id(project, consented_run):
    """A redundant matching run id remains valid for existing producers."""
    attempt = _mint(project, consented_run)
    _claim_output_attempt(project, attempt)
    call_id = "matching-nested-run-id"

    RunResultStore(project).write_model_calls(
        consented_run,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "model_calls": [
                    _unstamped_llm_fact(call_id, run_id=str(consented_run)),
                ],
            }
        ],
        **_writer_authority(
            project,
            consented_run,
            attempt_id=attempt.attempt_id,
        ),
    )

    recorded = project.db.execute(
        "SELECT run_id FROM model_calls WHERE id=?", (call_id,)
    ).fetchone()
    assert recorded is not None
    assert recorded["run_id"] == consented_run


def test_fact_writer_refuses_a_foreign_current_attempt(project, consented_run):
    """A run pointer cannot stamp facts with an attempt owned by another run.

    ``runs.current_attempt_id`` has a single-column FK, so a claim-side seam
    mistake can legally point run A at run B's attempt.  The fact sink is the
    last boundary before that mistake becomes durable settlement evidence and
    must verify the horizontal ``execution_attempts.run_id`` ownership join.
    """
    from frisket.execution.attempt import open_attempt

    run_a = consented_run
    run_b = _peer_run(project, run_a)
    foreign_attempt_id, _seq = open_attempt(
        project,
        run_id=run_b,
        identity=action_identity_hash(SPEC),
        scope=(1, 2),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (foreign_attempt_id, run_a),
    )
    project.db.commit()
    assert (
        project.db.execute(
            "SELECT run_id FROM execution_attempts WHERE id=?",
            (foreign_attempt_id,),
        ).fetchone()["run_id"]
        == run_b
    )

    call_id = "foreign-current-attempt"
    with pytest.raises(RuntimeError, match="attempt"):
        RunResultStore(project).write_model_calls(
            run_a,
            [
                {
                    "row_id": 1,
                    "column_id": 1,
                    "model_calls": [_unstamped_llm_fact(call_id)],
                }
            ],
            **_writer_authority(
                project,
                run_a,
                attempt_id=foreign_attempt_id,
            ),
        )

    assert (
        project.db.execute(
            "SELECT run_id, attempt_id FROM model_calls WHERE id=?", (call_id,)
        ).fetchone()
        is None
    )


def test_a_late_fact_from_attempt_a_is_not_reparented_to_attempt_b(
    project, consented_run
):
    """A returned provider fact keeps the attempt that authorized its effect.

    The batch is assembled while A is dispatching, then persisted only after A
    closes and the same run advances to B.  The sink may either carry A's
    ownership into the row or refuse the now-ambiguous write; silently stamping
    B would charge A's work against B's consent and pinned terms.
    """
    from frisket.execution.attempt import set_attempt_state

    run_id = consented_run
    attempt_a = _mint(project, run_id)
    _claim_output_attempt(project, attempt_a)
    call_id = "late-attempt-a-fact"
    returned_under_a = [
        {
            "row_id": 1,
            "column_id": 1,
            "model_calls": [_unstamped_llm_fact(call_id)],
        }
    ]

    set_attempt_state(project, attempt_a.attempt_id, "abandoned")
    attempt_b = _mint(project, run_id)
    _claim_output_attempt(project, attempt_b)

    with pytest.raises(StaleAttemptWriter, match="not 'dispatching'"):
        RunResultStore(project).write_model_calls(
            run_id,
            returned_under_a,
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt_a.attempt_id,
            ),
        )
    assert (
        project.db.execute(
            "SELECT attempt_id FROM model_calls WHERE id=?", (call_id,)
        ).fetchone()
        is None
    )
    assert (
        project.db.execute(
            "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()["current_attempt_id"]
        == attempt_b.attempt_id
    )


def test_same_call_id_cannot_reconcile_across_attempt_ownership(project, consented_run):
    """A later attempt cannot enrich an earlier attempt's accepted call.

    Reusing a provider call id is not authority to move the returned meter
    across consent/pinned-term ownership. Cross-attempt provider idempotency
    would need its own explicit effect identity and accounting rule.
    """
    from frisket.execution.attempt import set_attempt_state

    run_id = consented_run
    attempt_a = _mint(project, run_id)
    _claim_output_attempt(project, attempt_a)
    call_id = "accepted-under-a"
    accepted = _unstamped_llm_fact(
        call_id,
        provider_cost_usd=None,
        cost_source="unknown",
        units={"requests": 1},
    )
    RunResultStore(project).write_model_calls(
        run_id,
        [{"row_id": 1, "column_id": 1, "model_calls": [accepted]}],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt_a.attempt_id,
        ),
    )
    set_attempt_state(project, attempt_a.attempt_id, "effected")

    attempt_b = _mint(project, run_id)
    _claim_output_attempt(project, attempt_b)
    completed = {
        **accepted,
        "provider_cost_usd": 0.25,
        "cost_source": "provider_reported",
    }
    with pytest.raises(RuntimeError, match="attempt_id changed"):
        RunResultStore(project).write_model_calls(
            run_id,
            [{"row_id": 1, "column_id": 1, "model_calls": [completed]}],
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt_b.attempt_id,
            ),
        )

    recorded = project.db.execute(
        "SELECT attempt_id, provider_cost_usd, cost_source FROM model_calls WHERE id=?",
        (call_id,),
    ).fetchone()
    assert recorded is not None
    assert recorded["attempt_id"] == attempt_a.attempt_id
    assert recorded["provider_cost_usd"] is None
    assert recorded["cost_source"] == "unknown"


def test_rejected_cross_attempt_call_id_cannot_leak_result_through_outer_commit(
    project, consented_run
):
    """A fact-identity refusal rolls back the sibling result mutation too.

    Run finalization normally commits after the row sink returns.  If the sink
    raises after upserting ``results`` but leaves that transaction open, the
    finalization commit can make a result from B durable while its only meter
    remains owned by A.  The transaction-owning write must fail atomically.
    """
    from frisket.execution.attempt import set_attempt_state

    run_id = consented_run
    store = RunResultStore(project)
    attempt_a = _mint(project, run_id)
    _claim_output_attempt(project, attempt_a)
    call_id = "result-and-fact-owned-by-a"
    accepted = _unstamped_llm_fact(call_id)
    store.write_results(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "value": "attempt A result",
                "outcome": "ok",
                "model_calls": [accepted],
            }
        ],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt_a.attempt_id,
        ),
    )
    set_attempt_state(project, attempt_a.attempt_id, "effected")

    attempt_b = _mint(project, run_id)
    _claim_output_attempt(project, attempt_b)
    with pytest.raises(RuntimeError, match="attempt_id changed"):
        store.write_results(
            run_id,
            [
                {
                    "row_id": 1,
                    "column_id": 1,
                    "value": "attempt B result",
                    "outcome": "ok",
                    "model_calls": [accepted],
                }
            ],
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt_b.attempt_id,
            ),
        )

    # Exercise the real leak window: a later owner commits ordinary run
    # finalization without knowing the rejected sink left mutations pending.
    store.finish_run(run_id, "completed")

    result = project.db.execute(
        "SELECT value FROM results WHERE run_id=? AND row_id=1 AND column_id=1",
        (run_id,),
    ).fetchone()
    assert result is not None
    assert json.loads(result["value"]) == "attempt A result"
    recorded = project.db.execute(
        "SELECT attempt_id FROM model_calls WHERE id=?", (call_id,)
    ).fetchone()
    assert recorded is not None
    assert recorded["attempt_id"] == attempt_a.attempt_id
    outcomes = {
        row["attempt_id"]: row["terminal_outcome"]
        for row in project.db.execute(
            "SELECT attempt_id, terminal_outcome "
            "FROM attempt_row_authorizations WHERE row_id=1 "
            "AND attempt_id IN (?, ?)",
            (attempt_a.attempt_id, attempt_b.attempt_id),
        ).fetchall()
    }
    assert outcomes == {
        attempt_a.attempt_id: "succeeded",
        attempt_b.attempt_id: None,
    }


def test_rejected_result_batch_rolls_back_to_commit_false_caller_savepoint(
    project, consented_run
):
    """``commit=False`` keeps outer work but removes this rejected batch."""
    from frisket.execution.attempt import set_attempt_state

    run_id = consented_run
    store = RunResultStore(project)
    attempt_a = _mint(project, run_id)
    _claim_output_attempt(project, attempt_a)
    call_id = "caller-owned-result-and-fact"
    accepted = _unstamped_llm_fact(call_id)
    store.write_results(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "value": "attempt A result",
                "outcome": "ok",
                "model_calls": [accepted],
            }
        ],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt_a.attempt_id,
        ),
    )
    set_attempt_state(project, attempt_a.attempt_id, "effected")

    attempt_b = _mint(project, run_id)
    _claim_output_attempt(project, attempt_b)
    caller_params = json.dumps({"outer_owner": "preserved"}, sort_keys=True)
    project.db.execute("UPDATE runs SET params=? WHERE id=?", (caller_params, run_id))
    assert project.db.in_transaction

    with pytest.raises(RuntimeError, match="attempt_id changed"):
        store.write_results(
            run_id,
            [
                {
                    "row_id": 1,
                    "column_id": 1,
                    "value": "attempt B result",
                    "outcome": "ok",
                    "model_calls": [accepted],
                }
            ],
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt_b.attempt_id,
            ),
            commit=False,
        )

    # The caller still owns a live transaction and can finish its unrelated
    # work.  Committing it must not resurrect the rejected sibling result.
    assert project.db.in_transaction
    store.finish_run(run_id, "completed", commit=False)
    project.db.commit()

    run = project.db.execute(
        "SELECT params, status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run is not None
    assert run["params"] == caller_params
    assert run["status"] == "completed"
    result = project.db.execute(
        "SELECT value FROM results WHERE run_id=? AND row_id=1 AND column_id=1",
        (run_id,),
    ).fetchone()
    assert result is not None
    assert json.loads(result["value"]) == "attempt A result"
    recorded = project.db.execute(
        "SELECT attempt_id FROM model_calls WHERE id=?", (call_id,)
    ).fetchone()
    assert recorded is not None
    assert recorded["attempt_id"] == attempt_a.attempt_id
    outcome = project.db.execute(
        "SELECT terminal_outcome FROM attempt_row_authorizations "
        "WHERE attempt_id=? AND row_id=1",
        (attempt_b.attempt_id,),
    ).fetchone()
    assert outcome is not None
    assert outcome["terminal_outcome"] is None


def test_a_receipt_answers_what_it_cost_by_joining_on_attempt_id(
    project, consented_run
):
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)

    _record_call(
        project,
        run_id,
        row_id=1,
        compute_seconds=1000.0,
        audio_seconds=240.0,
        authorized_attempt_id=attempt.attempt_id,
    )
    _record_call(
        project,
        run_id,
        row_id=2,
        compute_seconds=4000.0,
        audio_seconds=360.0,
        authorized_attempt_id=attempt.attempt_id,
    )

    stamped = project.db.execute(
        "SELECT attempt_id FROM model_calls ORDER BY id"
    ).fetchall()
    assert [row["attempt_id"] for row in stamped] == [attempt.attempt_id] * 2

    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["rated_calls"] == 2
    assert settlement["unmetered_calls"] == 0
    # 600 metered AUDIO-seconds -> 10 audio-minutes at the injected test rate,
    # which is exactly the bound the user consented to. The 5000 compute-seconds
    # the stopwatch recorded across the two calls reach nothing.
    assert settlement["metered_unit"] == "audio_seconds"
    assert settlement["metered_quantity"] == "600"
    assert settlement["billable_quantity"] == "10"
    assert settlement["charge_usd"] == "0.2"
    assert settlement["pricing_key"] == "test.synthetic.transcription.audio_minute"
    assert settlement["price_card_version"] == attempt.price_card_version


def test_a_second_attempt_settles_only_its_own_calls(project, consented_run):
    """A retry mints a new attempt, so the join is what makes "this row
    was paid for twice" visible as two settlements rather than one blurred
    total."""
    from frisket.execution.attempt import run_attempt_receipts, set_attempt_state

    run_id = consented_run
    first = _mint(project, run_id)
    _claim_output_attempt(project, first)
    _record_call(
        project,
        run_id,
        row_id=1,
        compute_seconds=4321.0,
        audio_seconds=600.0,
        authorized_attempt_id=first.attempt_id,
    )
    set_attempt_state(project, first.attempt_id, "effected")

    second = _mint(project, run_id)
    _claim_output_attempt(project, second)
    _record_call(
        project,
        run_id,
        row_id=1,
        compute_seconds=9.0,
        audio_seconds=120.0,
        authorized_attempt_id=second.attempt_id,
    )

    # Each attempt authorized the same ten-minute row quote, so each successful
    # row is $0.20 even though the second actual meter reports only two minutes.
    # Actual usage remains separate evidence; neither compute meter appears.
    receipts = {r["attempt_id"]: r for r in run_attempt_receipts(project, run_id)}
    assert receipts[first.attempt_id]["settlement"]["charge_usd"] == "0.2"
    assert receipts[second.attempt_id]["settlement"]["charge_usd"] == "0.2"
    assert receipts[second.attempt_id]["settlement"]["rated_charge_usd"] == "0.04"
    assert receipts[second.attempt_id]["settlement"]["absorbed_overage_usd"] == "0"
    assert receipts[first.attempt_id]["settlement"]["rated_calls"] == 1
    assert receipts[second.attempt_id]["settlement"]["rated_calls"] == 1


def test_settlement_rates_at_pinned_terms_not_a_changed_injected_offer(
    project, consented_run
):
    """A downstream deployment change cannot move a consented settlement."""
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(project, run_id, row_id=1, compute_seconds=77.0, audio_seconds=600.0)

    changed_offer = _test_offering(unit_rate="0.20")
    assert changed_offer.quote.unit_rate != attempt.cost_basis.unit_rate
    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["unit_rate"] == "0.02"
    assert settlement["charge_usd"] == "0.2"


def test_an_unmetered_call_is_counted_and_refuses_a_partial_total(
    project, consented_run
):
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    RunResultStore(project).write_model_calls(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "model_calls": [
                    {
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "llm.complete",
                        "engine": "parakeet-tdt",
                        "provider": "synthetic-venue",
                        "provider_kind": "sidecar",
                        "credential_source": "platform_key",
                        "units": {},
                    }
                ],
            }
        ],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt.attempt_id,
        ),
    )
    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["rated_calls"] == 0
    assert settlement["unmetered_calls"] == 1
    assert settlement["metered_quantity"] == "0"
    assert settlement["charge_usd"] is None
    assert settlement["unsettleable"] == "unmetered"
    assert settlement["exceeds_consented"] is None


def test_a_priced_attempt_that_metered_nothing_is_unsettleable_not_a_zero(
    project, consented_run
):
    """The keyed-money-proof defect (2026-07-26), one grain up from the
    unmetered-CALL case above.

    ``live-money-proof`` run 6 was a priced BYOK transcription
    (``openai/whisper-1.audio_second``) that wrote no metering row, so this
    join summed an empty set and the receipt said the run cost ``$0`` —
    a confident zero over a ledger with nothing in it. "The calls we found
    metered zero" and "no call was found" are different facts and only the
    first is a number. ``borne_by`` already declines to speak here (no rows,
    no answer); settlement now declines with it, so the two halves of the
    receipt cannot contradict each other.
    """
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    assert (
        project.db.execute(
            "SELECT COUNT(*) c FROM model_calls WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()["c"]
        == 0
    )

    receipt = attempt_receipt(project, attempt.attempt_id)
    settlement = receipt["settlement"]
    assert settlement["charge_usd"] is None
    assert settlement["unsettleable"] == "unmetered"
    assert settlement["rated_calls"] == 0
    # The AUTHORIZED side survives: what was quoted stays answerable even
    # though what was metered does not.
    assert settlement["pricing_key"] == "test.synthetic.transcription.audio_minute"
    assert settlement["unit_rate"] == "0.02"
    assert receipt["borne_by"] is None


def test_calls_written_before_any_attempt_are_refused_without_a_legacy_bypass(
    project, consented_run
):
    """A run-scoped fact cannot recreate the deleted attemptless write path."""
    run_id = consented_run
    with pytest.raises(StaleAttemptWriter, match="no immutable writer"):
        _record_call(project, run_id, row_id=1, compute_seconds=60.0)
    assert project.db.execute("SELECT attempt_id FROM model_calls").fetchone() is None


def test_an_attempt_with_a_mismatched_terms_version_is_not_re_rated(
    project, consented_run
):
    """The row's version must agree with the complete pinned basis."""
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(project, run_id, row_id=1, compute_seconds=600.0)
    project.db.execute(
        "UPDATE execution_attempts SET price_card_version='test.synthetic.terms.v0' "
        "WHERE id=?",
        (attempt.attempt_id,),
    )
    project.db.commit()

    receipt = attempt_receipt(project, attempt.attempt_id)
    assert receipt["settlement"] == {
        "price_card_version": "test.synthetic.terms.v0",
        "terminal_status": "running",
        "pricing_key": "test.synthetic.transcription.audio_minute",
        "charge_usd": None,
        "rated_charge_usd": None,
        "charged_quantity": None,
        "absorbed_overage_usd": None,
        "rated_calls": 0,
        "unmetered_calls": 1,
        "unsettleable": "terms_version_mismatch",
    }
    assert receipt["scope"] == [1, 2]
    assert receipt["state"] == "dispatching"


def test_settlement_reads_the_pinned_meter_not_whatever_unit_is_present(
    project, consented_run
):
    """What the equal-units fixture made unassertable. The injected terms are
    quoted in audio-minutes, so they meter ``audio_seconds``; the
    ``compute_seconds`` on the same fact is a different physical quantity — a
    stopwatch that starts before the container has even downloaded the audio —
    and must not reach the charge.

    Both units are present on the fact and they disagree by a factor of ten.
    Only one of them can produce $0.20.
    """
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(project, run_id, row_id=1, compute_seconds=61.0, audio_seconds=600.0)

    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["metered_unit"] == "audio_seconds"
    assert settlement["metered_quantity"] == "600"  # the audio, not the 61s meter
    assert settlement["billable_quantity"] == "10"
    assert settlement["charge_usd"] == "0.2"


def test_compute_variance_does_not_move_an_injected_offering_bill(
    project, consented_run
):
    """A quantity-unit offering bills its pinned meter, regardless of compute use.

    This run's stopwatch ran 5000 seconds over 600 seconds of audio (a cold
    container, a slow download, diarization on top): more than 8x real time.
    The user is charged for 10 audio-minutes and the operator absorbs the
    variance, which is what the pinned quantity terms mean. Under the wrong
    meter this same run would bill 83.33 audio-minutes, so the bill would move
    with network conditions.
    """
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(project, run_id, row_id=1, compute_seconds=5000.0, audio_seconds=600.0)

    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["billable_quantity"] == "10"  # audio-minutes, not 83.33...
    assert settlement["charge_usd"] == "0.2"
    assert settlement["exceeds_consented"] is False


def test_a_run_that_meters_past_its_consent_is_capped_and_flagged(
    project, consented_run
):
    """The end-to-end shape of the header-lie: consent quoted 10 audio-minutes
    and the file was really 100 minutes long. The contract is
    that the run is not interrupted after the provider effect. The receipt
    keeps the full rated value, caps the customer charge at the confirmed
    amount, and names what the operator absorbed.

    Now that both sides of the comparison are audio, this flag reports a
    genuine overrun (the media header lied) rather than compute noise: the
    stopwatch here reads a mere 42 seconds and changes nothing.
    """
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(project, run_id, row_id=1, compute_seconds=42.0, audio_seconds=6000.0)

    receipt = attempt_receipt(project, attempt.attempt_id)
    settlement = receipt["settlement"]
    assert settlement["rated_charge_usd"] == "2"
    assert settlement["charge_usd"] == "0.2"
    assert settlement["charged_quantity"] == "10"
    assert settlement["absorbed_overage_usd"] == "1.8"
    assert settlement["exceeds_consented"] is True
    assert settlement["billable_quantity"] == "100"
    assert settlement["consented_quantity"] == "10"
    assert receipt["scope"] == [1, 2]
    assert receipt["cost_basis"]["estimated_quantity"] == "10"


def _terminal_success_fact(call_id: str, *, audio_seconds: float) -> dict:
    return {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "parakeet-tdt",
        "provider": "synthetic-venue",
        "provider_kind": "sidecar",
        "credential_source": "platform_key",
        "units": {"audio_seconds": audio_seconds},
    }


def _terminalize_cancelled_attempt(project, run_id: int, attempt_id: str) -> None:
    receipt_id = f"receipt_cancelled_settlement_{attempt_id}"
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id=receipt_id,
            project_id="cancelled-settlement-project",
            action_id=f"action_{attempt_id}",
            action_kind="media.transcribe",
            run_id=run_id,
            status="queued",
        )
    )
    claim = project.db.execute(
        "SELECT claim_token FROM output_column_claims "
        "WHERE run_id=? AND status='active' LIMIT 1",
        (run_id,),
    ).fetchone()
    assert claim is not None
    project.db.execute(
        "UPDATE output_column_claims SET receipt_id=? "
        "WHERE run_id=? AND status='active'",
        (receipt_id, run_id),
    )
    project.db.commit()

    terminalized = terminalize_project_run(
        project,
        run_id=run_id,
        receipt_id=receipt_id,
        status="cancelled",
        authority=CurrentWriterTerminalAuthority(
            writer_attempt_id=attempt_id,
            claim_token=str(claim["claim_token"]),
        ),
    )
    assert terminalized.disposition == "terminalized"


def test_cancelled_result_outcome_is_a_write_once_row_classification(
    project, consented_run
):
    """The result writer and schema share cancellation's third vocabulary."""
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)

    RunResultStore(project).write_results(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "outcome": "cancelled",
                "model_calls": [],
            }
        ],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt.attempt_id,
        ),
    )

    outcome = project.db.execute(
        "SELECT terminal_outcome FROM attempt_row_authorizations "
        "WHERE attempt_id=? AND row_id=1",
        (attempt.attempt_id,),
    ).fetchone()
    assert outcome is not None
    assert outcome["terminal_outcome"] == "cancelled"

    with pytest.raises(RuntimeError, match="terminal outcome is immutable"):
        RunResultStore(project).write_results(
            run_id,
            [
                {
                    "row_id": 1,
                    "column_id": 1,
                    "value": "late success",
                    "outcome": "ok",
                    "model_calls": [
                        _terminal_success_fact(
                            "cancelled-row-late-success",
                            audio_seconds=600,
                        )
                    ],
                }
            ],
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt.attempt_id,
            ),
        )


def test_terminalizer_excludes_cancelled_partial_fact_before_completed_row_settlement(
    project, consented_run
):
    """A cancelled sibling cannot poison a completed row's valid settlement.

    This crosses the producer/consumer seam: result persistence classifies the
    completed row, the terminalizer fills the cancelled row, settlement reads
    both before rating, and an invalid fact owned by the cancelled row is
    excluded individually rather than turning the whole attempt unmetered.
    """
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    authority = _writer_authority(
        project,
        run_id,
        attempt_id=attempt.attempt_id,
    )
    store = RunResultStore(project)
    store.write_results(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "value": "completed transcript",
                "outcome": "ok",
                "model_calls": [
                    _terminal_success_fact(
                        "cancelled-settlement-completed-row",
                        audio_seconds=600,
                    )
                ],
            }
        ],
        **authority,
    )
    store.write_model_calls(
        run_id,
        [
            {
                "row_id": 2,
                "column_id": 1,
                "model_calls": [
                    {
                        **_terminal_success_fact(
                            "cancelled-settlement-partial-row",
                            audio_seconds=0,
                        ),
                        "units": {},
                    }
                ],
            }
        ],
        **authority,
    )

    _terminalize_cancelled_attempt(
        project,
        run_id,
        attempt.attempt_id,
    )

    outcomes = {
        int(row["row_id"]): str(row["terminal_outcome"])
        for row in project.db.execute(
            "SELECT row_id, terminal_outcome "
            "FROM attempt_row_authorizations WHERE attempt_id=? ORDER BY row_id",
            (attempt.attempt_id,),
        ).fetchall()
    }
    assert outcomes == {1: "succeeded", 2: "cancelled"}
    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["terminal_status"] == "cancelled"
    assert settlement["rated_calls"] == 1
    assert settlement["unmetered_calls"] == 0
    assert settlement["charged_quantity"] == "10"
    assert settlement["charge_usd"] == "0.2"


def test_terminalizer_proves_pre_effect_all_cancelled_zero_settlement(
    project, consented_run
):
    """No facts is zero only with the terminalizer's all-cancelled proof."""
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)

    _terminalize_cancelled_attempt(
        project,
        run_id,
        attempt.attempt_id,
    )

    outcomes = project.db.execute(
        "SELECT terminal_outcome FROM attempt_row_authorizations "
        "WHERE attempt_id=? ORDER BY row_id",
        (attempt.attempt_id,),
    ).fetchall()
    assert [str(row["terminal_outcome"]) for row in outcomes] == [
        "cancelled",
        "cancelled",
    ]
    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["terminal_status"] == "cancelled"
    assert settlement["rated_calls"] == 0
    assert settlement["unmetered_calls"] == 0
    assert settlement["rated_charge_usd"] == "0"
    assert settlement["charged_quantity"] == "0"
    assert settlement["charge_usd"] == "0"


def test_all_failed_rows_without_facts_remain_unsettleable(project, consented_run):
    """A failed outcome alone cannot prove that provider spend was zero."""
    from frisket.execution.attempt import set_attempt_state

    run_id = consented_run
    attempt = _mint(project, run_id)
    project.db.execute(
        "UPDATE attempt_row_authorizations SET terminal_outcome='failed' "
        "WHERE attempt_id=?",
        (attempt.attempt_id,),
    )
    project.db.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
    project.db.commit()
    set_attempt_state(project, attempt.attempt_id, "halted")

    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["charge_usd"] is None
    assert settlement["unsettleable"] == "unmetered"


def test_all_failed_quoted_rows_still_rate_returned_provider_facts(
    project, consented_run
):
    """Complete failed-call facts remain rated, but successful-row retail is zero."""
    from frisket.execution.attempt import set_attempt_state

    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(
        project,
        run_id,
        row_id=1,
        compute_seconds=1,
        audio_seconds=60,
        authorized_attempt_id=attempt.attempt_id,
    )
    project.db.execute(
        "UPDATE attempt_row_authorizations SET terminal_outcome='failed' "
        "WHERE attempt_id=?",
        (attempt.attempt_id,),
    )
    project.db.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
    project.db.commit()
    set_attempt_state(project, attempt.attempt_id, "halted")

    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["rated_calls"] == 1
    assert settlement["unmetered_calls"] == 0
    assert settlement["charge_usd"] == "0"


def test_row_settlement_invariants_follow_the_pinned_generic_mode(
    project, consented_run
):
    """The three durable sets are governed by the pinned row mode."""
    run_id = consented_run
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    _record_call(
        project,
        run_id,
        row_id=1,
        compute_seconds=1,
        audio_seconds=600,
    )
    project.db.execute(
        "UPDATE attempt_row_authorizations SET quoted_quantity='11' "
        "WHERE attempt_id=? AND row_id=1",
        (attempt.attempt_id,),
    )
    project.db.commit()
    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["charge_usd"] is None
    assert settlement["unsettleable"] == "row_quote_allocation_mismatch"


def test_a_replayed_batch_cannot_flip_a_failed_quoted_row_to_succeeded(
    project, consented_run
):
    """``attempt_row_authorizations.terminal_outcome`` is write-once.

    Quoted-row settlement debits one full allocation per row whose
    terminal outcome is ``succeeded``, so the terminal slot is the money
    boundary: if a replayed result batch could reclassify a failed row as
    succeeded, the customer debit would grow after the fact, silently. The
    fence has two layers — the pre-write refusal in
    ``_attempt_row_outcomes_for_batch`` and the conditioned UPDATE's rowcount
    backstop — and this test pins the refusal name, the unchanged durable
    state, and the unchanged debit.
    """
    run_id = consented_run
    store = RunResultStore(project)
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)

    # Row 1 carries the whole ten-minute quote and FAILS; only the
    # zero-quoted row 2 succeeds, so the settled customer debit is $0.
    store.write_results(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "error": "provider failed before a usable transcript",
                "outcome": "model_error",
                "model_calls": [],
            },
            {
                "row_id": 2,
                "column_id": 1,
                "value": "transcript 2",
                "outcome": "ok",
                "model_calls": [
                    _terminal_success_fact(
                        "terminal-immutable-row2-meter", audio_seconds=60
                    )
                ],
            },
        ],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt.attempt_id,
        ),
    )
    before = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert before["charged_quantity"] == "0"
    assert before["charge_usd"] == "0"

    # The replay: the same durable batch arrives again, but this time row 1
    # claims success and brings a meter — through the exact store path a
    # crash-replay result write uses.
    replay = [
        {
            "row_id": 1,
            "column_id": 1,
            "value": "late replayed transcript",
            "outcome": "ok",
            "model_calls": [
                _terminal_success_fact(
                    "terminal-immutable-row1-replay", audio_seconds=600
                )
            ],
        }
    ]
    with pytest.raises(RuntimeError, match="terminal outcome is immutable"):
        store.write_results(
            run_id,
            replay,
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt.attempt_id,
            ),
        )

    outcomes = {
        int(row["row_id"]): row["terminal_outcome"]
        for row in project.db.execute(
            "SELECT row_id, terminal_outcome FROM attempt_row_authorizations "
            "WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchall()
    }
    assert outcomes == {1: "failed", 2: "succeeded"}

    # The refusal fires before the write and rolls the whole batch back:
    # no replayed result cell, no replayed meter.
    result = project.db.execute(
        "SELECT value, outcome FROM results WHERE run_id=? AND row_id=1",
        (run_id,),
    ).fetchone()
    assert result is not None
    assert result["value"] is None
    assert result["outcome"] == "model_error"
    assert (
        project.db.execute(
            "SELECT id FROM model_calls WHERE id=?",
            ("terminal-immutable-row1-replay",),
        ).fetchone()
        is None
    )

    # The money consequence the fence exists for: the successful-row debit
    # did not grow. A flipped row 1 would have minted the "0.2" charge.
    after = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert after["charged_quantity"] == "0"
    assert after["charge_usd"] == "0"
    assert after["charge_usd"] != "0.2"


def test_a_replayed_batch_cannot_flip_a_succeeded_row_to_failed(project, consented_run):
    """Write-once cuts both ways. Any-failure-wins governs classification
    WITHIN one batch; across batches the first terminal outcome is final,
    so a late failure replay cannot un-settle an already-debited row."""
    run_id = consented_run
    store = RunResultStore(project)
    attempt = _mint(project, run_id)
    _claim_output_attempt(project, attempt)
    store.write_results(
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "value": "transcript 1",
                "outcome": "ok",
                "model_calls": [
                    _terminal_success_fact(
                        "terminal-immutable-row1-meter", audio_seconds=600
                    )
                ],
            }
        ],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt.attempt_id,
        ),
    )

    with pytest.raises(RuntimeError, match="terminal outcome is immutable"):
        store.write_results(
            run_id,
            [
                {
                    "row_id": 1,
                    "column_id": 1,
                    "error": "late replayed failure",
                    "outcome": "model_error",
                    "model_calls": [],
                }
            ],
            **_writer_authority(
                project,
                run_id,
                attempt_id=attempt.attempt_id,
            ),
        )

    outcome = project.db.execute(
        "SELECT terminal_outcome FROM attempt_row_authorizations "
        "WHERE attempt_id=? AND row_id=1",
        (attempt.attempt_id,),
    ).fetchone()
    assert outcome is not None
    assert outcome["terminal_outcome"] == "succeeded"


def test_no_settlement_table_was_added(project):
    """Mechanically: exactly one new column, no new table.
    A future release that reaches for a settlement table has to delete this
    test and say why."""
    tables = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert not [t for t in tables if "settlement" in t or "charge" in t]
    columns = {
        row["name"]
        for row in project.db.execute("PRAGMA table_info(model_calls)").fetchall()
    }
    assert "attempt_id" in columns
    assert not [c for c in columns if "rate" in c or "charge" in c or "price" in c]


def test_injected_row_terms_charge_only_successful_quoted_allocations(
    project,
):
    """Forty one-minute rows are confirmed; only 38 finish successfully.

    Every successful provider fact reports two actual minutes, so aggregate
    metering reaches 76 minutes.  Neither the full confirmed ceiling (40
    minutes) nor the actual meter is the debit: the pinned promise is
    one full quoted row per successful terminal row, hence exactly 38 minutes.
    """
    sheet_id = project.add_sheet("Forty files")
    source_column_id = project.add_column(sheet_id, "audio", type="audio")
    output_column_id = project.add_column(sheet_id, "transcript", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [{"audio": {"duration_seconds": 60}} for _ in range(40)],
        {"audio": source_column_id},
    )
    op_id = project.db.execute(
        "INSERT INTO ops (kind, spec) VALUES (?,?)", ("transcribe", "{}")
    ).lastrowid
    project.db.commit()
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        params=SPEC,
        row_ids=row_ids,
    )
    claim_token = f"output-claim:settlement-offering:{run_id}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["transcript"],
        action_kind="media.transcribe",
        run_id=run_id,
        op_id=int(op_id),
        claim_token=claim_token,
    )
    assert conflict is None
    assert len(claims) == 1
    OutputColumnClaimStore(project).bind_to_run(
        claim_token=claim_token,
        run_id=run_id,
        expected_output_names=["transcript"],
    )

    promises = json.loads(json.dumps(PROMISES))
    cost_promise = next(promise for promise in promises if promise["field"] == "cost")
    cost_basis = cost_promise["basis"]
    cost_basis["estimated_quantity"] = "40"
    cost_basis["row_quote_quantities"] = [
        {"row_id": row_id, "quantity": "1"} for row_id in row_ids
    ]
    cost_promise["value"] = "0.8"
    store = RouteStore.for_run(project, run_id)
    promise_set = store.append_promise_set(promises=promises, predecessor_id=None)
    store.record_consent(
        action_identity_hash=action_identity_hash(SPEC),
        promise_set_hash=promise_set.promise_set_hash,
        actor=instance_principal(project),
    )
    store.append_route(
        promise_set_id=promise_set.id,
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

    attempt = AttemptAuthority(
        project,
        composition=_execution_composition(),
    ).mint(
        recipe=TRANSCRIPTION_PLAN.program,
        spec=SPEC,
        run_id=run_id,
        scope=tuple(row_ids),
    )
    _claim_output_attempt(project, attempt)
    successes = [
        {
            "row_id": row_id,
            "column_id": output_column_id,
            "value": f"transcript {row_id}",
            "outcome": "ok",
            "model_calls": [
                {
                    "id": f"offering-success-{row_id}",
                    "fact_version": "frisket.model-call-fact.v1",
                    "capability": "llm.complete",
                    "engine": "parakeet-tdt",
                    "provider": "synthetic-venue",
                    "provider_kind": "sidecar",
                    "credential_source": "platform_key",
                    "units": {"audio_seconds": 120},
                }
            ],
        }
        for row_id in row_ids[:38]
    ]
    failures = [
        {
            "row_id": row_id,
            "column_id": output_column_id,
            "error": "provider failed before a usable transcript",
            "outcome": "model_error",
            "model_calls": [],
        }
        for row_id in row_ids[38:]
    ]
    # Provider facts alone are not proof a quoted row completed. Before the
    # terminal result write, preserve actual usage but refuse any debit.
    RunResultStore(project).write_model_calls(
        run_id,
        successes,
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt.attempt_id,
        ),
    )
    incomplete = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert incomplete["rated_charge_usd"] == "1.52"
    assert incomplete["charge_usd"] is None
    assert incomplete["unsettleable"] == "row_terminal_outcome_missing"

    RunResultStore(project).write_results(
        run_id,
        [*successes, *failures],
        **_writer_authority(
            project,
            run_id,
            attempt_id=attempt.attempt_id,
        ),
    )

    settlement = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settlement["consented_quantity"] == "40"
    assert settlement["billable_quantity"] == "76"
    assert settlement["rated_charge_usd"] == "1.52"
    assert settlement["charged_quantity"] == "38"
    assert settlement["charge_usd"] == "0.76"
    assert settlement["absorbed_overage_usd"] == "0.76"

    # A durable allocation row is an ownership join, not a second editable
    # price. Contradicting the consented basis keeps actual evidence but drops
    # the customer charge.
    project.db.execute(
        "UPDATE attempt_row_authorizations SET quoted_quantity='2' "
        "WHERE attempt_id=? AND row_id=?",
        (attempt.attempt_id, row_ids[0]),
    )
    project.db.commit()
    contradictory = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert contradictory["rated_charge_usd"] == "1.52"
    assert contradictory["charge_usd"] is None
    assert contradictory["unsettleable"] == "row_quote_allocation_mismatch"

    legacy_basis = json.loads(
        project.db.execute(
            "SELECT cost_basis_json FROM execution_attempts WHERE id=?",
            (attempt.attempt_id,),
        ).fetchone()["cost_basis_json"]
    )
    legacy_basis.pop("row_quote_quantities")
    project.db.execute(
        "UPDATE execution_attempts SET cost_basis_json=? WHERE id=?",
        (json.dumps(legacy_basis, sort_keys=True), attempt.attempt_id),
    )
    project.db.commit()
    missing = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert missing["rated_charge_usd"] == "1.52"
    assert missing["charge_usd"] is None
    assert missing["unsettleable"] == "row_quote_allocation_missing"
