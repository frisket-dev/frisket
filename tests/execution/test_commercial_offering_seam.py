"""Wire cuts for the request-scoped commercial-offering carriage."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from frisket.contracts.actions.schemas._engines import (
    CENSUS_ENGINE_TABLE,
    GEOCODE_ENGINE_TABLE,
)
from frisket.execution.attempt import cost_basis_from_promises
from frisket.execution.commercial import (
    LIVE_COST_TERM_KEYS,
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import MODELS_GATEWAY_TARGET_ID, build_static_targets
from frisket.execution.price_book import PlatformMetered, settle
from frisket.execution.promise_compiler import PricedCostBasis, UnpriceableCost
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExactExecutionMatch,
    ExecutionComposition,
)


class _Provider:
    def __init__(self, rows=None):
        self._rows = tuple(rows or build_static_targets())

    def targets(self):
        return self._rows

    def connection(self, target_id: str):
        return (
            ConnectionConfig()
            if any(row.id == target_id for row in self._rows)
            else None
        )


def _offer(
    *,
    target_id: str = MODELS_GATEWAY_TARGET_ID,
    engine: str = "parakeet-tdt",
    rate: str = "0.017",
    terms_version: str = "test.offer.terms.v1",
    meter_key: str = "audio_seconds",
    authority: str = "test.charge-authority",
) -> CommercialOffering:
    return CommercialOffering(
        match=CommercialOfferingMatch(target_id, "transcribe", engine),
        quote=CommercialQuoteTerms(
            pricing_key="test.offer.audio_minute",
            terms_version=terms_version,
            unit_rate=rate,
            quantity_unit="audio_minute",
            rounding=CommercialQuoteRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key=meter_key,
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode="quoted_successful_rows",
        ),
        charge_authority=authority,
        presentation=CommercialPresentation(
            venue_label="Synthetic shared venue",
            billing_label="Synthetic billing authority",
        ),
    )


def _platform_composition(*offers: CommercialOffering) -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-test",
            funding=PlatformMetered(),
        ),
        provider=_Provider(),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=tuple(offers),
    )


def test_platform_funding_keeps_only_noncommercial_free_public_targets() -> None:
    composition = _platform_composition()
    assert composition.offerings == ()
    supports = {
        (target.id, support.capability, support.engine)
        for target in composition.resolution_targets()
        for support in target.engines
    }
    assert supports == {
        ("nominatim", "geocode", "nominatim"),
        ("us-census", "census_demographics", "us_census_acs"),
    }
    assert composition.supplies_resolution_match(
        target_id="nominatim", capability="geocode", engine="nominatim"
    )
    assert composition.supplies_resolution_match(
        target_id="us-census",
        capability="census_demographics",
        engine="us_census_acs",
    )
    assert not composition.supplies_resolution_match(
        target_id="local-onnx",
        capability="transcribe",
        engine="parakeet-tdt",
    )
    assert composition.provider_for_resolution().targets()


def test_exact_offer_adds_only_its_commercial_target_support() -> None:
    offer = _offer()
    composition = _platform_composition(offer)
    targets = composition.resolution_targets()
    assert [
        (row.id, support.capability, support.engine)
        for row in targets
        for support in row.engines
    ] == [
        (MODELS_GATEWAY_TARGET_ID, "transcribe", "parakeet-tdt"),
        ("nominatim", "geocode", "nominatim"),
        ("us-census", "census_demographics", "us_census_acs"),
    ]
    assert (
        composition.offering_for(
            target_id=offer.match.target_id,
            capability=offer.match.capability,
            engine=offer.match.engine,
        )
        is offer
    )
    assert (
        composition.offering_for(
            target_id="local-onnx",
            capability="transcribe",
            engine="parakeet-tdt",
        )
        is None
    )


def test_open_local_composition_cannot_carry_an_offer() -> None:
    with pytest.raises(ValueError, match="cannot supply commercial offerings"):
        ExecutionComposition(
            facts=CompositionFacts(),
            provider=_Provider(),
            credential_use_context=CredentialUseContext.open(),
            offerings=(_offer(),),
        )


def test_every_offer_must_match_a_target_and_engine_in_the_same_composition() -> None:
    with pytest.raises(ValueError, match="target is not supplied"):
        _platform_composition(_offer(target_id="not-present"))
    with pytest.raises(ValueError, match="does not match an engine"):
        _platform_composition(_offer(engine="not-present"))


def test_offering_carriage_is_immutable_and_rejects_mutable_collections() -> None:
    offer = _offer()
    composition = _platform_composition(offer)
    with pytest.raises(FrozenInstanceError):
        offer.charge_authority = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable tuple"):
        replace(composition, offerings=[offer])  # type: ignore[arg-type]


def test_included_execution_match_must_be_exact() -> None:
    with pytest.raises(ValueError, match="require an exact engine"):
        ExactExecutionMatch(
            target_id="remote-api:openai",
            capability="transcribe",
            engine="openai/*",
        )


def test_unknown_rounding_mode_is_rejected_at_the_carrier_boundary() -> None:
    with pytest.raises(ValueError, match="unknown quote rounding mode"):
        CommercialQuoteRounding("invented", 2)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "target_id,capability,engine,meter_key",
    [
        ("nominatim", "geocode", "nominatim", "rows"),
        ("nominatim", "geocode", "nominatim-alias", "rows"),
        ("us-census", "census_demographics", "us_census_acs", "rows"),
        ("us-census", "census_demographics", "census-alias", "rows"),
    ],
)
def test_free_public_apis_cannot_be_priced_by_an_injected_offer(
    target_id: str, capability: str, engine: str, meter_key: str
) -> None:
    with pytest.raises(ValueError, match="free public APIs"):
        CommercialOffering(
            match=CommercialOfferingMatch(target_id, capability, engine),
            quote=CommercialQuoteTerms(
                pricing_key="invented.free-api.price",
                terms_version="invented.v1",
                unit_rate="0.01",
                quantity_unit="row",
                rounding=CommercialQuoteRounding(),
            ),
            settlement=CommercialSettlementTerms(
                meter_key=meter_key,
                meter_units_per_quantity_unit="1",
                ceiling_mode="none",
                row_settlement_mode="all_metered",
            ),
            charge_authority="invented",
            presentation=CommercialPresentation("Invented", "Invented"),
        )


def test_free_public_api_roster_entries_are_not_billable() -> None:
    assert {entry.id: entry.billable for entry in GEOCODE_ENGINE_TABLE} == {
        "opencage": True,
        "nominatim": False,
    }
    assert {entry.id: entry.billable for entry in CENSUS_ENGINE_TABLE} == {
        "us_census_acs": False,
    }


def test_offering_meter_must_match_the_capability_measurement() -> None:
    with pytest.raises(ValueError, match="quoted measurement"):
        replace(
            _offer(),
            settlement=CommercialSettlementTerms(
                meter_key="gpu_seconds",
                meter_units_per_quantity_unit="60",
                ceiling_mode="consented_quantity",
                row_settlement_mode="quoted_successful_rows",
            ),
        )


@pytest.mark.parametrize(
    "capability,meter_key",
    [
        ("ocr", "pages"),
        ("document.convert", "pages"),
        ("translate", "characters"),
        ("geocode", "rows"),
        ("census_demographics", "rows"),
    ],
)
def test_quoted_successful_rows_is_rejected_when_quote_has_no_row_allocation(
    capability: str, meter_key: str
) -> None:
    with pytest.raises(ValueError, match="supported only for transcribe"):
        replace(
            _offer(),
            match=CommercialOfferingMatch(
                "synthetic-row-venue", capability, "synthetic-engine"
            ),
            settlement=CommercialSettlementTerms(
                meter_key=meter_key,
                meter_units_per_quantity_unit="1",
                ceiling_mode="consented_quantity",
                row_settlement_mode="quoted_successful_rows",
            ),
        )


@pytest.mark.parametrize(
    "capability,meter_key",
    [
        ("transcribe", "audio_seconds"),
        ("ocr", "pages"),
        ("document.convert", "pages"),
        ("translate", "characters"),
        ("geocode", "rows"),
        ("census_demographics", "rows"),
    ],
)
def test_all_metered_is_a_generic_settlement_mode(
    capability: str, meter_key: str
) -> None:
    offering = replace(
        _offer(),
        match=CommercialOfferingMatch(
            "synthetic-row-venue", capability, "synthetic-engine"
        ),
        settlement=CommercialSettlementTerms(
            meter_key=meter_key,
            meter_units_per_quantity_unit="1",
            ceiling_mode="consented_quantity",
            row_settlement_mode="all_metered",
        ),
    )
    assert offering.match.capability == capability


def test_funding_and_credential_posture_cannot_disagree() -> None:
    with pytest.raises(ValueError, match="funding and credential context disagree"):
        ExecutionComposition(
            facts=CompositionFacts(
                edition="hosted",
                org_id="org-test",
                funding=PlatformMetered(),
            ),
            provider=_Provider(),
            credential_use_context=CredentialUseContext.open(),
        )


def _complete_pinned_basis() -> dict[str, object]:
    return {
        "kind": "priced",
        "pricing_key": "current.sku",
        "unit_rate": "0.1",
        "estimated_quantity": "2",
        "quantity_unit": "audio_minute",
        "terms_version": "current.terms.v1",
        "quantity_rounding_mode": "half_even",
        "quantity_rounding_decimal_places": 6,
        "meter_key": "audio_seconds",
        "meter_units_per_quantity_unit": "60",
        "ceiling_mode": "consented_quantity",
        "row_settlement_mode": "all_metered",
        "charge_authority": "current.authority",
    }


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("pricing_key", 123),
        ("unit_rate", 1),
        ("quantity_unit", 123),
        ("meter_key", 123),
        ("meter_units_per_quantity_unit", 60),
        ("charge_authority", 123),
    ],
)
def test_cost_promise_reader_never_stringifies_pinned_field_types(
    field: str,
    bad_value: object,
) -> None:
    basis = _complete_pinned_basis()
    assert isinstance(
        cost_basis_from_promises(
            ({"field": "cost", "basis": {**basis, field: bad_value}},),
        ),
        UnpriceableCost,
    )
    assert isinstance(
        cost_basis_from_promises(({"field": "cost", "basis": basis},)),
        PricedCostBasis,
    )


def test_complete_persisted_basis_reconstructs_and_settles() -> None:
    basis = _complete_pinned_basis()

    reconstructed = cost_basis_from_promises(({"field": "cost", "basis": basis},))
    receipt = settle(
        cost_basis=basis,
        metered_units=({"audio_seconds": "120"},),
        price_card_version="current.terms.v1",
        terminal_status="completed",
        all_rows_cancelled=False,
    )

    assert isinstance(reconstructed, PricedCostBasis)
    assert receipt["charge_usd"] == "0.2"
    assert receipt["charge_authority"] == "current.authority"


@pytest.mark.parametrize("missing_term", LIVE_COST_TERM_KEYS)
def test_incomplete_persisted_basis_refuses(missing_term: str) -> None:
    basis = _complete_pinned_basis()
    del basis[missing_term]

    reconstructed = cost_basis_from_promises(({"field": "cost", "basis": basis},))
    receipt = settle(
        cost_basis=basis,
        metered_units=({"audio_seconds": "120"},),
        price_card_version="current.terms.v1",
        terminal_status="completed",
        all_rows_cancelled=False,
    )

    assert isinstance(reconstructed, UnpriceableCost)
    assert receipt["charge_usd"] is None
    assert receipt["unsettleable"] == "settlement_terms_missing"
