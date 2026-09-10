"""Admission fences for request-scoped commercial offerings.

The historical regression covered by this module was caused by treating the
funding marker as the hosted product offering.  These tests keep the fence at
the corrected seam: only an exact offering injected by the current execution
composition can authorize a platform-priced route, while provider-direct
own-key estimates remain part of base.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import MODELS_GATEWAY_TARGET_ID, build_static_targets
from frisket.execution.price_book import (
    ByokZero,
    OperatorBorne,
    PlatformMetered,
    cost_posture_for,
    quote_transcription,
)
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
)
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExactExecutionMatch,
    ExecutionComposition,
)

from tests.execution.typed_transcription_helpers import (
    transcription_spec,
    transcription_program,
)

SPEC = transcription_spec(engine="parakeet-tdt")
SYNTHETIC_TARGET_ID = MODELS_GATEWAY_TARGET_ID


class _Provider:
    def __init__(self, target, *, connection_extra=None):
        self.target = target
        self.connection_extra = dict(connection_extra or {})
        self.connection_calls: list[str] = []

    def targets(self):
        return (self.target,)

    def connection(self, target_id: str):
        self.connection_calls.append(target_id)
        if target_id != self.target.id:
            return None
        return ConnectionConfig(extra=self.connection_extra)


@pytest.fixture
def project(tmp_path):
    value = Project.create(tmp_path / "offering-admission.frisket", name="test")
    yield value
    value.close()


def _target(target_id: str):
    return next(target for target in build_static_targets() if target.id == target_id)


def _offering(
    *,
    rate: str = "0.017",
    terms_version: str = "test.synthetic.terms.v1",
    meter_key: str = "audio_seconds",
    meter_scale: str = "60",
    charge_authority: str = "test.synthetic.authority",
) -> CommercialOffering:
    return CommercialOffering(
        match=CommercialOfferingMatch(
            target_id=SYNTHETIC_TARGET_ID,
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.synthetic.audio_minute",
            terms_version=terms_version,
            unit_rate=rate,
            quantity_unit="audio_minute",
            rounding=CommercialQuoteRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key=meter_key,
            meter_units_per_quantity_unit=meter_scale,
            ceiling_mode="consented_quantity",
            row_settlement_mode="all_metered",
        ),
        charge_authority=charge_authority,
        presentation=CommercialPresentation(
            venue_label="Synthetic venue",
            billing_label="Synthetic billing authority",
        ),
    )


def _composition(
    provider: _Provider,
    funding,
    *offerings: CommercialOffering,
) -> ExecutionComposition:
    posture = cost_posture_for(funding)
    return ExecutionComposition(
        facts=CompositionFacts(
            edition="open" if isinstance(funding, OperatorBorne) else "hosted",
            org_id=None if isinstance(funding, OperatorBorne) else "org-test",
            funding=funding,
        ),
        provider=provider,
        credential_use_context=CredentialUseContext(cost_posture=posture),
        offerings=offerings,
    )


def _promise_rows(target, engine: str, basis, funding) -> list[dict]:
    from frisket.execution.promise_compiler import compile_route_promises
    from frisket.execution.resolve_for_action import promise_rows
    from frisket.execution.resolver import RouteRowFacts

    credential_source = {
        "operator_borne": "local",
        "platform_metered": "platform_key",
        "org_key": "org_byok",
    }[cost_posture_for(funding)]
    facts = RouteRowFacts(
        target_id=target.id,
        engine=engine,
        operator=target.operator,
        egress_class=target.egress_class,
        region=target.region,
        credential_source=credential_source,
        cost_posture=cost_posture_for(funding),
    )
    return promise_rows(compile_route_promises(facts, basis))


def _persist(
    project,
    run_id: int,
    *,
    target,
    engine: str,
    transport: str,
    funding,
    basis,
):
    from frisket.engine.store.execution_routes import instance_principal
    from frisket.execution.resolve_for_action import (
        action_identity_hash,
        authored_options,
    )

    posture = cost_posture_for(funding)
    credential_source = {
        "operator_borne": "local",
        "platform_metered": "platform_key",
        "org_key": "org_byok",
    }[posture]
    store = RouteStore.for_run(project, run_id)
    spec = transcription_spec(engine=engine)
    promise_set = store.append_promise_set(
        promises=_promise_rows(target, engine, basis, funding),
        predecessor_id=None,
    )
    store.record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash=promise_set.promise_set_hash,
        actor=instance_principal(project),
    )
    return store.append_route(
        promise_set_id=promise_set.id,
        engine=engine,
        options=authored_options(spec),
        target_snapshot={
            "target_id": target.id,
            "capability": "transcribe",
            "transport": transport,
            "run_scoped": False,
        },
        operator=target.operator,
        egress_class=target.egress_class,
        region=target.region,
        credential_source=credential_source,
        cost_posture=posture,
        predecessor_id=None,
    )


def _cost_result(admission):
    rows = [
        result
        for promise, result in admission.evaluation.results
        if promise.field == "cost"
    ]
    assert len(rows) == 1
    return rows[0]


def test_platform_funding_without_an_offer_mints_no_price_or_target() -> None:
    provider = _Provider(_target(SYNTHETIC_TARGET_ID))
    composition = _composition(provider, PlatformMetered())

    basis = quote_transcription(
        target_id=SYNTHETIC_TARGET_ID,
        engine="parakeet-tdt",
        funding=composition.facts.funding,
        offering=None,
        audio_seconds=600,
        hardware_class="T4",
    )

    assert basis == OperatorBorneZeroCost()
    assert composition.offerings == ()
    assert composition.resolution_targets() == ()
    assert (
        composition.offering_for(
            target_id=SYNTHETIC_TARGET_ID,
            capability="transcribe",
            engine="parakeet-tdt",
        )
        is None
    )


def test_composition_filtered_target_has_no_estimate_quote(project) -> None:
    from frisket.engine.runner.validation import (
        ExecutionResolutionRefused,
        estimate_run,
    )
    from frisket.execution.pricing_policy import default_pricing_policy

    sheet = project.add_sheet("audio")
    column = project.add_column(sheet, "audio", type="audio")
    project.add_rows(sheet, [{"audio": "/missing.wav"}], {"audio": column})
    spec = transcription_spec(sheet, source="audio", engine="parakeet-tdt")
    provider = _Provider(_target("local-onnx"))
    composition = _composition(provider, PlatformMetered())

    with pytest.raises(ExecutionResolutionRefused) as exc_info:
        estimate_run(
            project,
            spec,
            program=transcription_program(spec),
            composition=composition,
            pricing_policy=default_pricing_policy(),
        )

    assert exc_info.value.family == "no_capable_target"
    assert exc_info.value.refusal.quote_absent is True
    assert provider.connection_calls == []


def test_empty_platform_provider_has_no_fallback_estimate_quote(project) -> None:
    from frisket.engine.runner.validation import (
        ExecutionResolutionRefused,
        estimate_run,
    )
    from frisket.execution.pricing_policy import default_pricing_policy

    class _EmptyProvider:
        def targets(self):
            return ()

        def connection(self, target_id: str):  # pragma: no cover - no target
            raise AssertionError(f"must not probe absent target {target_id!r}")

    sheet = project.add_sheet("empty-provider-audio")
    column = project.add_column(sheet, "audio", type="audio")
    project.add_rows(sheet, [{"audio": "/missing.wav"}], {"audio": column})
    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-empty-provider",
            funding=PlatformMetered(),
        ),
        provider=_EmptyProvider(),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
    )

    spec = transcription_spec(sheet, source="audio", engine="parakeet-tdt")
    with pytest.raises(ExecutionResolutionRefused) as exc_info:
        estimate_run(
            project,
            spec,
            program=transcription_program(spec),
            composition=composition,
            pricing_policy=default_pricing_policy(),
        )

    assert exc_info.value.family == "no_capable_target"
    assert exc_info.value.refusal.quote_absent is True


def test_exact_injected_offering_admits_without_connection_rate_labels(project):
    from frisket.execution.attempt_authority import admit_routed

    offering = _offering()
    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    composition = _composition(provider, PlatformMetered(), offering)
    basis = quote_transcription(
        target_id=target.id,
        engine="parakeet-tdt",
        funding=composition.facts.funding,
        offering=offering,
        audio_seconds=600,
        hardware_class="T4",
    )
    assert isinstance(basis, PricedCostBasis)
    _persist(
        project,
        7,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=composition.facts.funding,
        basis=basis,
    )

    admission = admit_routed(project, 7, SPEC, composition=composition)

    assert _cost_result(admission).status == "satisfied"
    assert admission.binding.connection.extra == {
        "protocol": "frisket.transcription.v1"
    }
    assert provider.connection_calls == [target.id]


def test_exact_included_execution_admits_without_an_offer(project):
    from frisket.execution.attempt_authority import admit_routed

    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    composition = replace(
        _composition(provider, PlatformMetered()),
        included=(
            ExactExecutionMatch(
                target_id=target.id,
                capability="transcribe",
                engine="parakeet-tdt",
            ),
        ),
    )
    basis = quote_transcription(
        target_id=target.id,
        engine="parakeet-tdt",
        funding=composition.facts.funding,
        offering=None,
        audio_seconds=600,
        hardware_class="T4",
    )
    assert basis == OperatorBorneZeroCost()
    _persist(
        project,
        17,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=composition.facts.funding,
        basis=basis,
    )

    admission = admit_routed(project, 17, SPEC, composition=composition)

    assert admission.binding.connection.extra == {
        "protocol": "frisket.transcription.v1"
    }
    assert provider.connection_calls == [target.id]


def test_matched_offer_with_unknown_quantity_pins_terms_and_can_admit(project):
    from frisket.execution.attempt_authority import admit_routed

    offering = _offering()
    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    composition = _composition(provider, PlatformMetered(), offering)
    basis = quote_transcription(
        target_id=target.id,
        engine="parakeet-tdt",
        funding=composition.facts.funding,
        offering=offering,
        audio_seconds=None,
        hardware_class="T4",
    )

    assert isinstance(basis, PricedCostBasis)
    assert basis.estimated_quantity is None
    _persist(
        project,
        13,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=composition.facts.funding,
        basis=basis,
    )

    admission = admit_routed(project, 13, SPEC, composition=composition)
    assert _cost_result(admission).status == "satisfied"


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(_offering(rate="0.018"), id="rate"),
        pytest.param(_offering(meter_scale="30"), id="meter-scale"),
        pytest.param(
            _offering(charge_authority="test.replacement.authority"),
            id="charge-authority",
        ),
    ],
)
def test_admission_refuses_when_current_offer_no_longer_matches_pin(project, current):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.ops.base import RecipeInvocationHalt

    pinned = _offering()
    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    basis = quote_transcription(
        target_id=target.id,
        engine="parakeet-tdt",
        funding=PlatformMetered(),
        offering=pinned,
        audio_seconds=600,
        hardware_class="T4",
    )
    _persist(
        project,
        8,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=PlatformMetered(),
        basis=basis,
    )

    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project,
            8,
            SPEC,
            composition=_composition(provider, PlatformMetered(), current),
        )

    assert exc_info.value.code == "promise_violation"
    assert "current offer no longer exactly matches" in exc_info.value.detail
    assert provider.connection_calls == [target.id]
    assert len(RouteStore.for_run(project, 8).violations()) == 1


def test_admission_refuses_when_pinned_offer_is_missing(project):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.ops.base import RecipeInvocationHalt

    pinned = _offering()
    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    basis = quote_transcription(
        target_id=target.id,
        engine="parakeet-tdt",
        funding=PlatformMetered(),
        offering=pinned,
        audio_seconds=600,
        hardware_class="T4",
    )
    _persist(
        project,
        9,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=PlatformMetered(),
        basis=basis,
    )

    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project,
            9,
            SPEC,
            composition=_composition(provider, PlatformMetered()),
        )

    assert exc_info.value.code == "promise_violation"
    assert "no longer supplies the pinned offer" in exc_info.value.detail
    assert provider.connection_calls == [target.id]


@pytest.mark.parametrize(
    "old_basis",
    [
        pytest.param(OperatorBorneZeroCost(), id="zero"),
        pytest.param(UnpriceableCost(), id="unpriceable"),
    ],
)
def test_platform_funding_cannot_admit_an_old_nonpriced_pin_without_offer(
    project, old_basis
):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.ops.base import RecipeInvocationHalt

    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    _persist(
        project,
        12,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=PlatformMetered(),
        basis=old_basis,
    )

    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project,
            12,
            SPEC,
            composition=_composition(provider, PlatformMetered()),
        )

    assert exc_info.value.code == "promise_violation"
    assert "supplies no exact offer" in exc_info.value.detail


def test_current_composition_absence_refuses_an_old_operator_route(project):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.ops.base import RecipeInvocationHalt

    target = _target("local-onnx")
    provider = _Provider(target)
    _persist(
        project,
        14,
        target=target,
        engine="parakeet-tdt",
        transport="local",
        funding=OperatorBorne(),
        basis=OperatorBorneZeroCost(),
    )

    current = _composition(provider, PlatformMetered())
    assert current.resolution_targets() == ()
    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(project, 14, SPEC, composition=current)

    assert exc_info.value.code == "promise_violation"
    assert "no longer supplies the pinned target" in exc_info.value.detail


def test_sibling_offer_does_not_admit_an_unoffered_own_key_engine(project):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.ops.base import RecipeInvocationHalt

    target = _target("remote-api:openai")
    provider = _Provider(target)
    pinned_engine = "openai/whisper-1"
    pinned_funding = ByokZero()
    basis = quote_transcription(
        target_id=target.id,
        engine=pinned_engine,
        funding=pinned_funding,
        offering=None,
        audio_seconds=60,
        hardware_class=None,
    )
    _persist(
        project,
        15,
        target=target,
        engine=pinned_engine,
        transport="remote",
        funding=pinned_funding,
        basis=basis,
    )
    sibling = replace(
        _offering(),
        match=CommercialOfferingMatch(
            target_id=target.id,
            capability="transcribe",
            engine="openai/future-whisper",
        ),
    )
    current = _composition(provider, PlatformMetered(), sibling)
    assert not current.supplies_resolution_match(
        target_id=target.id,
        capability="transcribe",
        engine=pinned_engine,
    )

    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project, 15, transcription_spec(engine=pinned_engine), composition=current
        )

    assert exc_info.value.code == "promise_violation"
    assert "no longer supplies the pinned target" in exc_info.value.detail


def test_new_offer_cannot_retroactively_price_an_unpriced_pin(project):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.ops.base import RecipeInvocationHalt

    target = _target(SYNTHETIC_TARGET_ID)
    provider = _Provider(
        target, connection_extra={"protocol": "frisket.transcription.v1"}
    )
    _persist(
        project,
        10,
        target=target,
        engine="parakeet-tdt",
        transport="frisket.transcription.v1",
        funding=PlatformMetered(),
        basis=OperatorBorneZeroCost(),
    )

    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project,
            10,
            SPEC,
            composition=_composition(provider, PlatformMetered(), _offering()),
        )

    assert exc_info.value.code == "promise_violation"
    assert "does not carry that offer" in exc_info.value.detail


@pytest.mark.parametrize(
    "funding",
    [
        pytest.param(OperatorBorne(), id="operator-own-key"),
        pytest.param(ByokZero(), id="org-byok"),
    ],
)
def test_provider_direct_own_key_estimate_still_admits(project, funding):
    from frisket.execution.attempt_authority import admit_routed

    target = _target("remote-api:openai")
    provider = _Provider(target)
    composition = _composition(provider, funding)
    basis = quote_transcription(
        target_id=target.id,
        engine="openai/whisper-1",
        funding=funding,
        offering=None,
        audio_seconds=90,
        hardware_class=None,
    )
    assert isinstance(basis, PricedCostBasis)
    assert basis.pricing_key == "openai/whisper-1.audio_second"
    assert basis.terms_version is None
    assert basis.charge_authority == "provider_direct"

    _persist(
        project,
        11,
        target=target,
        engine="openai/whisper-1",
        transport="remote",
        funding=funding,
        basis=basis,
    )
    admission = admit_routed(
        project,
        11,
        transcription_spec(engine="openai/whisper-1"),
        composition=composition,
    )

    assert _cost_result(admission).status == "satisfied"
    assert provider.connection_calls == [target.id]
