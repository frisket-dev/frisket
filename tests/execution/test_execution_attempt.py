"""The ``ExecutionAttempt`` commitment.

The surviving acceptance criteria are that a user can inspect an attempt and
read all six receipt facts, a ``MapRunner`` built without an authority raises
``TypeError``, and a claim is refused if the route head moves under it. This
module also pins the construction-closure discipline and the bounded-age
``abandoned`` detector.

The bundle-migration criterion was retired with the migration layer it tested:
``schema.py`` is authoritative and a bundle from another build is
refused, not converted (``tests/engine/test_bundle_schema_fence.py``).
"""

from __future__ import annotations

import ast
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.execution.attempt import (
    AttemptClaimRefused,
    abandon_stale_dispatching_attempts,
    attempt_receipt,
    claim,
    run_attempt_receipts,
    set_attempt_state,
)
from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.resolve_for_action import action_identity_hash
from tests.deterministic_time import controlled_time


def _transcription_plan():
    action = ACTION_REGISTRY.get("media.transcribe")
    return _typed_map_rows_plan(
        BoundTypedActionRequest.bind(
            action,
            ActionRequest(
                action_id=action.action_id,
                scope=SheetRows(sheet_id=1),
                params={"source": "media", "engine": "parakeet-tdt"},
                idempotency_key="attempt-authority-test",
            ),
        )
    )


SPEC = dict(_transcription_plan().spec)

PROMISES = [
    {
        "field": "egress_class",
        "op": "eq",
        "value": "operator_lan",
        "basis": None,
        "order_ref": None,
        "audience": "user_claim",
    },
    {
        "field": "cost",
        "op": "le",
        "value": "1.00",
        "basis": {
            "pricing_key": "test.synthetic.audio_minute",
            "unit_rate": "0.01",
            "estimated_quantity": "100",
            "quantity_unit": "audio_minute",
            "terms_version": "test.synthetic.terms.v1",
            "quantity_rounding_mode": "half_even",
            "quantity_rounding_decimal_places": 6,
            "meter_key": "audio_seconds",
            "meter_units_per_quantity_unit": "60",
            "ceiling_mode": "consented_quantity",
            "row_settlement_mode": "all_metered",
            "charge_authority": "test.synthetic.authority",
            "hardware_class": None,
            "throughput_ref": None,
        },
        "order_ref": None,
        "audience": "user_claim",
    },
]


class _PricedProvider:
    """One live gateway target whose exact offering satisfies the promises."""

    def targets(self):
        from frisket.contracts.transcription_sidecar import (
            TranscriptionOptionSupport,
        )
        from frisket.execution.targets import (
            CAPABILITY_TRANSCRIBE,
            ExecutionTarget,
            TargetEngineSupport,
        )

        return (
            ExecutionTarget(
                id="models-gateway",
                operator="self",
                egress_class="operator_lan",
                engines=(
                    TargetEngineSupport(
                        engine="parakeet-tdt",
                        transport="frisket.transcription.v1",
                        capability=CAPABILITY_TRANSCRIBE,
                        options=TranscriptionOptionSupport(
                            diarization_mode="optional",
                            speaker_hint="none",
                            language=False,
                            model_size=False,
                            vad=True,
                            context=False,
                        ),
                    ),
                ),
            ),
        )

    def connection(self, target_id: str):
        from frisket.execution.provider import ConnectionConfig

        if target_id != "models-gateway":
            return None
        return ConnectionConfig(
            base_url="http://models.example.internal",
            token="test-token",
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
    """A run with the persisted artifacts a routed admission needs: a promise
    set, the consent the user gave for it, and the head route."""
    sheet_id = project.add_sheet("S")
    op_id = project.db.execute(
        "INSERT INTO ops (kind, spec) VALUES (?,?)",
        ("transcribe", "{}"),
    ).lastrowid
    run_id = project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind, params) VALUES (?,?,?,?)",
        (op_id, sheet_id, "media.transcribe", json.dumps(SPEC)),
    ).lastrowid
    project.db.commit()
    return (run_id, *_consent_and_route(project, run_id))


