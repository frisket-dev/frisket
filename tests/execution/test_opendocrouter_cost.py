"""Unknown estimates must retain actual-charge authority through admission."""

import pytest

from frisket.execution.attempt import cost_basis_from_promises
from frisket.execution.price_book import OperatorBorne, quote_ocr, settle
from frisket.execution.promise_compiler import ProviderUsageCost, UnpriceableCost

ENGINE = "opendocrouter/rednote-hilab/dots.mocr"


def test_unknown_provider_quote_survives_promise_roundtrip():
    quote = quote_ocr(
        target_id="opendocrouter",
        engine=ENGINE,
        funding=OperatorBorne(),
        offering=None,
        pages=None,
    )
    assert isinstance(quote, ProviderUsageCost)
    assert isinstance(quote, UnpriceableCost)
    restored = cost_basis_from_promises(
        [
            {
                "field": "cost",
                "basis": {"kind": "provider_usage", "pricing_key": quote.pricing_key},
            }
        ]
    )
    assert restored == quote


@pytest.mark.parametrize("key", [None, "", " bad ", 42])
def test_malformed_provider_basis_remains_unknown(key):
    basis = cost_basis_from_promises(
        [{"field": "cost", "basis": {"kind": "provider_usage", "pricing_key": key}}]
    )
    assert type(basis) is UnpriceableCost


@pytest.mark.parametrize(
    "costs,expected",
    [
        ([], None),
        ([None], None),
        ([0], "0"),
        ([0.02, 0.03], "0.05"),
        ([0.02, None], None),
    ],
)
def test_reported_cost_requires_complete_facts(costs, expected):
    result = settle(
        cost_basis={"kind": "provider_usage", "pricing_key": ENGINE + ".parse_page"},
        metered_units=[{"pages": 1} for _ in costs],
        provider_costs=costs,
        price_card_version=None,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert result["charge_usd"] == expected
