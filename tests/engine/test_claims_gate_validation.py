"""Route-resolution integration tests over validate_spec / MapRunner._prepare:
single resolution per invocation, the ClaimsGate, hash-echo verification,
and route/consent persistence at run creation.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from decimal import Decimal
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.runner import validation
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    default_pricing_policy,
)
from frisket.engine.runner.validation import (
    ClaimsGate,
    ExecutionResolutionRefused,
)
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore
from frisket.execution.resolve_for_action import action_identity_hash
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)


def _route_rows(project, run_id: int):
    """The run's route chain, oldest first. ``RouteStore.routes()`` was
    deleted in the cleanup (zero src callers — production reads the head),
    so chain-SHAPE assertions read the table."""
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


class _OneMicroPolicy:
    policy_id = "test.changed-claims.v1"

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        return RatedQuote(
            billed_cost=1,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


@pytest.fixture()
def audio_project(tmp_path: Path):
    project = Project.create(tmp_path / "p.frisket")
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
        yield project, sheet_id
    finally:
        project.close()


@pytest.fixture()
def gateway_env(monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")


def _spec(sheet_id: int, **overrides):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    confirmation = overrides.pop("consented_promise_set_hash", None)
    row_ids = overrides.pop("row_ids", None)
    scope = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    plan = _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "media.transcribe",
                "scope": scope,
                "params": {"source": "media", "engine": "faster_whisper", **overrides},
                "output_names": {"text": "transcript"},
                "idempotency_key": "claims-gate",
            }
        )
    )
    spec = plan.spec_dict()
    if confirmation is not None:
        spec["consented_promise_set_hash"] = confirmation
    return spec


def _model_spec(sheet_id: int, *, model: str):
    """An unrouted model program (map.extract) over the same sheet: the legacy
    CostGate path, now raised as a ClaimsGate with no display claims."""
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    return _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "map.extract",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["media"],
                    "model": model,
                    "instruction": "p",
                    "fields": [{"name": "x", "type": "text"}],
                },
                "idempotency_key": "claims-gate-model",
            }
        )
    ).spec_dict()


def _program(spec):
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        bound_typed_program_request_from_runner_spec,
    )

    bound = bound_typed_program_request_from_runner_spec(spec)
    assert bound is not None, spec["action_kind"]
    return _typed_map_rows_plan(bound).program


def _validate(
    project,
    spec,
    *,
    confirmed=False,
    persistence="durable",
    router=None,
    composition=None,
    pricing_policy=None,
    consent_coverage=None,
):
    router = router or ModelRouter()
    return validation.validate_spec(
        project,
        router,
        RunResultStore(project),
        spec,
        program=_program(spec),
        confirmed=confirmed,
        resume_run_id=None,
        persistence=persistence,
        pricing_policy=pricing_policy or default_pricing_policy(),
        composition=composition
        or open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        ),
        consent_coverage=consent_coverage,
    )


def _validate_gateway_claims(project, spec, **kwargs):
    """Exercise gateway claims above explicit zero test preapproval."""
    return _validate(
        project,
        spec,
        pricing_policy=_OneMicroPolicy(),
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Single resolution per invocation
# ---------------------------------------------------------------------------


def test_validate_spec_resolves_exactly_once(audio_project, monkeypatch):
    project, sheet_id = audio_project
    calls = []
    real = validation.resolve_for_action

    def counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(validation, "resolve_for_action", counting)
    validated = _validate(project, _spec(sheet_id))
    assert len(calls) == 1
    assert validated.resolved_execution is not None
    assert validated.resolved_execution.persistence == "durable"


def test_validate_spec_resolves_once_for_gated_engine(
    audio_project, gateway_env, monkeypatch
):
    project, sheet_id = audio_project
    calls = []
    real = validation.resolve_for_action

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(validation, "resolve_for_action", counting)
    with pytest.raises(ClaimsGate):
        _validate_gateway_claims(project, _spec(sheet_id, engine="moss"))
    assert len(calls) == 1


def test_resume_never_resolves(audio_project, monkeypatch):
    project, sheet_id = audio_project
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
    )
    prepared = runner._prepare(
        _spec(sheet_id),
        program=_program(_spec(sheet_id)),
        confirmed=False,
        resume_run_id=None,
    )
    calls = []
    monkeypatch.setattr(
        validation,
        "resolve_for_action",
        lambda *a, **k: calls.append(a) or None,
    )
    runner._prepare(
        _spec(sheet_id),
        program=_program(_spec(sheet_id)),
        confirmed=True,
        resume_run_id=prepared.run_id,
    )
    assert calls == []
    # ...and no second route/set was appended for the subject.
    store = RouteStore.for_run(project, prepared.run_id)
    assert len(_route_rows(project, prepared.run_id)) == 1
    assert len(store.promise_sets()) == 1


def test_non_resolution_recipes_pass_none_and_keep_cost_gate(audio_project):
    project, sheet_id = audio_project
    # An LLM spec (map program) with an unpriced model: legacy CostGate path,
    # now raised as ClaimsGate (a CostGate subclass) with no display claims,
    # but with a context hash that binds the 402 retry and receipt.
    spec = _model_spec(sheet_id, model="openai/some-unpriced-model")
    with pytest.raises(CostGate) as exc_info:
        _validate(project, spec)
    gate = exc_info.value
    assert isinstance(gate, ClaimsGate)
    assert gate.claims == []
    assert isinstance(gate.promise_set_hash, str)
    assert len(gate.promise_set_hash) == 64
    assert gate.estimate is None  # unpriced model: unknown, not zero


def test_priced_free_local_model_estimates_zero_and_never_gates(audio_project):
    """Any canonical local model carries known zero through admission."""
    project, sheet_id = audio_project
    spec = _model_spec(sheet_id, model="ollama/@test-local/frisket-new-unlisted-model")
    est = validation.estimate_run(project, spec, program=_program(spec))
    assert est["cost"] == 0.0
    assert est["cost"] is not None
    router = ModelRouter(
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="test-local",
                display_name="Test local",
                origin="http://127.0.0.1:11434",
                source="local_file",
            ),
        )
    )
    _validate(project, spec, router=router)  # must not raise CostGate


# ---------------------------------------------------------------------------
# ClaimsGate + hash echo
# ---------------------------------------------------------------------------


def test_local_free_run_never_gates_and_persists_o1(audio_project):
    project, sheet_id = audio_project
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
    )
    prepared = runner._prepare(
        _spec(sheet_id),
        program=_program(_spec(sheet_id)),
        confirmed=False,
        resume_run_id=None,
    )
    store = RouteStore.for_run(project, prepared.run_id)
    # O1: exactly one route row + one promise set, NO consent row.
    assert len(_route_rows(project, prepared.run_id)) == 1
    assert len(store.promise_sets()) == 1
    assert store.consents() == []
    head_route, head_set = store.head()
    assert head_route.egress_class == "none"
    assert head_set.consent_id is None


def test_gateway_run_gates_with_claims_and_hash(audio_project, gateway_env):
    project, sheet_id = audio_project
    with pytest.raises(ClaimsGate) as exc_info:
        _validate_gateway_claims(project, _spec(sheet_id, engine="moss"))
    gate = exc_info.value
    assert isinstance(gate, CostGate)  # 402-compat: handlers see a CostGate
    assert [c["field"] for c in gate.claims] == ["egress_class"]
    assert gate.claims[0]["display"]
    assert gate.promise_set_hash
    assert gate.estimate == 0.000001  # explicit one-micro claims-test tariff
    # User-facing copy: the modal's own control is a "type confirm" box, so the
    # message names the action, not the request field that carries it.
    assert "Confirm to run it anyway." in str(gate)
    assert "confirmed=True" not in str(gate)


def test_confirmed_with_matching_echo_passes_and_records_consent(
    audio_project, gateway_env
):
    project, sheet_id = audio_project
    with pytest.raises(ClaimsGate) as exc_info:
        _validate_gateway_claims(project, _spec(sheet_id, engine="moss"))
    set_hash = exc_info.value.promise_set_hash

    spec = _spec(sheet_id, engine="moss", consented_promise_set_hash=set_hash)
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
        pricing_policy=_OneMicroPolicy(),
    )
    prepared = runner._prepare(
        spec, program=_program(spec), confirmed=True, resume_run_id=None
    )
    store = RouteStore.for_run(project, prepared.run_id)
    consents = store.consents()
    assert len(consents) == 1
    consent = consents[0]
    assert consent.promise_set_hash == set_hash
    assert consent.action_identity_hash == action_identity_hash(spec)
    [promise_set] = store.promise_sets()
    assert promise_set.consent_id == consent.id
    assert promise_set.promise_set_hash == set_hash
    [route] = _route_rows(project, prepared.run_id)
    assert route["promise_set_id"] == promise_set.id
    assert route["egress_class"] == "operator_lan"


def test_hash_echo_mismatch_regates_even_when_confirmed(audio_project, gateway_env):
    project, sheet_id = audio_project
    spec = _spec(
        sheet_id,
        engine="moss",
        consented_promise_set_hash="not-the-hash-you-saw",
    )
    with pytest.raises(ClaimsGate) as exc_info:
        _validate_gateway_claims(project, spec, confirmed=True)
    # the re-gate carries FRESH claims + the current hash
    gate = exc_info.value
    assert gate.claims and gate.promise_set_hash != "not-the-hash-you-saw"


def test_confirmed_without_echo_regates_resolution_claims(audio_project, gateway_env):
    project, sheet_id = audio_project
    with pytest.raises(ClaimsGate) as first_gate:
        _validate_gateway_claims(project, _spec(sheet_id, engine="moss"))

    with pytest.raises(ClaimsGate) as omitted_echo:
        _validate_gateway_claims(
            project, _spec(sheet_id, engine="moss"), confirmed=True
        )

    assert omitted_echo.value.promise_set_hash == first_gate.value.promise_set_hash
    assert omitted_echo.value.claims == first_gate.value.claims


def test_resolution_backfill_confirmation_requires_current_hash(
    audio_project, gateway_env
):
    project, sheet_id = audio_project
    consent_coverage = ConsentCoverage(instance_principal(project), Decimal("0"))
    pricing_policy = _OneMicroPolicy()
    runner = MapRunner(
        project,
        ModelRouter(),
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
    )
    # The original run is machine-local and consent-free. Its backfill changes
    # to a gateway target, introducing an egress claim the original route never
    # covered.
    run_id = runner.prepare_run(
        _spec(sheet_id), program=_program(_spec(sheet_id))
    ).run_id

    column = project.columns(sheet_id)[0]
    digest = project.db.execute("SELECT hash FROM blobs LIMIT 1").fetchone()[0]
    [new_row_id] = project.add_rows(
        sheet_id,
        [
            {
                "media": media_cell(
                    digest,
                    mime="audio/wav",
                    filename="a.wav",
                )
            }
        ],
        {"media": column["id"]},
    )
    backfill_spec = _spec(sheet_id, engine="moss", row_ids=[new_row_id])

    with pytest.raises(ClaimsGate) as omitted_echo:
        validation.validate_spec(
            project,
            runner.router,
            runner.run_store,
            backfill_spec,
            program=_program(backfill_spec),
            confirmed=True,
            resume_run_id=run_id,
            pricing_policy=pricing_policy,
            composition=runner.execution_composition,
            consent_coverage=consent_coverage,
        )

    backfill_spec["consented_promise_set_hash"] = omitted_echo.value.promise_set_hash
    validated = validation.validate_spec(
        project,
        runner.router,
        runner.run_store,
        backfill_spec,
        program=_program(backfill_spec),
        confirmed=True,
        resume_run_id=run_id,
        pricing_policy=pricing_policy,
        composition=runner.execution_composition,
        consent_coverage=consent_coverage,
    )
    assert validated.resume_confirmation is not None


# ---------------------------------------------------------------------------
# Refusal mapping + estimate behavior
# ---------------------------------------------------------------------------


def test_dead_target_refusal_maps_to_validation_error(audio_project, monkeypatch):
    project, sheet_id = audio_project
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    with pytest.raises(ExecutionResolutionRefused) as exc_info:
        _validate(project, _spec(sheet_id, engine="moss"))
    assert isinstance(exc_info.value, ValueError)  # queued path maps to 400
    assert exc_info.value.family == "no_live_target"
    assert "FRISKET_MODELS_URL" in str(exc_info.value)


def test_missing_route_refuses_before_requesting_consent(audio_project, monkeypatch):
    project, sheet_id = audio_project
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # an unpriced provider model: estimate is unknown, not zero
    spec = _spec(sheet_id, engine="openai/not-a-priced-model")
    with pytest.raises(ExecutionResolutionRefused):
        _validate(project, spec)
    with pytest.raises(ExecutionResolutionRefused):
        _validate(project, spec, confirmed=True)


def test_estimate_run_attaches_claims_and_hash(audio_project, gateway_env):
    project, sheet_id = audio_project
    router = ModelRouter()
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    est = validation.estimate_run(
        project,
        _spec(sheet_id, engine="moss"),
        program=_program(_spec(sheet_id, engine="moss")),
        composition=composition,
        pricing_policy=_OneMicroPolicy(),
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )
    assert est["cost"] == 0.0
    assert est["rows"] == 1
    assert [c["field"] for c in est["claims"]] == ["egress_class"]
    assert est["promise_set_hash"]
    # local runs carry no claims keys at all
    local = validation.estimate_run(
        project,
        _spec(sheet_id),
        program=_program(_spec(sheet_id)),
        composition=composition,
        pricing_policy=default_pricing_policy(),
    )
    assert "claims" not in local and "promise_set_hash" not in local


def test_estimate_run_preserves_route_refusal(audio_project, monkeypatch):
    project, sheet_id = audio_project
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    router = ModelRouter()
    spec = _spec(sheet_id, engine="openai/not-a-priced-model")
    with pytest.raises(ExecutionResolutionRefused):
        validation.estimate_run(
            project,
            spec,
            program=_program(spec),
            composition=open_execution_composition(
                project, router, ExecutionCompositionContext.direct()
            ),
            pricing_policy=default_pricing_policy(),
        )


# ---------------------------------------------------------------------------
# Previews are ephemeral and never persist
# ---------------------------------------------------------------------------


def _route_table_counts(project):
    return tuple(
        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("routes", "promise_sets")
    )


def test_preview_persistence_is_ephemeral_and_writes_nothing(
    audio_project, gateway_env
):
    project, sheet_id = audio_project
    spec = _spec(sheet_id, engine="moss")
    with pytest.raises(ClaimsGate) as gate:
        _validate_gateway_claims(project, spec, persistence="ephemeral")
    spec["consented_promise_set_hash"] = gate.value.promise_set_hash
    validated = _validate_gateway_claims(
        project,
        spec,
        confirmed=True,
        persistence="ephemeral",
    )
    assert validated.resolved_execution.persistence == "ephemeral"
    assert _route_table_counts(project) == (0, 0)
    # the writer refuses the ephemeral object by type
    from frisket.execution.resolve_for_action import record_resolved_execution

    with pytest.raises(TypeError, match="ephemeral"):
        record_resolved_execution(
            project,
            99,
            validated.resolved_execution,
            spec=_spec(sheet_id, engine="moss"),
            consent_required=False,
        )
    assert _route_table_counts(project) == (0, 0)


def test_standalone_estimate_never_persists(audio_project, gateway_env):
    project, sheet_id = audio_project
    router = ModelRouter()
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    validation.estimate_run(
        project,
        _spec(sheet_id, engine="moss"),
        program=_program(_spec(sheet_id, engine="moss")),
        composition=composition,
        pricing_policy=default_pricing_policy(),
    )
    validation.estimate_run(
        project,
        _spec(sheet_id),
        program=_program(_spec(sheet_id)),
        composition=composition,
        pricing_policy=default_pricing_policy(),
    )
    assert _route_table_counts(project) == (0, 0)
    assert (
        project.db.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 1
    )  # only the standing cost consent minted at migration