def _consent_and_route(project, run_id):
    """The persisted artifacts a routed admission needs, for a run that
    already exists: a promise set, the consent the user gave for it, and the
    head route."""
    store = RouteStore.for_run(project, run_id)
    promise_set = store.append_promise_set(promises=PROMISES, predecessor_id=None)
    consent = store.record_consent(
        action_identity_hash=action_identity_hash(SPEC),
        promise_set_hash=promise_set.promise_set_hash,
        actor=instance_principal(project),
    )
    route = store.append_route(
        promise_set_id=promise_set.id,
        engine="parakeet-tdt",
        options={},
        target_snapshot={
            "target_id": "models-gateway",
            "capability": "transcribe",
            "transport": "frisket.transcription.v1",
            "run_scoped": False,
        },
        operator="self",
        egress_class="operator_lan",
        region=None,
        credential_source="platform_key",
        cost_posture="platform_metered",
        predecessor_id=None,
    )
    return route, promise_set, consent


def _mint(project, run_id, *, scope=(7, 11)):
    from frisket.execution.commercial import (
        CommercialOffering,
        CommercialOfferingMatch,
        CommercialPresentation,
        CommercialQuoteRounding,
        CommercialQuoteTerms,
        CommercialSettlementTerms,
    )
    from frisket.execution.credential_use import CredentialUseContext
    from frisket.execution.provider import CompositionFacts, ExecutionComposition
    from frisket.execution.price_book import PlatformMetered

    offering = CommercialOffering(
        match=CommercialOfferingMatch(
            target_id="models-gateway",
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.synthetic.audio_minute",
            terms_version="test.synthetic.terms.v1",
            unit_rate="0.01",
            quantity_unit="audio_minute",
            rounding=CommercialQuoteRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode="all_metered",
        ),
        charge_authority="test.synthetic.authority",
        presentation=CommercialPresentation(
            venue_label="Synthetic gateway",
            billing_label="Synthetic billing authority",
        ),
    )

    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-test",
            funding=PlatformMetered(),
        ),
        provider=_PricedProvider(),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=(offering,),
    )
    return AttemptAuthority(project, composition=composition).mint(
        recipe=_transcription_plan().program, spec=SPEC, run_id=run_id, scope=scope
    )


def _claim_unscoped(project, commitment) -> None:
    """These lifecycle probes carry no output-column effect."""

    claim(project, commitment, claimless_direct_effect=True)


def test_claim_requires_an_explicit_claimed_or_claimless_mode(
    project,
    consented_run,
):
    run_id, _route, _set, _consent = consented_run
    attempt = _mint(project, run_id)
    with pytest.raises(TypeError, match="claimless_direct_effect"):
        claim(project, attempt)
    assert attempt_receipt(project, attempt.attempt_id)["state"] == "admitted"


def test_claimless_dispatch_refuses_an_output_column_plan(
    project,
    consented_run,
):
    run_id, _route, _set, _consent = consented_run
    attempt = _mint(project, run_id)
    with pytest.raises(
        AttemptClaimRefused,
        match="cannot authorize output columns",
    ):
        claim(
            project,
            attempt,
            output_column_ids=frozenset({123}),
            claimless_direct_effect=True,
        )
    assert attempt_receipt(project, attempt.attempt_id)["state"] == "admitted"


# ---------------------------------------------------------------------------
# (c) A MapRunner built without an authority is a TypeError.
# ---------------------------------------------------------------------------


def test_maprunner_without_an_authority_is_a_type_error(project):
    """The fix for the recurring 'optional wiring' shape, stated as a test:
    the capability is required AT THE PLACE THE WORK HAPPENS, so a caller
    cannot forget it. Previously, eleven construction sites were each
    trusted to remember a keyword and an effect-site fence existed to notice
    when one didn't."""
    with pytest.raises(TypeError) as exc_info:
        MapRunner(project, ModelRouter())
    assert "authority" in str(exc_info.value)

    # Positional is refused too — it is keyword-only, so the argument is
    # named at every site rather than hiding in a position.
    with pytest.raises(TypeError):
        MapRunner(project, ModelRouter(), 1, None, None, {}, False, None, object())


# ---------------------------------------------------------------------------
# The six receipt facts.
# ---------------------------------------------------------------------------


