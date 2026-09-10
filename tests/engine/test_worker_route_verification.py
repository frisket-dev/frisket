"""Worker-side route availability and authorization at claim time.

Drives ``register_project_run_handler``'s registered handler directly with a
queued ``project.run`` payload (the exact worker entry point), with the ONLY
stub being ``run_transcription_engine`` — so "the adapter was never called"
is literal: the stub records every invocation.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.runs import (
    register_project_run_handler,
    verify_execution_route,
)
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.executor.queued_actions import (
    queued_v1_action_request,
    queued_v1_job_payload,
)
from frisket.engine.runner import MapRunner
from frisket.engine.runner.validation import ClaimsGate
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.output_claims import (
    ClaimLeaseRenewalFailed,
    OutputColumnClaimStore,
)
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import StaleAttemptWriter
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from frisket.ops.base import persisted_recipe_invocation_halt
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    _reset_pricing_policy_for_tests,
    install_pricing_policy,
)
from tests.execution_composition_helpers import open_attempt_authority


class _AboveLimitPricingPolicy:
    """Make normally free gateway work exceed the default preapproval limit."""

    policy_id = "test.worker-route-verification.above-limit.v1"

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        return RatedQuote(
            billed_cost=5_000_000,  # $5.00 in micro-dollars
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


@pytest.fixture(autouse=True)
def _price_the_gateway_above_the_limit() -> Iterator[None]:
    """Keep claim-time refusal tests on the explicit-consent path."""

    install_pricing_policy(_AboveLimitPricingPolicy())
    try:
        yield
    finally:
        _reset_pricing_policy_for_tests()


_PREPARED_QUEUES = {}


@pytest.fixture()
def workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    project = Project.create(root / "proj.frisket")
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="a.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="a.wav")}],
        {"media": col},
    )
    try:
        yield root, project, sheet_id
    finally:
        _PREPARED_QUEUES.clear()
        project.close()


@pytest.fixture()
def gateway_env(monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")


@pytest.fixture()
def adapter_stub(monkeypatch):
    """Stub provider execution, after the real worker route/source checks."""
    from frisket.sdk.ops import transcribe_engines
    from frisket.sdk.ops.transcribe_engines import TranscriptionEngineResult

    calls: list[dict] = []

    async def fake_execute(engine, path, spec, ctx, **kwargs):
        calls.append({"engine": engine})
        return TranscriptionEngineResult(
            output={"text": "stubbed transcript", "segments": [], "language": "en"},
            model_calls=(),
        )

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_execute)
    return calls


def _spec(sheet_id: int, **overrides):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    engine = overrides.pop("engine", "faster_whisper")
    output = overrides.pop("output_name", "transcript")
    confirmation = overrides.pop("consented_promise_set_hash", None)
    request = {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "media", "engine": engine, **overrides},
        "output_names": {"text": output, "segments": output + "_segments"},
        "replace_existing": False,
        "idempotency_key": "worker-route",
    }
    if confirmation is not None:
        request["confirmation"] = confirmation
    return _typed_map_rows_plan(typed_action_for_request(request)).spec_dict()


def _program(spec):
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        bound_typed_program_request_from_runner_spec,
    )

    return _typed_map_rows_plan(
        bound_typed_program_request_from_runner_spec(spec)
    ).program


def _prepare_local_run(project, spec, *, confirmed=False) -> int:
    """Reserve before preparing, matching the real typed queue order."""
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
    )

    bound = bound_typed_program_request_from_runner_spec(spec)
    action_body = bound.request.model_copy(
        update={
            "idempotency_key": "worker-route-" + uuid.uuid4().hex,
            "confirmation": spec.get("consented_promise_set_hash"),
        }
    ).model_dump(mode="json", exclude_none=True)
    request = queued_v1_action_request(action_body)
    assert request is not None
    reservation = request.entry.reserve_action(
        project,
        request.action,
        request.params,
        project_id="proj",
        router=ModelRouter(),
        program=request.program,
    )
    assert isinstance(reservation, dict), reservation
    spec.clear()
    spec.update(reservation["runner_spec"])
    progress = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=open_attempt_authority(project),
    ).prepare_run(spec, program=request.program, confirmed=confirmed)
    request.entry.mark_prepared(
        project,
        receipt_id=reservation["receipt_id"],
        run_id=progress.run_id,
        attempt_id=None,
        output_fields=reservation["output_fields"],
        program=request.program,
    )
    _PREPARED_QUEUES[(str(project.path), progress.run_id)] = (
        action_body,
        request,
        reservation,
    )
    return progress.run_id


def _prepare_consented_gateway_run(project, sheet_id) -> tuple[int, dict]:
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=open_attempt_authority(project),
    )
    with pytest.raises(ClaimsGate) as exc_info:
        runner.prepare_run(
            _spec(sheet_id, engine="moss"),
            program=_program(_spec(sheet_id, engine="moss")),
        )
    spec = _spec(
        sheet_id,
        engine="moss",
        consented_promise_set_hash=exc_info.value.promise_set_hash,
    )
    return _prepare_local_run(project, spec, confirmed=True), spec


def _prepare_consented_gateway_dispatch(project, sheet_id):
    """Prepare one fresh consented run for an explicitly claimed dispatch."""

    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=open_attempt_authority(project),
    )
    with pytest.raises(ClaimsGate) as exc_info:
        runner._prepare(
            _spec(sheet_id, engine="moss"),
            program=_program(_spec(sheet_id, engine="moss")),
            confirmed=False,
            resume_run_id=None,
        )
    spec = _spec(
        sheet_id,
        engine="moss",
        consented_promise_set_hash=exc_info.value.promise_set_hash,
    )
    prepared = runner._prepare(
        spec, program=_program(spec), confirmed=True, resume_run_id=None
    )
    return prepared, spec


def _claimed_output_authority(project, run_id: int) -> tuple[str, set[int]]:
    run = project.db.execute(
        "SELECT sheet_id, action_kind FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert run is not None
    columns = project.db.execute(
        "SELECT id, name FROM columns WHERE current_run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    assert columns
    queued = _PREPARED_QUEUES.get((str(project.path), run_id))
    if queued is not None:
        token = "output-claim:" + queued[2]["receipt_id"]
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=token,
            run_id=run_id,
            expected_output_names=[str(row["name"]) for row in columns],
        )
        return token, {int(row["id"]) for row in columns}
    token = f"output-claim:worker-route-verification:{run_id}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=int(run["sheet_id"]),
        output_names=[str(row["name"]) for row in columns],
        action_kind=str(run["action_kind"]),
        run_id=run_id,
        claim_token=token,
    )
    assert conflict is None
    assert len(claims) == len(columns)
    OutputColumnClaimStore(project).bind_to_run(
        claim_token=token,
        run_id=run_id,
        expected_output_names=[str(row["name"]) for row in columns],
    )
    return token, {int(row["id"]) for row in columns}


def _handle(root: Path, run_id: int, spec: dict) -> dict:
    project = Project(root / "proj.frisket")
    try:
        action_body, request, reservation = _PREPARED_QUEUES[
            (str(project.path), run_id)
        ]
        marked_spec = request.entry.mark_runner_spec(
            {**spec, **reservation["runner_spec"]},
            receipt_id=reservation["receipt_id"],
            action_id=reservation["action_id"],
            params_hash=reservation["params_hash"],
        )
        project.db.execute(
            "UPDATE runs SET action_kind=?, params=? WHERE id=?",
            ("media.transcribe", json.dumps(marked_spec, sort_keys=True), run_id),
        )
        project.db.commit()
        try:
            attempt = open_attempt_authority(project).mint(
                recipe=request.program,
                spec=marked_spec,
                run_id=run_id,
                scope=tuple(RunResultStore(project).run_row_scope(run_id)),
            )
            attempt_id = attempt.attempt_id
        except ExecutionRouteVerificationFailed:
            # These corruption probes model a complete tuple whose route or
            # consent becomes unreadable after publication. Mint leaves its
            # exact row at ``created`` when admission refuses; promote only
            # that test row so the worker re-runs the real admission check.
            row = project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=? "
                "ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            assert row is not None
            attempt_id = str(row["id"])
            project.db.execute(
                "UPDATE execution_attempts SET state='admitted' WHERE id=?",
                (attempt_id,),
            )
            project.db.commit()
        request.entry.mark_prepared(
            project,
            receipt_id=reservation["receipt_id"],
            run_id=run_id,
            attempt_id=attempt_id,
            output_fields=reservation["output_fields"],
            program=request.program,
        )
        payload = {
            "project_id": "proj",
            "run_id": run_id,
            "spec": marked_spec,
            "workspace_root": str(root),
            "v1_cache_mode": "replay",
            "v1_local_endpoint_bindings": [],
            **queued_v1_job_payload(
                request,
                action_body,
                reservation,
                run_id=run_id,
            ),
        }
    finally:
        project.close()
    registry = HandlerRegistry()
    register_project_run_handler(registry, workspace_root=root, router=ModelRouter())
    handler = registry.get("project.run")
    assert handler is not None
    return handler(payload, JobHandlerContext.without_job_row())


def _delete_route_artifacts(project, run_id: int) -> None:
    """Simulate the crash window where the run commits without route artifacts."""
    for table in ("routes", "promise_sets", "consents"):
        project.db.execute(
            f"DELETE FROM {table} WHERE subject_kind='run' AND subject_id=?",
            (str(run_id),),
        )
    project.db.commit()


def _run_row(root: Path, run_id: int):
    reopened = Project(root / "proj.frisket")
    try:
        return reopened.db.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    finally:
        reopened.close()


def _error_code(result: dict) -> str:
    return result["action_result"]["errors"][0]["code"]


# ---------------------------------------------------------------------------
# Coverage-verified happy path
# ---------------------------------------------------------------------------


def test_project_run_stale_writer_refusal_is_non_destructive(
    workspace,
    adapter_stub,
    monkeypatch,
) -> None:
    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)

    def lose_effect_authority(*_args, **_kwargs) -> None:
        raise StaleAttemptWriter("attempt A resumed after replacement B")

    monkeypatch.setattr(
        RunResultStore,
        "write_results",
        lose_effect_authority,
    )
    result = _handle(root, run_id, spec)

    assert result["status"] == "stale_attempt_writer"
    assert result["error"]["code"] == "stale_attempt_writer"
    assert adapter_stub
    reopened = Project(root / "proj.frisket")
    try:
        run = reopened.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "running"
        assert isinstance(run["current_attempt_id"], str)
        assert (
            reopened.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (run["current_attempt_id"],),
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE run_id=? AND status='active'",
                (run_id,),
            ).fetchone()[0]
            > 0
        )
        receipt = reopened.db.execute(
            "SELECT status, body FROM receipts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert receipt is not None
        assert receipt["status"] == "queued"
        assert json.loads(receipt["body"])["errors"] == []
    finally:
        reopened.close()


def test_project_run_renewal_failure_is_retryable_and_leaves_run_alive(
    workspace,
    adapter_stub,
    monkeypatch,
) -> None:
    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)

    def fail_renewal(*_args, **_kwargs) -> int:
        raise sqlite3.OperationalError("injected claim renewal outage")

    monkeypatch.setattr(OutputColumnClaimStore, "renew", fail_renewal)
    with pytest.raises(
        ClaimLeaseRenewalFailed,
        match="claim renewal outage",
    ):
        _handle(root, run_id, spec)

    assert adapter_stub
    reopened = Project(root / "proj.frisket")
    try:
        run = reopened.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "running"
        assert isinstance(run["current_attempt_id"], str)
        assert (
            reopened.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (run["current_attempt_id"],),
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE run_id=? AND status='active'",
                (run_id,),
            ).fetchone()[0]
            > 0
        )
        assert (
            reopened.db.execute(
                "SELECT status FROM receipts WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == "queued"
        )
    finally:
        reopened.close()


def test_consented_gateway_run_verifies_and_executes(
    workspace, gateway_env, adapter_stub
):
    root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    result = _handle(root, run_id, spec)
    assert result["status"] == "completed", result
    assert len(adapter_stub) == 1
    assert _run_row(root, run_id)["status"] == "completed"


def test_local_free_run_verifies_with_no_consent_row(workspace, adapter_stub):
    # O1: zero user claims -> coverage holds vacuously from artifacts alone.
    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)
    assert RouteStore.for_run(project, run_id).consents() == []
    result = _handle(root, run_id, spec)
    assert result["status"] == "completed", result
    assert len(adapter_stub) == 1


# ---------------------------------------------------------------------------
# consent_missing: fail closed, durable failure, nothing executed
# ---------------------------------------------------------------------------


def test_missing_route_artifacts_fail_closed_consent_missing(workspace, adapter_stub):
    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)
    _delete_route_artifacts(project, run_id)
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    assert _error_code(result) == "consent_missing"
    assert adapter_stub == []
    assert _run_row(root, run_id)["status"] == "failed"


def test_wrong_action_identity_hash_is_consent_missing(
    workspace, gateway_env, adapter_stub
):
    root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    project.db.execute(
        "UPDATE consents SET action_identity_hash='sha256:not-this-action' "
        "WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    )
    project.db.commit()
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    assert _error_code(result) == "consent_missing"
    assert adapter_stub == []


def test_uncovered_claim_without_consent_row_is_consent_missing(
    workspace, gateway_env, adapter_stub
):
    root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    project.db.execute(
        "UPDATE promise_sets SET consent_id=NULL "
        "WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    )
    project.db.execute(
        "DELETE FROM consents WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    )
    project.db.commit()
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    assert _error_code(result) == "consent_missing"
    assert adapter_stub == []


def test_non_resolution_recipes_are_never_verified(workspace):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

    _root, project, sheet_id = workspace
    project.add_column(sheet_id, "text")
    # The durable runner spec of a typed program that never consumes
    # resolution (an unrouted LLM extract), exactly as the queue stores it.
    plan = build_typed_map_rows_plan(
        project,
        typed_action_for_request(
            {
                "action_id": "map.extract",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["text"],
                    "model": "anthropic/claude-haiku-4-5",
                    "instruction": "Extract the named person.",
                    "fields": [{"name": "person", "type": "text"}],
                },
                "idempotency_key": "worker-route-non-resolution",
            }
        ),
    )
    assert plan.program.consumes_resolution is False
    # No run, no route artifacts: a non-resolution program returns immediately.
    verify_execution_route(
        project,
        424242,
        plan.spec_dict(),
        composition=open_execution_composition(
            project, None, ExecutionCompositionContext.direct()
        ),
    )


# ---------------------------------------------------------------------------
# no_live_target at claim
# ---------------------------------------------------------------------------


def test_dead_target_at_claim_is_durable_no_live_target(
    workspace, gateway_env, adapter_stub, monkeypatch
):
    root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    # The gateway dies between consent and claim.
    monkeypatch.delenv("FRISKET_MODELS_URL")
    monkeypatch.delenv("FRISKET_MODELS_TOKEN")
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    error = result["action_result"]["errors"][0]
    assert error["code"] == "no_live_target"
    assert "FRISKET_MODELS_URL" in error["message"]  # the remedy, not a shrug
    assert adapter_stub == []
    assert _run_row(root, run_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# F5: actor authorization — foreign consents (restored bundle) fail closed
# ---------------------------------------------------------------------------


def test_foreign_actor_consent_is_consent_missing(workspace, gateway_env, adapter_stub):
    """A consent minted by ANOTHER installation principal (the restored-
    bundle scene) never authorizes this installation's dispatch: exact-match
    coverage requires the consent's actor to equal the CURRENT principal."""
    root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    project.db.execute(
        "UPDATE consents SET actor='deployment:some-other-install' "
        "WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    )
    project.db.commit()
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    assert _error_code(result) == "consent_missing"
    assert adapter_stub == []


# ---------------------------------------------------------------------------
# Confirm-area 3: infrastructure failures normalize to durable failure
# ---------------------------------------------------------------------------


def test_route_store_error_normalizes_to_durable_consent_missing(
    workspace, adapter_stub, monkeypatch
):
    from frisket.engine.store.execution_routes import RouteStore, RouteStoreError

    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)

    def exploding_head(self, *, txn=None):
        raise RouteStoreError("chain write still lock-contended after 5 attempts")

    monkeypatch.setattr(RouteStore, "head", exploding_head)
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    assert _error_code(result) == "consent_missing"
    message = result["action_result"]["errors"][0]["message"]
    assert "could not" in message and "authorizes nothing" in message
    assert adapter_stub == []


