"""Route-resolution unit tests: the resolve_for_action seam, promise-set identity,
standing-consent coverage, and the route writer's type refusals.

Project-backed integration behavior (validate_spec wiring, persistence on
prepare) lives in tests/engine/test_claims_gate_validation.py.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

import frisket.execution.provider as execution_provider
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
)
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.resolve_for_action import (
    BoundedScratchInput,
    action_identity_hash,
    consented_set_hash,
    gate_claims_payload,
    promise_rows,
    record_resolved_execution,
    resolve_for_action,
    uncovered_user_claims,
)
from frisket.execution.resolver import Refusal, ResolvedExecution
from frisket.execution.provider import (
    CompositionFacts,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.ops.base import Recipe
from tests.execution.typed_transcription_helpers import (
    transcription_spec,
    transcription_program,
)


def _route_rows(project, run_id: int):
    """The run's route chain, oldest first. ``RouteStore.routes()`` was
    deleted in the cleanup (zero src callers — production reads the head),
    so chain-SHAPE assertions read the table."""
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


@dataclass
class _PlainRecipe(Recipe):
    """Declared NOT to consume resolution: the seam returns None (capability
    dispatch — non-transcription actions proceed exactly as today).

    E-3: the declaration is what makes this a "no". A recipe that declares
    nothing raises at this consumer instead (see
    tests/execution/test_consumes_resolution_marker.py) — the six
    ``getattr(..., False)`` defaults that used to answer for it are gone.
    """

    name: str = "plain"
    consumes_resolution = False
    cost_class = "free"  # required declaration (Recipe)


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


def _spec(sheet_id: int, **overrides):
    flow = {
        key: overrides.pop(key)
        for key in ("confirmed", "consented_promise_set_hash")
        if key in overrides
    }
    engine = overrides.pop("engine", "faster_whisper")
    options = {"vad": True, **overrides} if engine != "moss" else overrides
    return {**transcription_spec(sheet_id, engine=engine, **options), **flow}


def _composition(project, facts: CompositionFacts | None = None):
    composition = open_execution_composition(
        project, ModelRouter(), ExecutionCompositionContext.direct()
    )
    if facts is None:
        return composition
    from frisket.execution.credential_use import CredentialUseContext
    from frisket.execution.price_book import cost_posture_for

    return replace(
        composition,
        facts=facts,
        credential_use_context=CredentialUseContext(
            cost_posture=cost_posture_for(facts.funding)
        ),
    )


def test_non_marked_recipe_returns_none(audio_project):
    project, sheet_id = audio_project
    assert (
        resolve_for_action(
            project,
            _spec(sheet_id),
            _PlainRecipe(),
            composition=_composition(project),
        )
        is None
    )


def test_local_engine_resolves_free_zero_claims(audio_project):
    project, sheet_id = audio_project
    resolved = resolve_for_action(
        project,
        _spec(sheet_id),
        transcription_program(_spec(sheet_id)),
        composition=_composition(project),
    )
    assert isinstance(resolved, ResolvedExecution)
    assert resolved.persistence == "durable"
    assert resolved.resolution.target.id == "local"
    assert resolved.resolution.facts.egress_class == "none"
    assert isinstance(resolved.cost_basis, OperatorBorneZeroCost)
    # O1: a local free run compiles ZERO user claims (no cost row at all).
    assert all(p.audience == "system_promise" for p in resolved.promise_set.promises)
    assert not any(p.field == "cost" for p in resolved.promise_set.promises)


def test_unknown_engine_refuses_no_capable_target(audio_project):
    project, sheet_id = audio_project
    with pytest.raises(ValueError, match="invalid_transcription_engine"):
        _spec(sheet_id, engine="totally-bogus")
    spec = _spec(sheet_id)
    # Typed authoring rejects this before placement; the downstream seam
    # also refuses a corrupted runner copy instead of selecting a default.
    outcome = resolve_for_action(
        project,
        {**spec, "engine": "totally-bogus"},
        transcription_program(spec),
        composition=_composition(project),
    )
    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_capable_target"


def test_dead_gateway_refuses_no_live_target_with_remedy(audio_project, monkeypatch):
    project, sheet_id = audio_project
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    outcome = resolve_for_action(
        project,
        _spec(sheet_id, engine="moss"),
        transcription_program(_spec(sheet_id, engine="moss")),
        composition=_composition(project),
    )
    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_live_target"
    assert "FRISKET_MODELS_URL" in outcome.remedy


def test_gateway_engine_compiles_egress_user_claim(audio_project, monkeypatch):
    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    resolved = resolve_for_action(
        project,
        _spec(sheet_id, engine="moss"),
        transcription_program(_spec(sheet_id, engine="moss")),
        composition=_composition(project),
    )
    assert isinstance(resolved, ResolvedExecution)
    assert resolved.resolution.target.id == "models-gateway"
    assert resolved.resolution.facts.egress_class == "operator_lan"
    assert isinstance(resolved.cost_basis, OperatorBorneZeroCost)
    claims = [p for p in resolved.promise_set.promises if p.audience == "user_claim"]
    assert [p.field for p in claims] == ["egress_class"]


def test_platform_funding_without_an_offer_has_no_target_or_quote(audio_project):
    """The funding marker cannot manufacture the old hosted SKU."""
    from frisket.execution.price_book import PlatformMetered

    project, sheet_id = audio_project
    outcome = resolve_for_action(
        project,
        _spec(sheet_id, engine="parakeet-tdt", diarize=True),
        transcription_program(_spec(sheet_id, engine="parakeet-tdt", diarize=True)),
        composition=_composition(
            project,
            CompositionFacts(edition="hosted", funding=PlatformMetered()),
        ),
    )
    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_capable_target"


def test_platform_inclusion_is_exact_free_and_not_presented_as_billing(
    audio_project, monkeypatch
):
    """One composition-included engine must not expose its target siblings."""
    from frisket.execution.price_book import PlatformMetered

    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    composition = replace(
        _composition(
            project,
            CompositionFacts(edition="hosted", funding=PlatformMetered()),
        ),
        included=(
            execution_provider.ExactExecutionMatch(
                target_id="models-gateway",
                capability="transcribe",
                engine="whisper-turbo",
            ),
        ),
    )

    included = resolve_for_action(
        project,
        _spec(sheet_id, engine="whisper-turbo"),
        transcription_program(_spec(sheet_id, engine="whisper-turbo")),
        composition=composition,
    )
    assert isinstance(included, ResolvedExecution)
    assert isinstance(included.cost_basis, OperatorBorneZeroCost)
    assert included.offering is None
    assert included.presentation is None
    assert not any(row.field == "cost" for row in included.promise_set.promises)

    sibling = resolve_for_action(
        project,
        _spec(sheet_id, engine="moss"),
        transcription_program(_spec(sheet_id, engine="moss")),
        composition=composition,
    )
    assert isinstance(sibling, Refusal)
    assert sibling.quote_absent is True


def test_composition_refuses_inclusion_not_supplied_by_provider(audio_project):
    from frisket.execution.price_book import PlatformMetered

    project, _sheet_id = audio_project
    with pytest.raises(ValueError, match="included.*supplied"):
        replace(
            _composition(
                project,
                CompositionFacts(edition="hosted", funding=PlatformMetered()),
            ),
            included=(
                execution_provider.ExactExecutionMatch(
                    target_id="models-gateway",
                    capability="transcribe",
                    engine="not-supplied",
                ),
            ),
        )


def test_an_injected_offer_prices_the_exact_selected_target(audio_project, monkeypatch):
    """The downstream value, not funding, creates the commercial world."""
    from frisket.execution.price_book import PlatformMetered

    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    composition = _composition(
        project,
        CompositionFacts(edition="hosted", funding=PlatformMetered()),
    )
    target = next(
        row for row in composition.provider.targets() if row.id == "models-gateway"
    )
    offering = CommercialOffering(
        match=CommercialOfferingMatch(
            target_id=target.id,
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.synthetic.audio_minute",
            terms_version="test.synthetic.terms.v1",
            unit_rate="0.017",
            quantity_unit="audio_minute",
            rounding=CommercialQuoteRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode="quoted_successful_rows",
        ),
        charge_authority="test.synthetic.authority",
        presentation=CommercialPresentation(
            venue_label="Synthetic shared venue",
            billing_label="Synthetic usage billing",
        ),
    )
    composition = replace(composition, offerings=(offering,))
    resolved = resolve_for_action(
        project,
        _spec(sheet_id, engine="parakeet-tdt", diarize=True),
        transcription_program(_spec(sheet_id, engine="parakeet-tdt", diarize=True)),
        composition=composition,
    )
    assert isinstance(resolved, ResolvedExecution)
    assert resolved.resolution.target.id == "models-gateway"
    assert resolved.resolution.facts.cost_posture == "platform_metered"
    assert resolved.resolution.facts.credential_source == "platform_key"

    basis = resolved.cost_basis
    assert isinstance(basis, PricedCostBasis)
    assert basis.pricing_key == "test.synthetic.audio_minute"
    assert basis.quantity_unit == "audio_minute"
    assert basis.unit_rate == "0.017"
    assert basis.terms_version == "test.synthetic.terms.v1"
    assert basis.meter_key == "audio_seconds"
    assert basis.row_settlement_mode == "quoted_successful_rows"
    assert basis.charge_authority == "test.synthetic.authority"
    assert basis.hardware_class is None and basis.throughput_ref is None
    assert basis.estimated_quantity == "0.033333"
    assert basis.bound == Decimal("0.017") * Decimal("0.033333")
    assert resolved.presentation == offering.presentation

    cost_rows = [p for p in resolved.promise_set.promises if p.field == "cost"]
    assert len(cost_rows) == 1 and cost_rows[0].op == "le"
    assert cost_rows[0].basis["pricing_key"] == offering.quote.pricing_key
    assert cost_rows[0].basis["charge_authority"] == offering.charge_authority


def test_bounded_scratch_offer_quotes_its_single_ephemeral_unit(
    audio_project, monkeypatch
):
    """Scratch media carries the row allocation required by retail settlement."""
    from frisket.execution.price_book import PlatformMetered

    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    composition = _composition(
        project,
        CompositionFacts(edition="hosted", funding=PlatformMetered()),
    )
    target = next(
        row for row in composition.provider.targets() if row.id == "models-gateway"
    )
    offering = CommercialOffering(
        match=CommercialOfferingMatch(
            target_id=target.id,
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.synthetic.audio_minute",
            terms_version="test.synthetic.terms.v1",
            unit_rate="0.017",
            quantity_unit="audio_minute",
            rounding=CommercialQuoteRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode="quoted_successful_rows",
        ),
        charge_authority="test.synthetic.authority",
        presentation=CommercialPresentation(
            venue_label="Synthetic shared venue",
            billing_label="Synthetic usage billing",
        ),
    )
    composition = replace(composition, offerings=(offering,))
    spec = _spec(sheet_id, engine="parakeet-tdt", diarize=True)

    resolved = resolve_for_action(
        project,
        spec,
        transcription_program(spec),
        composition=composition,
        bounded_scratch_input=BoundedScratchInput(
            quantity=15,
            work_scope={"identity": "scratch-audio"},
        ),
    )

    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, PricedCostBasis)
    assert resolved.cost_basis.estimated_quantity == "0.25"
    assert resolved.cost_basis.row_quote_quantities == ((1, "0.25"),)


# ---------------------------------------------------------------------------
# Coverage + identity
# ---------------------------------------------------------------------------


class _StandingConsent:
    """ConsentRow-shaped stub: F2 records the covered threshold on the row
    itself; F5 scopes coverage to the minting installation principal."""

    id = "consent_standing_stub"
    standing_policy = "cost_under_gate_v1"
    actor = "deployment:test-install"
    granted_at = "2026-07-24T00:00:00+00:00"
    policy_params = {"currency": "USD", "threshold_usd": "1"}


_PRINCIPAL = _StandingConsent.actor


def _resolved_gateway_offering(audio_project, monkeypatch, rate="0.000164"):
    from frisket.execution.price_book import PlatformMetered

    project, sheet_id = audio_project
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    composition = _composition(
        project,
        CompositionFacts(edition="hosted", funding=PlatformMetered()),
    )
    offering = CommercialOffering(
        match=CommercialOfferingMatch(
            target_id="models-gateway",
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.gateway.audio_second",
            terms_version="test.gateway.terms.v1",
            unit_rate=rate,
            quantity_unit="audio_second",
            rounding=CommercialQuoteRounding("half_even", 3),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="1",
            ceiling_mode="none",
            row_settlement_mode="all_metered",
        ),
        charge_authority="test.gateway.authority",
        presentation=CommercialPresentation(
            venue_label="Test gateway",
            billing_label="Test usage billing",
        ),
    )
    return project, resolve_for_action(
        project,
        _spec(sheet_id, engine="parakeet-tdt", diarize=True),
        transcription_program(_spec(sheet_id, engine="parakeet-tdt", diarize=True)),
        composition=replace(composition, offerings=(offering,)),
    )


def test_standing_consent_covers_sub_gate_cost_but_never_egress(
    audio_project, monkeypatch
):
    _, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    uncovered = uncovered_user_claims(
        resolved.promise_set,
        standing_consents=[_StandingConsent()],
        principal=_PRINCIPAL,
    )
    # The tiny commercial bound is covered by the standing consent; the
    # operator_lan egress claim is NOT (categorical claims
    # are covered only by exact-set consent).
    assert [p.field for p in uncovered] == ["egress_class"]


def test_without_standing_consent_cost_claim_is_uncovered(audio_project, monkeypatch):
    _, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    uncovered = uncovered_user_claims(
        resolved.promise_set, standing_consents=[], principal=_PRINCIPAL
    )
    assert sorted(p.field for p in uncovered) == ["cost", "egress_class"]


@pytest.mark.parametrize(
    ("provider_rate", "billed_cost", "covered"),
    [("2.5", 1_500_000, True), ("0.1", 3_000_000, False), ("0.1", None, False)],
)
def test_preapproval_uses_retail_quote_without_changing_provider_rates(
    audio_project, monkeypatch, provider_rate, billed_cost, covered
):
    from frisket.engine.runner.validation import _bind_rated_quote
    from frisket.execution.consent_coverage import ConsentCoverage
    from frisket.execution.resolve_for_action import project_uncovered_user_claims

    project, resolved = _resolved_gateway_offering(
        audio_project, monkeypatch, rate=provider_rate
    )
    original_cost = next(p for p in resolved.promise_set.promises if p.field == "cost")
    quoted = _bind_rated_quote(
        resolved,
        {
            "cost": float(resolved.cost_basis.bound),
            "billed_cost": billed_cost,
            "policy_id": "test.retail.v1",
        },
    )
    uncovered = project_uncovered_user_claims(
        project,
        quoted.promise_set,
        consent_coverage=ConsentCoverage(_PRINCIPAL, Decimal("2")),
    )

    assert (uncovered == []) is covered
    assert quoted.cost_basis == resolved.cost_basis
    assert (
        next(p for p in quoted.promise_set.promises if p.field == "cost")
        == original_cost
    )


def test_over_gate_cost_claim_is_uncovered_even_with_standing(
    audio_project, monkeypatch
):
    # absurd rate -> bound far above $1
    _, resolved = _resolved_gateway_offering(audio_project, monkeypatch, rate="1000")
    uncovered = uncovered_user_claims(
        resolved.promise_set,
        standing_consents=[_StandingConsent()],
        principal=_PRINCIPAL,
    )
    assert sorted(p.field for p in uncovered) == ["cost", "egress_class"]


def test_gate_claims_payload_shape(audio_project, monkeypatch):
    _, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    uncovered = uncovered_user_claims(
        resolved.promise_set, standing_consents=[], principal=_PRINCIPAL
    )
    payload = gate_claims_payload(
        uncovered,
        resolved.resolution.facts,
        has_cost_claim=any(
            promise.field == "cost" for promise in resolved.promise_set.promises
        ),
    )
    assert all(set(row) == {"field", "display"} for row in payload)
    assert all(row["display"] for row in payload)


def test_action_identity_hash_strips_retry_flow_flags(audio_project):
    _, sheet_id = audio_project
    base = _spec(sheet_id)
    confirmed = _spec(sheet_id, confirmed=True, consented_promise_set_hash="deadbeef")
    other = _spec(sheet_id, engine="moss")
    assert action_identity_hash(base) == action_identity_hash(confirmed)
    assert action_identity_hash(base) != action_identity_hash(other)
    digest = action_identity_hash(base)
    # F4: bare hex, consistent with every other content hash.
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


def test_consented_set_hash_matches_store_hash(audio_project):
    project, sheet_id = audio_project
    resolved = resolve_for_action(
        project,
        _spec(sheet_id),
        transcription_program(_spec(sheet_id)),
        composition=_composition(project),
    )
    from frisket.engine.store.execution_routes import (
        promise_set_hash as store_hash,
    )

    assert consented_set_hash(resolved.promise_set) == store_hash(
        promise_rows(resolved.promise_set)
    )


# ---------------------------------------------------------------------------
# The route writer's refusals + O1 write shape
# ---------------------------------------------------------------------------


def test_writer_type_refuses_ephemeral(audio_project):
    project, sheet_id = audio_project
    spec = _spec(sheet_id)
    ephemeral = resolve_for_action(
        project,
        spec,
        transcription_program(spec),
        composition=_composition(project),
        persistence="ephemeral",
    )
    assert ephemeral.persistence == "ephemeral"
    with pytest.raises(TypeError, match="ephemeral"):
        record_resolved_execution(
            project, 1, ephemeral, spec=spec, consent_required=False
        )
    # ...and nothing route-shaped touched the store.
    assert project.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM promise_sets").fetchone()[0] == 0


def test_writer_refuses_non_resolved_objects(audio_project):
    project, _ = audio_project
    with pytest.raises(TypeError, match="ResolvedExecution"):
        record_resolved_execution(project, 1, object(), spec={}, consent_required=False)


def test_target_snapshot_has_exact_dispatch_consumed_shape(audio_project):
    project, sheet_id = audio_project
    spec = _spec(sheet_id)
    resolved = resolve_for_action(
        project, spec, transcription_program(spec), composition=_composition(project)
    )
    promise_set_row, route_row = record_resolved_execution(
        project, 41, resolved, spec=spec, consent_required=False
    )
    from frisket.engine.store.execution_routes import RouteStore, route_fact_hash

    store = RouteStore.for_run(project, 41)
    assert [r["id"] for r in _route_rows(project, 41)] == [route_row.id]
    assert [p.id for p in store.promise_sets()] == [promise_set_row.id]
    assert store.consents() == []  # no consent event happened
    assert promise_set_row.consent_id is None
    head_route, head_set = store.head()
    assert head_route.promise_set_id == head_set.id
    assert head_route.engine == "faster_whisper"
    assert head_route.target_snapshot == {
        "target_id": "local",
        "capability": "transcribe",
        "transport": "local",
        "run_scoped": False,
    }
    assert set(head_route.target_snapshot) == {
        "target_id",
        "capability",
        "transport",
        "run_scoped",
    }
    assert all(
        isinstance(head_route.target_snapshot[key], str)
        and head_route.target_snapshot[key]
        for key in ("target_id", "capability", "transport")
    )
    assert type(head_route.target_snapshot["run_scoped"]) is bool
    assert head_route.route_fact_hash == route_fact_hash(
        operator=resolved.resolution.facts.operator,
        egress_class=resolved.resolution.facts.egress_class,
        region=resolved.resolution.facts.region,
        credential_source=resolved.resolution.facts.credential_source,
        cost_posture=resolved.resolution.facts.cost_posture,
    )
    assert head_set.promise_set_hash == consented_set_hash(resolved.promise_set)


@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "target_id": "local",
            "capability": "transcribe",
            "transport": "local",
        },
        {
            "target_id": "local",
            "capability": "transcribe",
            "transport": "local",
            "run_scoped": False,
            "resolver": "static-v1",
        },
        {
            "target_id": "",
            "capability": "transcribe",
            "transport": "local",
            "run_scoped": False,
        },
        {
            "target_id": "local",
            "capability": "unknown",
            "transport": "local",
            "run_scoped": False,
        },
        {
            "target_id": "local",
            "capability": "transcribe",
            "transport": "opencage.v1",
            "run_scoped": False,
        },
        {
            "target_id": "local",
            "capability": "transcribe",
            "transport": "local",
            "run_scoped": 0,
        },
    ],
)
def test_target_snapshot_refuses_old_unknown_or_malformed_values(snapshot):
    from frisket.execution.targets import validated_target_snapshot

    with pytest.raises(ValueError):
        validated_target_snapshot(snapshot)


def test_writer_is_atomic_on_partial_failure(audio_project, monkeypatch):
    """Consent + promise set + route land in ONE transaction — a
    failure appending the route rolls the consent and set back too."""
    project, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    from frisket.engine.store import execution_routes

    original = execution_routes.RouteStore.append_route

    def exploding(self, **kwargs):
        raise RuntimeError("boom mid-write")

    monkeypatch.setattr(execution_routes.RouteStore, "append_route", exploding)
    spec = _spec(1, engine="parakeet-tdt", diarize=True)
    with pytest.raises(RuntimeError, match="boom"):
        record_resolved_execution(
            project, 11, resolved, spec=spec, consent_required=True
        )
    monkeypatch.setattr(execution_routes.RouteStore, "append_route", original)
    counts = [
        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("routes", "promise_sets")
    ]
    assert counts == [0, 0]
    # only the standing cost consent survives; the per-action consent
    # rolled back with the failed transaction
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM consents WHERE standing_policy IS NULL"
        ).fetchone()[0]
        == 0
    )


def test_writer_consent_event_links_consent_set_route(audio_project, monkeypatch):
    project, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    spec = _spec(1, engine="parakeet-tdt", diarize=True)
    promise_set_row, route_row = record_resolved_execution(
        project, 7, resolved, spec=spec, consent_required=True
    )
    from frisket.engine.store.execution_routes import RouteStore

    store = RouteStore.for_run(project, 7)
    consents = store.consents()
    assert len(consents) == 1
    consent = consents[0]
    assert consent.promise_set_hash == promise_set_row.promise_set_hash
    assert consent.action_identity_hash == action_identity_hash(spec)
    assert consent.actor.startswith("deployment:")
    assert promise_set_row.consent_id == consent.id
    assert route_row.promise_set_id == promise_set_row.id
    assert route_row.egress_class == "operator_lan"
    assert route_row.cost_posture == "platform_metered"


# ---------------------------------------------------------------------------
# the route-bundle cutover socket restoration: the exact-match consent gate (triage "Socket
# restoration"). Before this, the large hardened standing-threshold machine
# had nothing to decide — every claim-bearing launch re-asked from scratch,
# because a persisted per-action consent was consulted ONLY at the worker.
# ---------------------------------------------------------------------------


def _record_consent_for(project, run_id, spec, promise_set):
    from frisket.engine.store.execution_routes import RouteStore, instance_principal
    from frisket.execution.resolve_for_action import (
        action_identity_hash,
        consented_set_hash,
    )

    return RouteStore.for_run(project, run_id).record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash=consented_set_hash(promise_set),
        actor=instance_principal(project),
    )


def test_exact_match_consent_covers_an_identical_rerun(audio_project, monkeypatch):
    """A consent recorded on run 7 covers the identical action launched as a
    NEW run: a re-run is a new run id, so subject-scoped lookup could never
    see it — the gate's question is installation-scoped by necessity."""
    from frisket.execution.resolve_for_action import (
        exact_match_consent_covers,
        project_uncovered_user_claims,
    )
    from frisket.engine.runner.validation import _bind_rated_quote
    from frisket.engine.store.execution_routes import instance_principal
    from frisket.execution.consent_coverage import ConsentCoverage

    project, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    _, sheet_id = audio_project
    spec = _spec(sheet_id, engine="parakeet-tdt", diarize=True)
    resolved = _bind_rated_quote(
        resolved,
        {
            "cost": float(resolved.cost_basis.bound),
            "billed_cost": 2_000_000,
            "policy_id": "test.retail.v1",
        },
    )
    coverage = ConsentCoverage(instance_principal(project), Decimal("0"))

    # The production gate hashes the retail-rated promise set. Keep this
    # exact-consent test above its explicit preapproval threshold so it does
    # not pass merely because the fresh run is preapproved.
    assert project_uncovered_user_claims(
        project, resolved.promise_set, spec=spec, consent_coverage=coverage
    )

    _record_consent_for(project, 7, spec, resolved.promise_set)
    assert exact_match_consent_covers(
        project, spec, resolved.promise_set, consent_coverage=coverage
    )
    assert (
        project_uncovered_user_claims(
            project, resolved.promise_set, spec=spec, consent_coverage=coverage
        )
        == []
    )