def test_attempt_receipt_answers_all_six_facts(project, consented_run):
    from frisket.engine.store.execution_routes import CONSENT_BASIS_CONFIRMED

    run_id, route, promise_set, consent = consented_run
    attempt = _mint(project, run_id)
    _claim_unscoped(project, attempt)

    receipt = attempt_receipt(project, attempt.attempt_id)
    assert receipt is not None

    # 1. WHICH ROWS it covered.
    assert receipt["scope"] == [7, 11]

    # 2. WHICH TARGET/PROVIDER the data went to.
    assert receipt["target"] == {
        "target_id": "models-gateway",
        "transport": "frisket.transcription.v1",
        "engine": "parakeet-tdt",
        "operator": "self",
        "egress_class": "operator_lan",
        "region": None,
        "credential_source": "platform_key",
    }

    # 3. WHICH CONSENT authorized it, and
    # 4. whether that consent was DIRECT or DERIVED. This is
    #    ``consents.grant_basis``'s first reader — the whole reason B6
    #    resolved as an unconditional KEEP.
    assert receipt["consent"]["id"] == consent.id
    assert receipt["consent"]["grant_basis"] == CONSENT_BASIS_CONFIRMED
    assert receipt["consent"]["promise_set_hash"] == promise_set.promise_set_hash

    # 5. WHICH COST BASIS and PRICE CARD were authorized — read back off the
    #    promise row the user actually confirmed, not re-minted from live
    #    rates.
    assert receipt["cost_basis"] == {
        "kind": "priced",
        "pricing_key": "test.synthetic.audio_minute",
        "unit_rate": "0.01",
        "estimated_quantity": "100",
        "quantity_unit": "audio_minute",
        "terms_version": "test.synthetic.terms.v1",
        "quantity_rounding_mode": "half_even",
        "quantity_rounding_decimal_places": 6,
        "meter_key": "audio_seconds",
        "meter_units_per_quantity_unit": "60",
        "ceiling_mode": "consented_quantity",
        "row_settlement_mode": "all_metered",
        "charge_authority": "test.synthetic.authority",
    }
    assert receipt["price_card_version"] == "test.synthetic.terms.v1"

    # 6. Its TERMINAL OUTCOME (here: mid-flight, having claimed).
    assert receipt["state"] == "dispatching"

    # And the evidence behind the admission: which claims were evaluated and
    # why it passed. A consent pointer alone would prove a consent row
    # existed, not that.
    fields = {row["field"] for row in receipt["evaluation"]["results"]}
    assert "egress_class" in fields
    assert "blocked" not in receipt["evaluation"]
    assert all("action" not in row for row in receipt["evaluation"]["results"])
    assert receipt["action_identity_hash"] == action_identity_hash(SPEC)
    assert receipt["target"]["target_id"] == route.target_snapshot["target_id"]


def test_a_retry_mints_a_new_attempt_rather_than_mutating_the_old_one(
    project, consented_run
):
    """Same run, ``seq+1``, a NEW scope, a fresh admission. That is how
    a user sees two attempts covering a row that was paid for twice, and it
    is why ``execution_attempts`` never grows result columns."""
    run_id, _route, promise_set, consent = consented_run
    first = _mint(project, run_id, scope=(7, 11))
    _claim_unscoped(project, first)
    second = _mint(project, run_id, scope=(11,))

    assert second.attempt_id != first.attempt_id
    assert second.seq == first.seq + 1
    receipts = run_attempt_receipts(project, run_id)
    assert [r["seq"] for r in receipts] == [1, 0]  # newest first
    assert [r["scope"] for r in receipts] == [[11], [7, 11]]
    assert [
        (receipt["consent"]["id"], receipt["consent"]["promise_set_hash"])
        for receipt in receipts
    ] == [(consent.id, promise_set.promise_set_hash)] * 2


def test_the_attempt_receipt_route_serves_the_facts(tmp_path):
    """The acceptance criterion is that a USER can inspect an attempt, so the
    facts have to be reachable over HTTP, not only from Python."""
    from frisket.server.app import create_app

    app = create_app(tmp_path / "ws")
    created = app.state.workspace.create("attempts")
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None)
        == "/api/projects/{pid}/actions/runs/{run_id}/attempts"
    )
    assert route.endpoint(created["id"], 1) == {"run_id": 1, "attempts": []}


# ---------------------------------------------------------------------------
# The bounded-age detector feeding ``abandoned``.
# ---------------------------------------------------------------------------