def test_malformed_route_json_normalizes_to_durable_consent_missing(
    workspace, adapter_stub
):
    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)
    project.db.execute(
        "UPDATE promise_sets SET promises_json='{not json' "
        "WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    )
    project.db.commit()
    result = _handle(root, run_id, spec)
    assert result["status"] == "failed"
    assert _error_code(result) == "consent_missing"
    assert adapter_stub == []


# ---------------------------------------------------------------------------
# Coverage envelope: fold the sets THIS principal consented to for THIS
# action. Its reachable reader is the VALIDATION gate, not the worker, so what
# is pinned below is `persisted_claim_coverage` itself.
# ---------------------------------------------------------------------------


def _cost_claim(value: str) -> dict:
    return {
        "field": "cost",
        "op": "le",
        "value": value,
        "basis": {"pricing_key": "test.key"},
        "order_ref": None,
        "audience": "user_claim",
    }


def _append_set(
    project, run_id: int, *, spec: dict, promises: list[dict], consented: bool
):
    """Grow the run's chain the way production does — consent row first, then
    the promise set, then its route."""
    from frisket.engine.store.execution_routes import (
        instance_principal,
        promise_set_hash,
    )
    from frisket.execution.resolve_for_action import action_identity_hash

    store = RouteStore.for_run(project, run_id)
    head_route, head_set = store.head()
    consent_id = None
    if consented:
        consent = store.record_consent(
            action_identity_hash=action_identity_hash(spec),
            promise_set_hash=promise_set_hash(promises),
            actor=instance_principal(project),
        )
        consent_id = consent.id
    new_set = store.append_promise_set(
        promises=promises,
        predecessor_id=head_set.id,
        consent_id=consent_id,
    )
    store.append_route(
        promise_set_id=new_set.id,
        engine=head_route.engine,
        options=head_route.options,
        target_snapshot=head_route.target_snapshot,
        operator=head_route.operator,
        egress_class=head_route.egress_class,
        region=head_route.region,
        credential_source=head_route.credential_source,
        cost_posture=head_route.cost_posture,
        predecessor_id=head_route.id,
    )
    return new_set


def _recomputed_envelope(project, run_id: int, spec: dict) -> dict:
    """THE coverage read, spelled out independently of the production
    helper: fold every set consented by THIS principal for THIS action,
    walking the whole chain."""
    from frisket.engine.store.execution_routes import instance_principal
    from frisket.execution.promises import (
        Promise,
        PromiseSet,
        compute_coverage_envelope,
    )
    from frisket.execution.resolve_for_action import action_identity_hash

    store = RouteStore.for_run(project, run_id)
    identity = action_identity_hash(spec)
    principal = instance_principal(project)
    consented = {
        consent.promise_set_hash
        for consent in store.consents()
        if consent.action_identity_hash == identity and consent.actor == principal
    }
    return compute_coverage_envelope(
        [
            PromiseSet.make(tuple(Promise.from_row(row) for row in stored.promises))
            for stored in store.promise_sets()
            if stored.promise_set_hash in consented
        ]
    )


def test_coverage_envelope_is_the_high_water_of_the_consented_chain(workspace):
    """mint -> consented successor -> consented successor: the coverage
    helper's envelope is the fold over the sets THIS principal consented to
    for THIS action, recomputed from the chain on every read.

    The value used to be cached per append in
    ``promise_sets.coverage_envelope_json``. Its reachable caller walks the
    chain for the categorical claim, so what is pinned here is the fold, not
    its storage."""
    from frisket.engine.store.execution_routes import instance_principal
    from frisket.execution.resolve_for_action import (
        action_identity_hash,
        persisted_claim_coverage,
    )

    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)
    store = RouteStore.for_run(project, run_id)
    base = list(store.head()[1].promises)
    # The mint recorded no consent (a local free run has zero user claims),
    # so its root set contributes nothing to the CONSENTED envelope.
    assert _recomputed_envelope(project, run_id, spec) == {}

    _append_set(
        project,
        run_id,
        spec=spec,
        promises=base + [_cost_claim("2.50")],
        consented=True,
    )
    _append_set(
        project,
        run_id,
        spec=spec,
        promises=base + [_cost_claim("5.00")],
        consented=True,
    )

    recomputed = _recomputed_envelope(project, run_id, spec)
    assert recomputed  # the chain really did grow a quantitative high-water
    coverage = persisted_claim_coverage(
        store,
        store.consents(),
        expected_identity=action_identity_hash(spec),
        principal=instance_principal(project),
    )
    assert coverage.envelope == recomputed


