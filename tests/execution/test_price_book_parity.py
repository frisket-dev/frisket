"""Base price-book fences after commercial venue pricing moved downstream."""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    owned_media_metadata_document,
)
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.price_book import (
    OperatorBorne,
    PlatformMetered,
    byok_audio_second_rate,
    quote_commercial_offering,
    quote_transcription,
    settle,
    sku_for,
)
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
)
from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    NOMINATIM_TARGET_ID,
    US_CENSUS_TARGET_ID,
)


def _offering(
    *,
    rate: str = "0.017",
    rounding: CommercialQuoteRounding | None = None,
    row_mode: str = "all_metered",
) -> CommercialOffering:
    return CommercialOffering(
        match=CommercialOfferingMatch(
            target_id="synthetic-venue",
            capability="transcribe",
            engine="parakeet-tdt",
        ),
        quote=CommercialQuoteTerms(
            pricing_key="test.synthetic.audio_minute",
            terms_version="test.synthetic.terms.v3",
            unit_rate=rate,
            quantity_unit="audio_minute",
            rounding=rounding or CommercialQuoteRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode=row_mode,  # type: ignore[arg-type]
        ),
        charge_authority="test.synthetic.authority",
        presentation=CommercialPresentation(
            venue_label="Synthetic venue",
            billing_label="Synthetic billing terms",
        ),
    )


@pytest.mark.parametrize(
    "target_id,engine,expected_type",
    [
        ("local", "faster_whisper", OperatorBorneZeroCost),
        ("local-onnx", "parakeet-tdt", OperatorBorneZeroCost),
        ("models-gateway", "whisper-turbo", OperatorBorneZeroCost),
        ("remote-api:openai", "openai/definitely-not-a-model", UnpriceableCost),
    ],
)
def test_provider_direct_quote_posture_is_unchanged(
    target_id: str, engine: str, expected_type: type
) -> None:
    basis = quote_transcription(
        target_id=target_id,
        engine=engine,
        funding=OperatorBorne(),
        offering=None,
        audio_seconds=90,
        hardware_class=None,
    )
    assert isinstance(basis, expected_type)


def test_known_own_key_audio_price_stays_visible() -> None:
    basis = quote_transcription(
        target_id="remote-api:openai",
        engine="openai/whisper-1",
        funding=OperatorBorne(),
        offering=None,
        audio_seconds=90,
        hardware_class=None,
    )
    assert isinstance(basis, PricedCostBasis)
    assert basis.pricing_key == "openai/whisper-1.audio_second"
    assert basis.terms_version is None
    assert basis.charge_authority == "provider_direct"
    assert basis.meter_key == "audio_seconds"


@pytest.mark.parametrize(
    ("engine", "rate", "source", "key"),
    [
        ("ollama/@desk/unlisted", Decimal("0"), "free_local", None),
        (
            "openai/whisper-1",
            Decimal("0.0001"),
            "pricing_data",
            "openai/whisper-1.audio_second",
        ),
        (
            "openai/unpriced-audio",
            None,
            "unknown",
            "openai/unpriced-audio.audio_second",
        ),
    ],
)
def test_audio_rate_lookup_carries_provenance_by_value(
    engine: str,
    rate: Decimal | None,
    source: str,
    key: str | None,
) -> None:
    quoted = byok_audio_second_rate(engine)
    assert quoted.rate == rate
    assert quoted.cost_source == source
    assert quoted.pricing_key == key