def test_stale_dispatching_attempts_are_closed_before_a_retry_is_admitted(
    project, consented_run
):
    """A crash mid-dispatch leaves a ``dispatching`` row. Left alone it would
    make every future reconsent on that run 409 forever, so recovery closes
    it EXPLICITLY before the next attempt is admitted. This is a detector;
    the claim transaction provides the structural exclusion."""
    run_id, _route, _set, _consent = consented_run
    crashed = _mint(project, run_id)
    _claim_unscoped(project, crashed)

    # Age the row past the bound, exactly as a crashed worker would leave it.
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    project.db.execute(
        "UPDATE execution_attempts SET created_at=? WHERE id=?",
        (old, crashed.attempt_id),
    )
    project.db.commit()

    assert abandon_stale_dispatching_attempts(project, run_id) == 1
    assert attempt_receipt(project, crashed.attempt_id)["state"] == "abandoned"
    # The run no longer points at a live attempt, so reconsent is reachable
    # again.
    assert (
        project.db.execute(
            "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()["current_attempt_id"]
        is None
    )
    # A fresh mint runs the detector itself, so recovery needs no operator.
    assert _mint(project, run_id).seq == 1


def test_a_live_dispatching_attempt_is_never_abandoned(project, consented_run):
    """The bound is what makes the detector safe: a young dispatching attempt
    is a run that is working, not a crash."""
    run_id, _route, _set, _consent = consented_run
    live = _mint(project, run_id)
    _claim_unscoped(project, live)
    assert abandon_stale_dispatching_attempts(project, run_id) == 0
    assert attempt_receipt(project, live.attempt_id)["state"] == "dispatching"


# ---------------------------------------------------------------------------
# ONE dispatch per run.
# ---------------------------------------------------------------------------


def test_two_threads_claiming_one_run_produce_exactly_one_dispatch(
    project, consented_run
):
    """The double-billing critical, as its reproduction.

    Two admitted attempts on one run, two REAL threads, one bundle. The claim
    CAS used to ask only ``WHERE id=? AND state='admitted'`` — a question
    about the attempt, never about the run — so both won and both dispatched:
    two workers transcribing the same rows against the same paid route, the
    user billed twice, duplicate cell writes, nothing refusing. It is reachable
    from a worker lease expiring under a live handler.

    Exactly one claims; the loser gets the TYPED refusal (never a bare
    IntegrityError, which callers would surface as an untyped job retry that
    claims again).
    """
    run_id, _route, _set, _consent = consented_run
    first = _mint(project, run_id)
    second = _mint(project, run_id)

    gate = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def _claim(name, commitment):
        gate.wait()
        try:
            _claim_unscoped(project, commitment)
            outcomes[name] = "claimed"
        except AttemptClaimRefused as exc:
            outcomes[name] = exc

    with controlled_time() as clock:
        threads = [
            clock.background(partial(_claim, "first", first)),
            clock.background(partial(_claim, "second", second)),
        ]
        for thread in threads:
            thread.join(timeout=30)

    assert sorted(map(str, outcomes)) == ["first", "second"]
    winners = [name for name, out in outcomes.items() if out == "claimed"]
    assert len(winners) == 1, outcomes
    loser = next(out for out in outcomes.values() if out != "claimed")
    assert isinstance(loser, AttemptClaimRefused), loser
    assert loser.code == "stale_head"
    assert "already dispatching" in str(loser)

    dispatching = project.db.execute(
        "SELECT id FROM execution_attempts WHERE run_id=? AND state='dispatching'",
        (run_id,),
    ).fetchall()
    assert len(dispatching) == 1
    # And the run points at the one that won.
    assert (
        project.db.execute(
            "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()["current_attempt_id"]
        == dispatching[0]["id"]
    )


def test_the_partial_unique_index_backstops_the_claim_cas(project, consented_run):
    """The structural half, checked without going through ``claim``: even a
    direct write cannot put two dispatching attempts on one run. The CAS
    protects writers that take the claim lock; the index makes the state
    unrepresentable, which is what ``uq_jobs_active_dedupe`` does for the
    analogous queue race."""
    run_id, _route, _set, _consent = consented_run
    first = _mint(project, run_id)
    second = _mint(project, run_id)
    _claim_unscoped(project, first)
    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute(
            "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
            (second.attempt_id,),
        )
    project.db.rollback()


def test_lock_contention_is_the_typed_refusal_not_a_bare_sqlite_error(
    project, consented_run
):
    """``BEGIN IMMEDIATE`` losing the write lock is a race outcome, not an
    infrastructure fault. It has to arrive in the vocabulary the caller
    already handles (``AttemptClaimRefused`` -> the durable reconsent-shaped
    failure), because a bare ``sqlite3.OperationalError`` surfaces as an
    untyped job failure — which retries, and claims again."""
    run_id, _route, _set, _consent = consented_run
    attempt = _mint(project, run_id)
    project.db.execute("PRAGMA busy_timeout=50")
    holder = sqlite3.connect(project.db_path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(AttemptClaimRefused) as exc_info:
            _claim_unscoped(project, attempt)
        assert exc_info.value.code == "stale_head"
        assert "nothing was dispatched" in str(exc_info.value)
    finally:
        holder.rollback()
        holder.close()
        project.db.execute("PRAGMA busy_timeout=10000")
    # Nothing moved: the claim never took the lock.
    assert attempt_receipt(project, attempt.attempt_id)["state"] == "admitted"


def test_a_terminal_transition_releases_the_run_for_the_next_dispatch(
    project, consented_run
):
    """The pointer stops lying, and the slot is genuinely freed: every
    terminal state clears ``runs.current_attempt_id`` and lets the next
    attempt claim."""
    run_id, _route, _set, _consent = consented_run
    for terminal in ("effected", "halted", "superseded", "abandoned"):
        attempt = _mint(project, run_id)
        _claim_unscoped(project, attempt)
        assert (
            project.db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()["current_attempt_id"]
            == attempt.attempt_id
        )
        set_attempt_state(project, attempt.attempt_id, terminal)
        assert attempt_receipt(project, attempt.attempt_id)["state"] == terminal
        assert (
            project.db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()["current_attempt_id"]
            is None
        )


# ---------------------------------------------------------------------------
# The construction-closure discipline.
# ---------------------------------------------------------------------------


def test_only_the_two_attempt_modules_construct_a_commitment():
    """The opaque-handle principle's checkable half. This is a DISCIPLINE,
    not a guard — a single-process laptop app cannot rest consent on the
    authenticity of a dataclass — but keeping construction in two named
    modules is what makes 'consent-critical consumption loads the persisted
    row' an auditable claim rather than a hope."""
    src = Path(__file__).resolve().parents[2] / "src" / "frisket"
    constructing: set[str] = set()
    for path in src.rglob("*.py"):
        # rule19: closure fence — AttemptCommitment construction pinned to the two named modules
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AttemptCommitment"
            ):
                constructing.add(
                    str(path.relative_to(src.parent)).replace("/", ".")[: -len(".py")]
                )
    # ``attempt`` DEFINES the type; ``attempt_authority`` is the one place
    # that CONSTRUCTS one, so the scan asserts construction never leaks
    # outside that pair.
    assert constructing <= {
        "frisket.execution.attempt",
        "frisket.execution.attempt_authority",
    }, constructing
    assert "frisket.execution.attempt_authority" in constructing


# ---------------------------------------------------------------------------
# Halt state and edition context are COLUMNS — relocation, not
# retirement. ``revert_run_resume`` snapshots and restores them exactly the
# way it restores ``params``, because a run whose resume refused before the
# first row must be indistinguishable from one never resumed, awaiting-
# reconsent markers included.
# ---------------------------------------------------------------------------


def _halt_columns(project, run_id: int) -> tuple[str | None, str | None]:
    row = project.db.execute(
        "SELECT halted_code, halted_reason FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    return row["halted_code"], row["halted_reason"]


@pytest.mark.parametrize(
    (
        "halted_code",
        "halted_reason",
        "expected_stored_halt",
        "expected_reader_halt",
        "resume_admitted",
    ),
    (
        (
            "promise_violation",
            None,
            ("promise_violation", ""),
            ("promise_violation", None),
            True,
        ),
        (
            None,
            "halted after 10 failures",
            (None, "halted after 10 failures"),
            (None, "halted after 10 failures"),
            False,
        ),
        (None, None, (None, None), (None, None), False),
    ),
)
def test_set_run_halt_keeps_both_reader_representations_in_sync(
    project,
    consented_run,
    halted_code,
    halted_reason,
    expected_stored_halt,
    expected_reader_halt,
    resume_admitted,
):
    from frisket.engine.store.runs import RunResultStore
    from frisket.server.run_status import _run_halt

    run_id, _route, _set, _consent = consented_run
    store = RunResultStore(project)
    if halted_code is None and halted_reason is None:
        project.db.execute(
            "UPDATE runs SET params=?, halted_code=?, halted_reason=? WHERE id=?",
            (
                json.dumps(
                    {
                        **SPEC,
                        "halted_code": "stale_halt",
                        "halted_reason": "stale reason",
                    }
                ),
                "stale_halt",
                "stale reason",
                run_id,
            ),
        )
        project.db.commit()

    store.set_run_halt(run_id, halted_code, halted_reason)

    row = store.get_run(run_id)
    assert row is not None
    assert _run_halt(row) == expected_reader_halt
    assert _halt_columns(project, run_id) == expected_stored_halt
    params = json.loads(row["params"])
    if expected_stored_halt == (None, None):
        assert "halted_code" not in params
        assert "halted_reason" not in params
    else:
        assert params.get("halted_code") == expected_stored_halt[0]
        assert params.get("halted_reason") == expected_stored_halt[1]

    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at='2026-08-06' WHERE id=?",
        (run_id,),
    )
    project.db.commit()
    admission = store.begin_run_resume(
        run_id,
        reopen_operator_cancelled=False,
        resumable_halt_codes=("promise_violation",),
    )
    assert admission.admitted is resume_admitted
    assert (
        admission.prior_halted_code,
        admission.prior_halted_reason,
    ) == expected_stored_halt


def test_set_run_halt_owns_only_its_default_transaction(project, consented_run):
    from frisket.engine.store.runs import RunResultStore

    run_id, _route, _set, _consent = consented_run
    store = RunResultStore(project)
    statements = []
    project.db.set_trace_callback(statements.append)
    try:
        store.set_run_halt(run_id, "promise_violation", "re-consent")
    finally:
        project.db.set_trace_callback(None)

    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"
    assert project.db.in_transaction is False

    project.db.execute("BEGIN IMMEDIATE")
    store.set_run_halt(run_id, None, "temporary legacy halt", commit=False)
    assert project.db.in_transaction is True
    assert _halt_columns(project, run_id) == (None, "temporary legacy halt")
    project.db.rollback()

    row = store.get_run(run_id)
    assert row is not None
    assert _halt_columns(project, run_id) == ("promise_violation", "re-consent")
    assert json.loads(row["params"])["halted_code"] == "promise_violation"


def test_halt_columns_are_written_cleared_and_restored(project, consented_run):
    from frisket.engine.store.runs import RunResultStore

    run_id, _route, _set, _consent = consented_run
    store = RunResultStore(project)

    # Written by the one finalization writer.
    store.set_run_halt(run_id, "promise_violation", "re-consent to resume")
    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at='2026-07-25' WHERE id=?",
        (run_id,),
    )
    project.db.commit()
    assert _halt_columns(project, run_id) == (
        "promise_violation",
        "re-consent to resume",
    )

    # Cleared by resume admission...
    admission = store.begin_run_resume(
        run_id,
        reopen_operator_cancelled=False,
        resumable_halt_codes=("promise_violation",),
    )
    assert admission.admitted is True
    assert admission.prior_halted_code == "promise_violation"
    assert _halt_columns(project, run_id) == (None, None)

    # ...and restored verbatim when that resume refuses before dispatch.
    store.revert_run_resume(run_id, admission)
    assert _halt_columns(project, run_id) == (
        "promise_violation",
        "re-consent to resume",
    )
    assert (
        project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[
            "status"
        ]
        == "cancelled"
    )


def test_losing_resume_claimant_does_not_revert_the_live_winner(project, consented_run):
    """A resume rollback belongs only to the invocation that transiently
    reopened the run.

    A reopens a resumable cancelled run and mints its attempt. B observes the
    transient ``running`` state, is also admitted, and wins the one-dispatch
    claim. A then loses that claim. Its pre-dispatch cleanup must mutate
    nothing shared: in particular, it must not restore A's earlier
    ``cancelled`` snapshot over B's live dispatch.
    """
    from frisket.engine.store.runs import RunResultStore

    run_id, _route, _set, _consent = consented_run
    store = RunResultStore(project)
    store.set_run_halt(run_id, "promise_violation", "re-consent to resume")
    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at='2026-07-25' WHERE id=?",
        (run_id,),
    )
    project.db.commit()

    first_admission = store.begin_run_resume(
        run_id,
        reopen_operator_cancelled=False,
        resumable_halt_codes=("promise_violation",),
    )
    first = _mint(project, run_id)
    second_admission = store.begin_run_resume(
        run_id,
        reopen_operator_cancelled=False,
        resumable_halt_codes=("promise_violation",),
    )
    second = _mint(project, run_id)
    assert first_admission.admitted is True
    assert second_admission.admitted is True

    _claim_unscoped(project, second)
    with pytest.raises(AttemptClaimRefused):
        _claim_unscoped(project, first)
    store.revert_run_resume(run_id, first_admission)

    row = project.db.execute(
        "SELECT status, finished_at, halted_code, halted_reason, "
        "current_attempt_id FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert dict(row) == {
        "status": "running",
        "finished_at": None,
        "halted_code": None,
        "halted_reason": None,
        "current_attempt_id": second.attempt_id,
    }
    assert attempt_receipt(project, second.attempt_id)["state"] == "dispatching"