# ---------------------------------------------------------------------------
# Bind/verify head coherence is STRUCTURAL. Dispatch used
# to deref its connection material from the head BEFORE the fence ran, so a
# reconsent landing in that window left dispatch pinned to H1 while
# verification approved H2's claims. The authority reads the head ONCE and
# the claim transaction compare-and-swaps on it, so the reconciliation
# argument (``bound_route_id``) and its ``stale_head`` re-read are retired —
# the CODE survives as the refusal the losing side of the race gets.
# ---------------------------------------------------------------------------


def test_head_that_moved_between_admission_and_the_claim_refuses(
    workspace, gateway_env, adapter_stub
):
    from frisket.execution.attempt import AttemptClaimRefused, claim

    _root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    head_route, _head_set = RouteStore.for_run(project, run_id).head()

    attempt = open_attempt_authority(project).mint(
        recipe=_program(spec),
        spec=spec,
        run_id=run_id,
        scope=(1,),
    )
    assert attempt.admission.head_route_id == head_route.id
    claim_token, output_column_ids = _claimed_output_authority(project, run_id)

    # A reconsent lands in the admission -> claim window: it appends a
    # SUCCESSOR route, so the head the attempt pinned is no longer current.
    store = RouteStore.for_run(project, run_id)
    successor_set = store.append_promise_set(
        promises=[dict(row) for row in _head_set.promises],
        predecessor_id=_head_set.id,
    )
    successor = store.append_route(
        promise_set_id=successor_set.id,
        engine=head_route.engine,
        options=dict(head_route.options),
        target_snapshot=dict(head_route.target_snapshot),
        operator=head_route.operator,
        egress_class=head_route.egress_class,
        region=head_route.region,
        credential_source=head_route.credential_source,
        cost_posture=head_route.cost_posture,
        predecessor_id=head_route.id,
    )
    assert successor.id != head_route.id

    with pytest.raises(AttemptClaimRefused) as exc_info:
        claim(
            project,
            attempt,
            claimless_direct_effect=False,
            claim_token=claim_token,
            output_column_ids=output_column_ids,
        )
    assert exc_info.value.code == "stale_head"
    assert head_route.id in str(exc_info.value)
    assert successor.id in str(exc_info.value)
    assert adapter_stub == []
    # Nothing dispatched, and the run never claimed the attempt.
    row = project.db.execute(
        "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row["current_attempt_id"] is None


def test_pending_row_edit_after_admission_refuses_before_egress(
    workspace, gateway_env, adapter_stub
):
    """The work-scope check must govern the values actually sent.

    Validation and attempt admission both observe the original media cell.
    The edit lands only after ``AttemptAuthority.mint`` has returned its
    admitted commitment, while the row is still pending and before the
    adapter is entered. Dispatch must not rebuild a different live value and
    send it under the old consent.
    """
    import asyncio

    _root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    claim_token, _output_column_ids = _claimed_output_authority(project, run_id)
    row_id = int(project.visible_row_ids(sheet_id)[0])
    source_column = next(
        column for column in project.columns(sheet_id) if column["name"] == "media"
    )
    replacement_blob = project.add_blob(
        b"RIFFreplacementWAVEfmt ",
        filename="replacement.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 9.0, "kind": "audio"}
        ),
    )
    replacement = media_cell(
        replacement_blob,
        mime="audio/wav",
        filename="replacement.wav",
    )

    class _EditAfterAdmissionAuthority:
        def __init__(self) -> None:
            self.delegate = open_attempt_authority(project)
            self.edited = False

        def mint(self, **kwargs):
            attempt = self.delegate.mint(**kwargs)
            project.apply_edits(
                [
                    {
                        "row_id": row_id,
                        "column_id": int(source_column["id"]),
                        "value": replacement,
                    }
                ],
                label="edit pending row after admission",
            )
            self.edited = True
            return attempt

    authority = _EditAfterAdmissionAuthority()
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=authority,
    )
    progress = asyncio.run(
        runner.run(
            spec,
            program=_program(spec),
            confirmed=True,
            resume_run_id=run_id,
            reopen_operator_cancelled=False,
            claim_token=claim_token,
        )
    )

    assert authority.edited is True
    assert adapter_stub == []
    assert progress.halted_code == "promise_violation"
    assert progress.completed == 0


