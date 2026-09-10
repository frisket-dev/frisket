"""The consented QUANTITY is half the consented bound, and settlement must
read it.

Two defects this file pins:

1. ``settle()`` read ``pricing_key``/``unit_rate``/``quantity_unit`` off the
   pinned basis and ignored ``estimated_quantity`` — the other factor of the
   ``cost le <bound>`` row the user actually agreed to. A media file whose
   container header understates its duration quotes low and used to debit the
   full actual meter, with no customer-charge ceiling anywhere in the join.
2. The compiler demoted a zero-bound cost row out of the user-visible claim
   set (``audience="user_claim" if bound > 0``). A truncated header or a
   zero-page PDF therefore erased the run's only cost claim, and ``cost le 0``
   evaluated SATISFIED while settlement could debit metered work above the
   invisible zero ceiling. Audience is a consent-visibility
   decision; deriving it from an
   arithmetic value that untrusted media metadata controls is the hole.
"""

from __future__ import annotations

import pytest

from frisket.execution.price_book import (
    SKU_DATALAB_OCR_PAGE,
    settle,
)
from frisket.execution.promise_compiler import PricedCostBasis, compile_route_promises
from frisket.execution.resolver import RouteRowFacts

# ---------------------------------------------------------------------------
# (1) the meter is bounded by the consented quantity
# ---------------------------------------------------------------------------

TEST_OFFERING_KEY = "test.synthetic.audio_minute"
TEST_TERMS_VERSION = "test.synthetic.terms.v1"
OFFERING_TERMS = {
    "terms_version": TEST_TERMS_VERSION,
    "quantity_rounding_mode": "half_even",
    "quantity_rounding_decimal_places": 6,
    "meter_key": "audio_seconds",
    "meter_units_per_quantity_unit": "60",
    "ceiling_mode": "consented_quantity",
    "row_settlement_mode": "all_metered",
    "charge_authority": "test.synthetic.authority",
}


#: 10 audio-minutes at a synthetic injected rate, spelled as the persisted
#: JSON shape. Base itself ships neither this key nor this rate.
HOSTED_BASIS = {
    "kind": "priced",
    "pricing_key": TEST_OFFERING_KEY,
    "unit_rate": "0.03",
    "estimated_quantity": "10",
    "quantity_unit": "audio_minute",
    **OFFERING_TERMS,
}