def test_halted_reason_without_a_code_stays_representable(project, consented_run):
    """The legacy consecutive-failure circuit breaker sets a reason with NO
    typed code. Collapsing the two columns would reclassify its halts as
    typed recipe-invocation halts, so the pair is deliberately independent."""
    from frisket.engine.store.runs import RunResultStore

    run_id, _route, _set, _consent = consented_run
    RunResultStore(project).set_run_halt(run_id, None, "halted after 10 failures")
    assert _halt_columns(project, run_id) == (None, "halted after 10 failures")


def test_running_queue_admission_preserves_racing_cancel_intent(project, consented_run):
    """A prepared queued run is already running when resume admission starts."""

    from frisket.engine.store.runs import RunResultStore

    run_id, _route, _set, _consent = consented_run
    store = RunResultStore(project)
    assert store.request_cancel(run_id) is True
    requested_at = project.db.execute(
        "SELECT status, cancel_requested_at FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert requested_at["status"] == "running"
    requested_at = requested_at["cancel_requested_at"]

    admission = store.begin_run_resume(
        run_id,
        reopen_operator_cancelled=False,
    )

    assert admission.admitted is True
    assert admission.prior_cancel_requested_at == requested_at
    assert admission.transient_cancel_requested_at == requested_at
    assert store.cancellation_requested(run_id) is True


def test_terminal_resume_clears_and_refused_revert_restores_cancel_intent(
    project, consented_run
):
    from frisket.engine.store.runs import RunResultStore

    run_id, _route, _set, _consent = consented_run
    store = RunResultStore(project)
    assert store.request_cancel(run_id) is True
    requested_at = project.db.execute(
        "SELECT cancel_requested_at FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()["cancel_requested_at"]
    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at='2026-07-25' WHERE id=?",
        (run_id,),
    )
    project.db.commit()

    admission = store.begin_run_resume(run_id)
    assert admission.admitted is True
    assert admission.prior_cancel_requested_at == requested_at
    assert admission.transient_cancel_requested_at is None
    assert store.has_cancel_intent(run_id) is False

    store.revert_run_resume(run_id, admission)
    restored = project.db.execute(
        "SELECT status, finished_at, cancel_requested_at FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert tuple(restored) == ("cancelled", "2026-07-25", requested_at)


def test_edition_run_context_lands_on_its_column_not_in_params(project, consented_run):
    """Edition context was always write-only inside ``params`` — no src reader,
    one identity carve-out, one pop in the preview path — so the relocation
    takes ``start_run``'s INSERT one step closer to being the spec blob's only
    writer."""
    from frisket.server.action_enqueue import attach_run_edition_run_context

    run_id, _route, _set, _consent = consented_run
    attach_run_edition_run_context(project, run_id, {"edition": "open"})
    row = project.db.execute(
        "SELECT params, edition_run_context FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert json.loads(row["edition_run_context"]) == {"edition": "open"}
    assert "edition_run_context" not in json.loads(row["params"])

    # Idempotent: an already-attached context is never overwritten.
    attach_run_edition_run_context(project, run_id, {"edition": "hosted"})
    assert json.loads(
        project.db.execute(
            "SELECT edition_run_context FROM runs WHERE id=?", (run_id,)
        ).fetchone()["edition_run_context"]
    ) == {"edition": "open"}


# ---------------------------------------------------------------------------
# Consent records outlive the data.
# ---------------------------------------------------------------------------


def test_the_attempt_outlives_the_run_compaction_reclaims(project):
    """A paid action, undone, its branch discarded, then compacted.

    ``compact()`` is allowed to reclaim the *data* on a discarded branch —
    that is its whole job. It is not allowed to reclaim the record that the
    user consented and was charged: "did I approve it, what did it cost" has
    to stay answerable after the rows it produced are gone. The attempt row
    survives with ``run_id`` NULL (the ``receipts.run_id`` shape), still
    naming its consent, its cost basis and its pinned price card.
    """
    from frisket.engine.store.runs import RunResultStore

    sheet_id = project.add_sheet("S")
    op_id = project.append_op("transcribe")
    run_id = RunResultStore(project).start_run(
        op_id, sheet_id, "media.transcribe", params=dict(SPEC)
    )
    _route, promise_set, consent = _consent_and_route(project, run_id)
    attempt = _mint(project, run_id)
    _claim_unscoped(project, attempt)
    # The metering row the settlement join sums — the "what did it cost" side.
    project.db.execute(
        "INSERT INTO model_calls (id, fact_version, run_id, capability, engine, "
        "provider, provider_kind, units, attempt_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "call_1",
            "1",
            run_id,
            "transcribe",
            "faster_whisper",
            "models-gateway",
            "sidecar",
            json.dumps({"audio_seconds": "120"}),
            attempt.attempt_id,
        ),
    )
    project.db.commit()
    set_attempt_state(project, attempt.attempt_id, "effected")

    # Undo, then a new op so the undone branch is no longer redoable.
    project.undo()
    project.append_op("noop")
    assert (
        project.db.execute("SELECT status FROM ops WHERE id=?", (op_id,)).fetchone()[
            "status"
        ]
        == "discarded"
    )

    project.compact()

    # The run itself IS reclaimed — that is the behaviour under test, not a
    # bug: if the run row survived, this test would prove nothing.
    assert (
        project.db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
        is None
    )

    receipt = attempt_receipt(project, attempt.attempt_id)
    assert receipt is not None, "the consent record died with the data"
    assert receipt["run_id"] is None  # honest absence, not a dangling id
    assert receipt["state"] == "effected"
    assert receipt["consent"]["id"] == consent.id
    assert receipt["consent"]["promise_set_hash"] == promise_set.promise_set_hash
    assert receipt["cost_basis"] == {
        "kind": "priced",
        "pricing_key": "test.synthetic.audio_minute",
        "unit_rate": "0.01",
        "estimated_quantity": "100",
        "quantity_unit": "audio_minute",
        "terms_version": "test.synthetic.terms.v1",
        "quantity_rounding_mode": "half_even",
        "quantity_rounding_decimal_places": 6,
        "meter_key": "audio_seconds",
        "meter_units_per_quantity_unit": "60",
        "ceiling_mode": "consented_quantity",
        "row_settlement_mode": "all_metered",
        "charge_authority": "test.synthetic.authority",
    }
    assert receipt["price_card_version"] == "test.synthetic.terms.v1"
    assert receipt["scope"] == [7, 11]

    # What it was AUTHORIZED to cost survives (above). What it actually
    # metered does NOT: `model_calls.run_id` cascades, so the rows this join
    # sums went with the run. The receipt says that instead of summing an
    # empty set into a confident "$0" for a run that really was billed.
    assert (
        project.db.execute(
            "SELECT id FROM model_calls WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchall()
        == []
    )
    assert receipt["settlement"] == {
        "price_card_version": "test.synthetic.terms.v1",
        "terminal_status": None,
        "charge_usd": None,
        "unsettleable": "metering_reclaimed",
    }