def test_exact_match_needs_all_three_components(audio_project, monkeypatch):
    """Identity, set hash, AND actor. Any one off and the consent does not
    cover — categorical consent is never implied across identities, and F5's
    restored-bundle scene fails closed."""
    from frisket.engine.store.execution_routes import RouteStore, instance_principal
    from frisket.execution.resolve_for_action import (
        action_identity_hash,
        consented_set_hash,
        exact_match_consent_covers,
    )

    project, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    _, sheet_id = audio_project
    spec = _spec(sheet_id, engine="parakeet-tdt", diarize=True)
    store = RouteStore.for_run(project, 7)
    principal = instance_principal(project)

    # Wrong action identity.
    store.record_consent(
        action_identity_hash="a" * 64,
        promise_set_hash=consented_set_hash(resolved.promise_set),
        actor=principal,
    )
    assert not exact_match_consent_covers(project, spec, resolved.promise_set)

    # Right identity, wrong set hash (the claims changed).
    store.record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash="b" * 64,
        actor=principal,
    )
    assert not exact_match_consent_covers(project, spec, resolved.promise_set)

    # Right identity + set hash, FOREIGN actor (restored bundle).
    store.record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash=consented_set_hash(resolved.promise_set),
        actor="deployment:someone-else",
    )
    assert not exact_match_consent_covers(project, spec, resolved.promise_set)

    # All three, this installation: covered.
    _record_consent_for(project, 7, spec, resolved.promise_set)
    assert exact_match_consent_covers(project, spec, resolved.promise_set)