@pytest.mark.parametrize(
    ("engine", "cost", "source", "key"),
    [
        ("faster_whisper", 0.0, "free_local", None),
        (
            "openai/whisper-1",
            0.0002,
            "pricing_data",
            "openai/whisper-1.audio_second",
        ),
        (
            "openai/unpriced-audio",
            None,
            "unknown",
            "openai/unpriced-audio.audio_second",
        ),
    ],
)
def test_transcription_estimate_preserves_audio_rate_provenance(
    tmp_path,
    monkeypatch,
    engine: str,
    cost: float | None,
    source: str,
    key: str | None,
) -> None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
    from frisket.execution.provider import (
        ExecutionCompositionContext,
        open_execution_composition,
    )
    from frisket.execution.resolve_for_action import resolve_for_action
    from frisket.execution.resolver import ResolvedExecution

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-audio-quote-key")
    router = ModelRouter()

    project = Project.create(tmp_path / "audio-rate.frisket")
    try:
        digest = project.add_blob(
            b"RIFFxxxxWAVEfmt ",
            filename="audio.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 2.0, "kind": "audio"}
            ),
        )
        rows = [
            {
                "media": MediaBlobStore.media_cell(
                    digest,
                    filename="audio.wav",
                    mime="audio/wav",
                )
            }
        ]
        sheet = project.add_sheet("Audio")
        column = project.add_column(sheet, "media", "json")
        project.add_rows(sheet, rows, {"media": column})
        action = ACTION_REGISTRY.get("media.transcribe")
        plan = _typed_map_rows_plan(
            BoundTypedActionRequest.bind(
                action,
                ActionRequest(
                    action_id=action.action_id,
                    scope=SheetRows(sheet_id=sheet),
                    params={"source": "media", "engine": engine},
                    idempotency_key="audio-rate-estimate",
                ),
            )
        )
        resolved = resolve_for_action(
            project,
            plan.spec,
            plan.program,
            composition=open_execution_composition(
                project,
                router,
                ExecutionCompositionContext.direct(),
            ),
        )
        assert isinstance(resolved, ResolvedExecution)
        estimate = plan.program.estimate(project, plan.spec, rows, resolution=resolved)
    finally:
        project.close()
    assert estimate is not None
    assert estimate["cost"] == cost
    assert estimate["cost_source"] == source
    # Unknown quotes retain their unknown cost source, but cannot name a
    # priced basis; the raw rate lookup above still diagnoses its missing key.
    assert estimate.get("pricing_key") == (key if cost is not None else None)


def test_funding_alone_never_mints_a_platform_quote() -> None:
    basis = quote_transcription(
        target_id="local-onnx",
        engine="parakeet-tdt",
        funding=PlatformMetered(),
        offering=None,
        audio_seconds=600,
        hardware_class=None,
    )
    assert basis == OperatorBorneZeroCost()
    assert "funding" not in inspect.signature(sku_for).parameters
    assert (
        sku_for(
            capability="transcribe",
            target_id="local-onnx",
            engine="parakeet-tdt",
        )
        is None
    )


def test_synthetic_offer_pins_every_quote_and_settlement_term() -> None:
    basis = quote_commercial_offering(
        offering=_offering(),
        metered_quantity=121,
    )
    assert isinstance(basis, PricedCostBasis)
    assert basis == PricedCostBasis(
        pricing_key="test.synthetic.audio_minute",
        unit_rate="0.017",
        estimated_quantity="2.016667",
        quantity_unit="audio_minute",
        terms_version="test.synthetic.terms.v3",
        quantity_rounding_mode="half_even",
        quantity_rounding_decimal_places=6,
        meter_key="audio_seconds",
        meter_units_per_quantity_unit="60",
        ceiling_mode="consented_quantity",
        row_settlement_mode="all_metered",
        charge_authority="test.synthetic.authority",
    )