def test_the_claim_transaction_must_own_its_write_lock(
    workspace, gateway_env, adapter_stub
):
    """Joining a caller's transaction would degrade the BEGIN IMMEDIATE write
    lock to a deferred read and turn the
    compare-and-swap back into a read-and-compare."""
    from frisket.execution.attempt import AttemptClaimRefused, claim

    _root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    attempt = open_attempt_authority(project).mint(
        recipe=_program(spec), spec=spec, run_id=run_id, scope=(1,)
    )
    claim_token, output_column_ids = _claimed_output_authority(project, run_id)
    project.db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(AttemptClaimRefused):
            claim(
                project,
                attempt,
                claimless_direct_effect=False,
                claim_token=claim_token,
                output_column_ids=output_column_ids,
            )
    finally:
        project.db.rollback()


def test_dispatch_pins_the_head_the_authority_verified(
    workspace, gateway_env, adapter_stub
):
    """The wiring, not just the check: the attempt the MapRunner claims names
    the SAME head the authority read — the two reads are one decision now."""
    root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_gateway_run(project, sheet_id)
    head_route, head_set = RouteStore.for_run(project, run_id).head()

    assert _handle(root, run_id, spec)["status"] == "completed"

    row = project.db.execute(
        "SELECT id, run_id, state, head_route_id, head_promise_set_id, scope_json "
        "FROM execution_attempts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert int(row["run_id"]) == run_id
    # The frozen scope is the rows dispatch actually walked — for a
    # run-scoped run that is the PENDING scope page, not the spec's empty
    # ``row_ids`` key.
    scope = json.loads(row["scope_json"])
    assert scope == [
        int(r["row_id"])
        for r in project.db.execute(
            "SELECT row_id FROM run_rows WHERE run_id=? ORDER BY position",
            (run_id,),
        )
    ]
    assert scope
    assert row["head_route_id"] == head_route.id
    assert row["head_promise_set_id"] == head_set.id
    assert row["state"] == "effected"
    # ``runs.current_attempt_id`` names the LIVE dispatch and is released on
    # the terminal transition (the claim-time pin is asserted in
    # tests/execution/test_execution_attempt.py). A pointer that outlives its
    # attempt is exactly the stale pointer the reconsent guard used to read.
    claimed = project.db.execute(
        "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert claimed["current_attempt_id"] is None


# ---------------------------------------------------------------------------
# The fence is INVOKED before the batch-recipe branch returns. It used to be
# presence-checked only, so a batch recipe that consumed resolution
# executed a whole run with zero verification — the exact shape the fence
# exists to kill, hiding inside the fence's own module.
# ---------------------------------------------------------------------------


def test_batch_recipe_invokes_the_run_start_fence_before_dispatching(
    workspace, monkeypatch
):
    import asyncio

    from frisket.ops.base import RecipeInvocationHalt
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram

    _root, project, sheet_id = workspace
    spec = _spec(sheet_id)

    # A synthetic BATCH recipe that still consumes resolution: same
    # transcription recipe, plus the execute_batch hook that makes
    # MapRunner.run take the batch branch and return from it.
    batched: list[int] = []

    async def execute_batch(self, values_by_row, spec, ctx):
        batched.append(len(values_by_row))
        from frisket.actions.types import RowResult
        from frisket.actions.media_types import TranscribedMedia

        return {
            row_id: self._prepare_publication(
                RowResult(
                    output=TranscribedMedia(
                        text="batched",
                        segments=[],
                        detected_language="en",
                    )
                )
            )
            for row_id in values_by_row
        }

    monkeypatch.setattr(
        _TypedMapRowsProgram, "execute_batch", execute_batch, raising=False
    )
    monkeypatch.setattr(_TypedMapRowsProgram, "is_llm", lambda self, spec: False)
    # Declared, not defaulted (E-3): the marker is a required declaration,
    # so this reads the class's own answer rather than a getattr default.
    assert _program(spec).consumes_resolution is True

    run_id = _prepare_local_run(project, spec)
    invoked: list[int] = []

    class _HaltingAuthority:
        """A double at the AUTHORITY seam, one level below the thing under
        test: the point is that the batch branch cannot reach
        ``execute_batch`` without a minted attempt, so the mint's own halt
        must terminalize the run."""

        def mint(self, *, recipe, spec, run_id, scope):
            invoked.append(run_id)
            raise RecipeInvocationHalt("promise_violation", "the fence fired")

    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=_HaltingAuthority(),
    )
    claim_token, _ = _claimed_output_authority(project, run_id)
    progress = asyncio.run(
        runner.run(
            dict(spec),
            program=_program(spec),
            confirmed=True,
            resume_run_id=run_id,
            claim_token=claim_token,
        )
    )

    assert invoked == [run_id], "the batch branch must go through the mint"
    assert batched == [], "zero rows dispatched"
    # The halt terminalizes resumably (cancelled + persisted markers), the
    # same shape the row-oriented path produces.
    assert progress.halted_code == "promise_violation"
    run = project.db.execute(
        "SELECT status, params FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run["status"] == "cancelled"
    halt = persisted_recipe_invocation_halt(run["params"])
    assert halt is not None and halt[0] == "promise_violation"


def test_batch_recipe_that_passes_the_fence_still_runs(workspace, monkeypatch):
    """Control: the fence's invocation must not break the batch path itself."""
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram

    root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    batched: list[int] = []

    async def execute_batch(self, values_by_row, spec, ctx):
        batched.append(len(values_by_row))
        from frisket.actions.types import RowResult
        from frisket.actions.media_types import TranscribedMedia

        return {
            row_id: self._prepare_publication(
                RowResult(
                    output=TranscribedMedia(
                        text="batched",
                        segments=[],
                        detected_language="en",
                    )
                )
            )
            for row_id in values_by_row
        }

    monkeypatch.setattr(
        _TypedMapRowsProgram, "execute_batch", execute_batch, raising=False
    )
    monkeypatch.setattr(_TypedMapRowsProgram, "is_llm", lambda self, spec: False)

    run_id = _prepare_local_run(project, spec)

    assert _handle(root, run_id, spec)["status"] == "completed"
    assert batched == [1]
    # The batch path is authorized exactly like the row path: one attempt,
    # claimed and closed.
    row = project.db.execute(
        "SELECT state FROM execution_attempts WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row is not None and row["state"] == "effected"


# ---------------------------------------------------------------------------
# Exact-match asymmetry: the validation gate ADMITS an identical re-run, so
# the re-run must carry its own consent artifact. Previously the gate
# admitted (installation-scoped exact match) while the worker fence read only
# the run's OWN subject-scoped chain, so run 2 was admitted at validation and
# then durably refused ``consent_missing`` at dispatch — forever.
# ---------------------------------------------------------------------------


def _consent_rows(project, run_id: int):
    return RouteStore.for_run(project, run_id).consents()


def test_identical_rerun_is_admitted_and_carries_a_derived_consent(
    workspace, gateway_env, adapter_stub
):
    from frisket.engine.store.execution_routes import (
        CONSENT_BASIS_CONFIRMED,
        CONSENT_BASIS_EXACT_MATCH,
    )

    root, project, sheet_id = workspace

    # Run 1: the user is gated, confirms, and the run dispatches.
    run1_id, confirmed_spec = _prepare_consented_gateway_run(project, sheet_id)
    [consent1] = _consent_rows(project, run1_id)
    assert consent1.grant_basis == CONSENT_BASIS_CONFIRMED
    assert _handle(root, run1_id, confirmed_spec)["status"] == "completed"
    assert len(adapter_stub) == 1

    # Run 2: the IDENTICAL action, launched with NO confirmation at all.
    # The exact-match socket covers it (same identity, same compiled set
    # hash, same actor), so validation admits without re-asking.
    plain_spec = _spec(sheet_id, engine="moss")
    plain_spec = _spec(sheet_id, engine="moss", output_name="transcript_2")
    run2_id = _prepare_local_run(project, plain_spec)
    assert run2_id != run1_id

    # ...and the admission left an ARTIFACT on run 2 — the whole point. The
    # worker fence is subject-scoped, so without this row run 2 would be
    # admitted here and refused there.
    [consent2] = _consent_rows(project, run2_id)
    assert consent2.grant_basis == CONSENT_BASIS_EXACT_MATCH
    assert consent2.promise_set_hash == consent1.promise_set_hash
    assert consent2.action_identity_hash == consent1.action_identity_hash
    assert consent2.actor == consent1.actor
    assert consent2.id != consent1.id

    # The REAL worker fence on run 2 (through the real queued handler):
    # it dispatches instead of durably refusing consent_missing.
    result = _handle(root, run2_id, plain_spec)
    assert result["status"] == "completed", result
    assert len(adapter_stub) == 2
    assert _run_row(root, run2_id)["status"] == "completed"

    # Run 3: derivation chains — run 2's own derived row is an equally
    # authoritative exact match for the next identical launch.
    plain_spec = _spec(sheet_id, engine="moss", output_name="transcript_3")
    run3_id = _prepare_local_run(project, plain_spec)
    [consent3] = _consent_rows(project, run3_id)
    assert consent3.grant_basis == CONSENT_BASIS_EXACT_MATCH
    assert _handle(root, run3_id, plain_spec)["status"] == "completed"
    assert len(adapter_stub) == 3


def test_a_different_action_still_re_asks(workspace, gateway_env, adapter_stub):
    """The socket is exact-match, not amnesty: a consented action never
    covers a DIFFERENT one (here, a different input column — same venue,
    same claims, different identity)."""
    _root, project, sheet_id = workspace
    _prepare_consented_gateway_run(project, sheet_id)
    source = project.columns(sheet_id)[0]
    other_column = project.add_column(sheet_id, "other_media", type="audio")
    values = project.get_values(sheet_id, source["id"])
    # Identical bytes, distinct authored input: cost is unchanged, intent is not.
    for row_id, value in values.items():
        project.db.execute(
            "INSERT INTO cells(row_id,column_id,value) VALUES(?,?,?)",
            (row_id, other_column, json.dumps(value)),
        )
    project.db.commit()
    other = _spec(
        sheet_id, engine="moss", source="other_media", output_name="transcript_2"
    )
    with pytest.raises(ClaimsGate):
        MapRunner(
            project,
            ModelRouter(),
            allow_action_lifecycle_only_recipes=True,
            authority=open_attempt_authority(project),
        ).prepare_run(other, program=_program(other))


# ---------------------------------------------------------------------------
# A FRESH launch refused pre-dispatch has no prior state to revert TO, so it
# is TERMINALIZED. Reverting nothing left it at status='running' with a
# NULL finished_at: never resumable, never reported, unreachable by
# run.reconsent. (The fresh routed launcher in production is the MCP
# ``run_recipe`` backend, which has no receipt — the run row IS the record.)
# ---------------------------------------------------------------------------


def _terminal_run(project, run_id: int):
    return project.db.execute(
        "SELECT status, finished_at, params FROM runs WHERE id=?", (run_id,)
    ).fetchone()


def _only_run_id(project) -> int:
    return int(project.db.execute("SELECT MAX(id) AS id FROM runs").fetchone()["id"])


def test_fresh_launch_refused_by_the_unverified_fence_terminalizes(
    workspace, gateway_env, adapter_stub
):
    import asyncio

    from frisket.execution.attempt_authority import (
        DependentChoiceRefusal,
        UnroutedOnlyAuthority,
    )

    _root, project, sheet_id = workspace
    # Consent first, so the fresh launch below is admitted by the exact-match
    # socket and reaches the dependent-choice refusal rather than the claims
    # gate.
    prepared, spec = _prepare_consented_gateway_dispatch(project, sheet_id)

    unwired = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
    )
    claim_token, _ = _claimed_output_authority(project, prepared.run_id)
    with pytest.raises(DependentChoiceRefusal) as exc_info:
        asyncio.run(
            unwired.run(
                dict(spec),
                program=_program(spec),
                confirmed=True,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )
    # Its own code: reconsent cannot repair missing wiring, so advertising
    # ``consent_missing`` would send the operator to a remedy that can never
    # work.
    assert exc_info.value.code == "execution_wiring_error"
    assert adapter_stub == []

    run = _terminal_run(project, _only_run_id(project))
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert json.loads(run["params"])["halted_code"] == "execution_wiring_error"


def test_fresh_launch_with_a_dead_target_terminalizes(
    workspace, gateway_env, adapter_stub, monkeypatch
):
    """The target dies in the resolve -> bind window (resolution admitted it;
    the dispatch deref no longer does)."""
    import asyncio

    from frisket.engine.jobs.runs import ExecutionRouteVerificationFailed
    from frisket.execution import resolver
    from frisket.execution.resolver import Refusal

    _root, project, sheet_id = workspace
    prepared, spec = _prepare_consented_gateway_dispatch(project, sheet_id)

    monkeypatch.setattr(
        resolver,
        "candidate_binding",
        lambda facts, provider: Refusal(
            family="no_live_target",
            remedy="set FRISKET_MODELS_URL to reach the gateway",
            engine=facts.engine,
            target_id=facts.target_id,
        ),
    )
    runner = MapRunner(
        project,
        ModelRouter(),
        authority=open_attempt_authority(project),
    )
    claim_token, _ = _claimed_output_authority(project, prepared.run_id)
    with pytest.raises(ExecutionRouteVerificationFailed) as exc_info:
        asyncio.run(
            runner.run(
                dict(spec),
                program=_program(spec),
                confirmed=True,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )
    assert exc_info.value.code == "no_live_target"
    assert adapter_stub == []

    run = _terminal_run(project, _only_run_id(project))
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    params = json.loads(run["params"])
    assert params["halted_code"] == "no_live_target"
    assert "FRISKET_MODELS_URL" in params["halted_reason"]  # the remedy rides


def test_fresh_launch_refused_consent_missing_terminalizes(
    workspace, gateway_env, adapter_stub
):
    import asyncio

    from frisket.engine.jobs.runs import ExecutionRouteVerificationFailed

    _root, project, sheet_id = workspace
    prepared, spec = _prepare_consented_gateway_dispatch(project, sheet_id)

    class _RefusingAuthority:
        def mint(self, *, recipe, spec, run_id, scope):
            raise ExecutionRouteVerificationFailed(
                "consent_missing", "no persisted consent covers this run's claims"
            )

    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=_RefusingAuthority(),
    )
    claim_token, _ = _claimed_output_authority(project, prepared.run_id)
    with pytest.raises(ExecutionRouteVerificationFailed):
        asyncio.run(
            runner.run(
                dict(spec),
                program=_program(spec),
                confirmed=True,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )
    assert adapter_stub == []

    run = _terminal_run(project, _only_run_id(project))
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert json.loads(run["params"])["halted_code"] == "consent_missing"


# ---------------------------------------------------------------------------
# The cost row on a remote-api route used to be structurally unevaluable:
# pricing is ENGINE-keyed, ``connection()`` is TARGET-keyed, so the fence
# compared "openai/whisper-1.audio_second" against None (basis_mismatch) on
# every run. The verification site HAS the engine (route.engine); it mints
# the same key + rate the estimate basis was compiled under.
# ---------------------------------------------------------------------------


@pytest.fixture()
def openai_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _remote_spec(sheet_id: int) -> dict:
    return _spec(sheet_id, engine="openai/whisper-1")


def _prepare_consented_remote_run(project, sheet_id: int) -> tuple[int, dict]:
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=open_attempt_authority(project),
    )
    spec = _remote_spec(sheet_id)
    with pytest.raises(ClaimsGate) as exc_info:
        runner.prepare_run(spec, program=_program(spec))
    confirmed = dict(spec, consented_promise_set_hash=exc_info.value.promise_set_hash)
    return _prepare_local_run(project, confirmed, confirmed=True), confirmed


def test_remote_api_cost_row_evaluates_at_the_live_engine_rate(workspace, openai_env):
    _root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_remote_run(project, sheet_id)
    store = RouteStore.for_run(project, run_id)
    _route, promise_set = store.head()
    cost_rows = [row for row in promise_set.promises if row["field"] == "cost"]
    assert cost_rows and cost_rows[0]["op"] == "le", cost_rows
    assert cost_rows[0]["basis"]["pricing_key"] == "openai/whisper-1.audio_second"

    # Satisfied at the consented rate, and the ledger is EMPTY. It used to
    # carry one permanently-unevaluable ``credential_source`` row on every
    # tariffed run — the provenance only appears at the model call, so the
    # run-start fence had nothing to score. The pre-effect USE constraint at
    # the adapter replaced that promise, so a satisfied run
    # now leaves no ledger noise at all.
    verify_execution_route(
        project,
        run_id,
        spec,
        composition=open_execution_composition(
            project, None, ExecutionCompositionContext.direct()
        ),
    )
    assert store.violations() == []
    assert not any(row["field"] == "credential_source" for row in promise_set.promises)


def test_remote_api_cost_row_refuses_when_the_live_rate_is_raised(
    workspace, openai_env, monkeypatch
):
    from frisket.ops.base import RecipeInvocationHalt

    _root, project, sheet_id = workspace
    run_id, spec = _prepare_consented_remote_run(project, sheet_id)
    store = RouteStore.for_run(project, run_id)
    _route, promise_set = store.head()

    # The provider raises its list price after consent. The bound was
    # consented under the old rate, so claim-time evaluation must record it
    # and refuse before dispatch under the stale confirmation.
    from frisket.ai.llm import pricing

    monkeypatch.setattr(pricing, "audio_price", lambda model: {"per_second": 0.01})
    with pytest.raises(RecipeInvocationHalt) as exc_info:
        verify_execution_route(
            project,
            run_id,
            spec,
            composition=open_execution_composition(
                project, None, ExecutionCompositionContext.direct()
            ),
        )
    assert exc_info.value.code == "promise_violation"
    assert "cost: bound_exceeded" in exc_info.value.detail
    recorded = {v.promise_fingerprint for v in store.violations()}
    assert _fingerprint_of(promise_set, "cost") in recorded
    assert [
        v
        for v in store.violations()
        if v.promise_fingerprint == _fingerprint_of(promise_set, "cost")
    ]


def test_route_verification_does_not_silently_skip_malformed_typed_program(workspace):
    _root, project, sheet_id = workspace
    spec = _spec(sheet_id)
    run_id = _prepare_local_run(project, spec)
    corrupted = {**spec, "params": {**spec["params"], "source": None}}

    with pytest.raises(ValueError):
        verify_execution_route(
            project,
            run_id,
            corrupted,
            composition=open_execution_composition(
                project, None, ExecutionCompositionContext.direct()
            ),
        )


def _fingerprint_of(promise_set, field: str) -> str:
    from frisket.engine.store.execution_routes import promise_fingerprint

    [row] = [r for r in promise_set.promises if r["field"] == field]
    return promise_fingerprint(row)