def test_exact_match_preserves_explicit_engine_option_requirements(
    audio_project, monkeypatch
):
    """Explicit VAD is a target-support requirement; omitted VAD is not.

    Typed consent keeps that distinction even if an engine's default happens
    to produce the same output. Ordinary equivalent Params defaults are tested
    separately in test_action_identity.py.
    """
    from frisket.execution.resolve_for_action import exact_match_consent_covers

    project, resolved = _resolved_gateway_offering(audio_project, monkeypatch)
    _, sheet_id = audio_project
    with_vad = _spec(sheet_id, engine="parakeet-tdt", diarize=True)
    assert with_vad["vad"] is True
    # Omission must happen in the authored request, not its derived runner copy.
    without_vad = transcription_spec(sheet_id, engine="parakeet-tdt", diarize=True)
    _record_consent_for(project, 7, without_vad, resolved.promise_set)

    assert exact_match_consent_covers(project, without_vad, resolved.promise_set)
    assert not exact_match_consent_covers(project, with_vad, resolved.promise_set)


def test_standing_consents_never_cover_through_the_exact_match_read(audio_project):
    """The exact-match read is per-action only: a standing policy row (which
    carries a NULL action identity) can never be mistaken for one."""
    from frisket.engine.store.execution_routes import (
        ConsentRegistry,
        ensure_standing_cost_consent,
    )

    project, _sheet_id = audio_project
    ensure_standing_cost_consent(project)
    registry = ConsentRegistry(project)
    assert registry.standing_consents()
    assert registry.action_consents("a" * 64) == []