def test_synthetic_offer_settles_from_the_pin_and_caps_at_consent() -> None:
    basis = quote_commercial_offering(
        offering=_offering(rate="0.02"),
        metered_quantity=120,
    )
    assert isinstance(basis, PricedCostBasis)
    receipt = settle(
        cost_basis={"kind": "priced", **basis.__dict__},
        metered_units=[{"audio_seconds": 180}],
        price_card_version=basis.terms_version,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert receipt["rated_charge_usd"] == "0.06"
    assert receipt["charge_usd"] == "0.04"
    assert receipt["charged_quantity"] == "2"
    assert receipt["absorbed_overage_usd"] == "0.02"
    assert receipt["charge_authority"] == "test.synthetic.authority"


def test_unknown_offered_quantity_pins_terms_but_cannot_invent_a_ceiling() -> None:
    basis = quote_commercial_offering(
        offering=_offering(rate="0.02"),
        metered_quantity=None,
    )
    assert isinstance(basis, PricedCostBasis)
    assert basis.estimated_quantity is None

    receipt = settle(
        cost_basis={"kind": "priced", **basis.__dict__},
        metered_units=[{"audio_seconds": 120}],
        price_card_version=basis.terms_version,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert receipt["rated_charge_usd"] == "0.04"
    assert receipt["charge_usd"] is None
    assert receipt["unsettleable"] == "consent_ceiling_missing"


def test_unequal_row_allocations_sum_to_the_exact_pinned_aggregate() -> None:
    basis = quote_commercial_offering(
        offering=_offering(row_mode="quoted_successful_rows"),
        metered_quantity=181.23474,
        row_metered_quantities={9: 0.00006, 3: 60.00012, 8: 121.23456},
    )
    assert isinstance(basis, PricedCostBasis)
    assert basis.row_quote_quantities == (
        (3, "1.000002"),
        (8, "2.020576"),
        (9, "0.000001"),
    )
    assert sum(
        (Decimal(quantity) for _row_id, quantity in basis.row_quote_quantities),
        Decimal(0),
    ) == Decimal(str(basis.estimated_quantity))


def test_row_offer_derives_aggregate_from_the_exact_row_values() -> None:
    basis = quote_commercial_offering(
        offering=_offering(row_mode="quoted_successful_rows"),
        # Deliberately carries the ordinary binary-float sum. The row map is
        # the quote authority, so this cannot disagree with a separate sum.
        metered_quantity=0.1 + 0.2,
        row_metered_quantities={1: 0.1, 2: 0.2},
    )
    assert isinstance(basis, PricedCostBasis)
    assert basis.row_quote_quantities == ((1, "0.001667"), (2, "0.003333"))
    assert basis.estimated_quantity == "0.005"


def test_capability_mint_rejects_an_offer_for_another_execution_choice() -> None:
    with pytest.raises(ValueError, match="does not match the selected"):
        quote_transcription(
            target_id="local",
            engine="openai/whisper-1",
            funding=PlatformMetered(),
            offering=_offering(),
            audio_seconds=60,
            hardware_class=None,
        )


def test_settlement_rejects_malformed_pinned_text_instead_of_coercing_it() -> None:
    basis = quote_commercial_offering(
        offering=_offering(),
        metered_quantity=60,
    )
    assert isinstance(basis, PricedCostBasis)
    malformed = {"kind": "priced", **basis.__dict__, "meter_key": None}

    receipt = settle(
        cost_basis=malformed,
        metered_units=[{"None": 2}],
        price_card_version=basis.terms_version,
        terminal_status="completed",
        all_rows_cancelled=False,
    )

    assert receipt["charge_usd"] is None
    assert receipt["unsettleable"] == "settlement_terms_invalid"


def test_base_contains_no_platform_card_or_concrete_retail_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import frisket.execution.price_book as price_book

    for dead in (
        "PriceCard",
        "PRICE_CARD_VERSION",
        "CURRENT_CARD",
        "current_card",
        "card_for_version",
        "SKU_HOSTED_TRANSCRIPTION",
        "PLATFORM_RETAIL_CHARGE_CEILING_SKUS",
        "ROW_QUOTE_SETTLEMENT_SKUS",
    ):
        assert not hasattr(price_book, dead)

    monkeypatch.setenv("FRISKET_NOMINATIM_USD_PER_ROW", "0.03")
    monkeypatch.setenv("FRISKET_CENSUS_DEMOGRAPHICS_USD_PER_ROW", "0.03")
    free_venues = (
        (
            CAPABILITY_GEOCODE,
            NOMINATIM_TARGET_ID,
            "nominatim",
            price_book.quote_geocode,
        ),
        (
            CAPABILITY_CENSUS,
            US_CENSUS_TARGET_ID,
            "us_census_acs",
            price_book.quote_census,
        ),
    )
    for capability, target_id, engine, quote in free_venues:
        assert (
            sku_for(capability=capability, target_id=target_id, engine=engine) is None
        )
        assert (
            quote(
                target_id=target_id,
                engine=engine,
                funding=OperatorBorne(),
                offering=None,
                rows=10,
            )
            == OperatorBorneZeroCost()
        )