def test_settle_caps_the_customer_charge_and_names_the_absorbed_overage():
    """The header-lie attack: a file whose container understates its duration
    quotes short and meters the real audio. The consented ceiling is 10
    audio-minutes; the decoder reported 72000 audio-seconds, i.e. 1200. The
    actual usage and its rated value stay visible, but the customer is never
    charged more than the amount the 402 described as "up to"."""
    result = settle(
        cost_basis=HOSTED_BASIS,
        metered_units=[{"audio_seconds": 72000.0, "gpu_seconds": 11.0}],
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert result["rated_charge_usd"] == "36"
    assert result["charge_usd"] == "0.3"
    assert result["charged_quantity"] == "10"
    assert result["absorbed_overage_usd"] == "35.7"
    assert result["exceeds_consented"] is True
    # The evidence a human needs to adjudicate, in the CONSENTED unit.
    assert result["consented_quantity"] == "10"
    assert result["billable_quantity"] == "1200"
    assert result["quantity_unit"] == "audio_minute"


def test_settle_bills_normally_up_to_the_consented_quantity():
    """The fence is a ceiling, not a target: at and below the consented
    quantity settlement is unchanged."""
    at_bound = settle(
        cost_basis=HOSTED_BASIS,
        metered_units=[{"audio_seconds": 600.0}],  # exactly 10 audio-minutes
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert at_bound["charge_usd"] == "0.3"
    assert at_bound["rated_charge_usd"] == "0.3"
    assert at_bound["charged_quantity"] == "10"
    assert at_bound["absorbed_overage_usd"] == "0"
    assert at_bound["exceeds_consented"] is False

    under = settle(
        cost_basis=HOSTED_BASIS,
        metered_units=[{"audio_seconds": 120.0}],  # 2 audio-minutes
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert under["charge_usd"] == "0.06"
    assert under["rated_charge_usd"] == "0.06"
    assert under["charged_quantity"] == "2"
    assert under["absorbed_overage_usd"] == "0"
    assert under["exceeds_consented"] is False


def test_the_comparison_is_in_the_consented_unit_not_the_meter_unit():
    """The subtlety that survives the 2026-07-25 ruling: hosted transcription
    is CONSENTED in audio-minutes and METERED in audio-SECONDS. Comparing 540
    metered seconds against a consented quantity of 10 would flag every
    ordinary hosted run as an overrun. The seconds->minutes scale has to run
    first. (What the ruling removed is the OTHER step: there is no longer a
    conversion between two different physical quantities in this path, only a
    unit scale within one.)"""
    result = settle(
        cost_basis=HOSTED_BASIS,
        metered_units=[{"audio_seconds": 540.0}],  # 9 audio-minutes, well under
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert result["metered_quantity"] == "540"
    assert result["metered_unit"] == "audio_seconds"
    assert result["billable_quantity"] == "9"
    assert result["charge_usd"] == "0.27"
    assert result.get("exceeds_consented") in (False, None)


def test_the_bound_holds_for_a_provider_list_price_sku_too():
    """A provider bill quoted AND metered in gpu-seconds, so
    no conversion — but the same ceiling."""
    basis = {
        "kind": "priced",
        "pricing_key": "test.synthetic.gpu_second",
        "unit_rate": "0.000164",
        "estimated_quantity": "100",
        "quantity_unit": "gpu_second",
        "terms_version": None,
        "quantity_rounding_mode": "half_even",
        "quantity_rounding_decimal_places": 3,
        "meter_key": "gpu_seconds",
        "meter_units_per_quantity_unit": "1",
        "ceiling_mode": "none",
        "row_settlement_mode": "all_metered",
        "charge_authority": "provider_direct",
    }
    ok = settle(
        cost_basis=basis,
        metered_units=[{"gpu_seconds": 100}],
        price_card_version=None,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert ok["charge_usd"] == "0.0164"

    over = settle(
        cost_basis=basis,
        metered_units=[{"gpu_seconds": 100.5}],
        price_card_version=None,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    # Provider-direct pricing is evidence of the provider's actual bill, not a
    # Frisket retail charge. Frisket cannot absorb or cap somebody else's
    # invoice, so this path deliberately keeps its existing semantics.
    assert over["charge_usd"] == "0.016482"
    assert over["rated_charge_usd"] == "0.016482"
    assert over["charged_quantity"] == "100.5"
    assert over["absorbed_overage_usd"] == "0"
    assert over["exceeds_consented"] is True


def test_the_bound_holds_across_several_calls_on_one_attempt():
    """The overrun is the attempt's TOTAL, not any single call: three pages
    consented, one page per call, four calls."""
    basis = {
        "kind": "priced",
        "pricing_key": SKU_DATALAB_OCR_PAGE,
        "unit_rate": "0.01",
        "estimated_quantity": "3",
        "quantity_unit": "page",
        "terms_version": None,
        "quantity_rounding_mode": "exact",
        "quantity_rounding_decimal_places": None,
        "meter_key": "pages",
        "meter_units_per_quantity_unit": "1",
        "ceiling_mode": "none",
        "row_settlement_mode": "all_metered",
        "charge_authority": "provider_direct",
    }
    result = settle(
        cost_basis=basis,
        metered_units=[{"pages": 1}] * 4,
        price_card_version=None,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    # Datalab here is the operator/provider-list SKU, not the platform retail
    # SKU. Its receipt continues to name the provider's actual cost.
    assert result["charge_usd"] == "0.04"
    assert result["rated_charge_usd"] == "0.04"
    assert result["charged_quantity"] == "4"
    assert result["absorbed_overage_usd"] == "0"
    assert result["exceeds_consented"] is True
    assert result["billable_quantity"] == "4"
    assert result["consented_quantity"] == "3"
    # Still a receipt: the counts a reader needs survive the refusal.
    assert result["rated_calls"] == 4


def test_a_basis_missing_the_consented_quantity_is_malformed():
    """A missing pin is not reinterpreted as the intentional null sentinel."""
    basis = dict(HOSTED_BASIS)
    del basis["estimated_quantity"]
    result = settle(
        cost_basis=basis,
        metered_units=[{"audio_seconds": 600.0}],
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert result["charge_usd"] is None
    assert result["rated_calls"] == 0
    assert result["unsettleable"] == "settlement_terms_invalid"


def test_an_explicit_null_consented_quantity_remains_intentionally_unbounded():
    """JSON null is the complete unbounded quote sentinel, not a missing pin."""
    basis = {**HOSTED_BASIS, "estimated_quantity": None}
    result = settle(
        cost_basis=basis,
        metered_units=[{"audio_seconds": 600.0}],
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert result["rated_charge_usd"] == "0.3"
    assert result["charge_usd"] is None
    assert result["unsettleable"] == "consent_ceiling_missing"
    assert result["consented_quantity"] is None


def test_a_priced_attempt_with_no_meter_rows_refuses_to_invent_zero():
    """An empty fact set cannot distinguish no calls from lost metering.
    A priced attempt therefore refuses instead of turning absence into $0."""
    result = settle(
        cost_basis=HOSTED_BASIS,
        metered_units=[],
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert result["charge_usd"] is None
    assert result["unmetered_calls"] == 0
    assert result["unsettleable"] == "unmetered"


@pytest.mark.parametrize(
    "bad_units",
    [
        pytest.param({}, id="missing-meter"),
        pytest.param({"audio_seconds": "not-a-number"}, id="malformed-meter"),
        pytest.param({"audio_seconds": -60.0}, id="negative-meter"),
    ],
)
def test_a_priced_attempt_with_any_invalid_meter_refuses_a_partial_total(
    bad_units: dict,
):
    """One valid call cannot turn an incomplete ledger into a total charge.

    The attempt made two provider calls.  If one call's meter is absent,
    malformed, or impossible, rating only the other call yields a plausible
    but false receipt total.  Negative work is invalid metering rather than a
    credit that can cancel real usage.
    """
    result = settle(
        cost_basis=HOSTED_BASIS,
        metered_units=[{"audio_seconds": 60.0}, bad_units],
        price_card_version=TEST_TERMS_VERSION,
        terminal_status="completed",
        all_rows_cancelled=False,
    )

    assert result["rated_calls"] == 1
    assert result["unmetered_calls"] == 1
    assert result["charge_usd"] is None
    assert result["rated_charge_usd"] is None
    assert result["charged_quantity"] is None
    assert result["absorbed_overage_usd"] is None
    assert result["unsettleable"] == "unmetered"
    assert result["exceeds_consented"] is None, (
        "a metered lower bound cannot prove the complete attempt stayed "
        "within the consented quantity"
    )


# ---------------------------------------------------------------------------
# (2) a priced basis always produces a user-visible cost claim
# ---------------------------------------------------------------------------

_HOSTED_ROUTE = RouteRowFacts(
    target_id="modal:transcribe",
    engine="parakeet-tdt",
    operator="frisket",
    egress_class="frisket_shared",
    region=None,
    credential_source="platform_key",
    cost_posture="platform_metered",
)


@pytest.mark.parametrize(
    "quantity",
    ["0", 0],  # a truncated header (0 audio-minutes) and quote_ocr(pages=0)
)
def test_a_zero_quantity_priced_basis_is_still_a_user_claim(quantity):
    """A zero quantity must not delete the run's cost claim. Before the fix
    the row compiled as ``system_promise``, so the confirm gate never showed
    a cost line and ``cost le 0`` was evaluated as a system detail while
    settlement could debit metered work above an invisible zero ceiling."""
    ps = compile_route_promises(
        _HOSTED_ROUTE,
        PricedCostBasis(
            pricing_key=TEST_OFFERING_KEY,
            unit_rate="0.03",
            estimated_quantity=quantity,
            quantity_unit="audio_minute",
            **OFFERING_TERMS,
        ),
    )
    cost_rows = [p for p in ps.promises if p.field == "cost"]
    assert len(cost_rows) == 1
    assert cost_rows[0].value == "0"
    assert cost_rows[0].audience == "user_claim"
    assert cost_rows[0].basis["estimated_quantity"] == quantity


def test_the_cost_claim_survives_a_zero_rate_too():
    """The same hole reached through the other factor: audience is decided by
    'is there a meter', never by the arithmetic of the bound."""
    ps = compile_route_promises(
        _HOSTED_ROUTE,
        PricedCostBasis(
            pricing_key="k.v",
            unit_rate="0",
            estimated_quantity=100,
            quantity_unit="row",
            **OFFERING_TERMS,
        ),
    )
    cost_rows = [p for p in ps.promises if p.field == "cost"]
    assert cost_rows[0].audience == "user_claim"


def test_a_zero_bound_claim_is_visible_but_the_fence_cannot_violate_it():
    """The KNOWN limit of the audience fix, pinned so nobody mistakes it for
    settlement enforcement.

    ``_eval_le_cost`` scores ``observed_rate x CONSENTED_quantity`` (quantity
    is deliberately basis-side: "quantity is the admission gate's job, not the
    binding's"). With a consented quantity of zero the product is zero for
    every possible rate, so the run-start fence reports SATISFIED no matter
    how expensive the venue turns out to be. Making the row a ``user_claim``
    buys VISIBILITY at the confirm gate; the ceiling on the QUANTITY is
    settlement's job, which is the first half of this file.
    """
    from frisket.execution.promises import SATISFIED, evaluate

    ps = compile_route_promises(
        _HOSTED_ROUTE,
        PricedCostBasis(
            pricing_key=TEST_OFFERING_KEY,
            unit_rate="0.03",
            estimated_quantity=0,
            quantity_unit="audio_minute",
            **OFFERING_TERMS,
        ),
    )
    cost_row = [p for p in ps.promises if p.field == "cost"][0]
    result = evaluate(
        cost_row,
        {
            "cost": {
                "pricing_key": TEST_OFFERING_KEY,
                "unit_rate": "999999",
                **OFFERING_TERMS,
                "quantity_unit": "audio_minute",
            }
        },
    )
    assert result.status == SATISFIED  # rate x 0 — the fence has no quantity
