"""Page-based token estimates authorize OCR without becoming its actual bill."""

import sqlite3
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

import pytest

from frisket.execution.attempt import attempt_settlement
from frisket.execution.price_book import (
    OperatorBorne,
    live_cost_fact,
    quote_ocr,
    settle,
)
from frisket.execution.promise_compiler import PricedCostBasis, UnpriceableCost


def _quote(pages=1, engine="gemini/gemini-3.5-flash-lite"):
    return quote_ocr(
        target_id="remote-api:gemini",
        engine=engine,
        funding=OperatorBorne(),
        offering=None,
        pages=pages,
    )


def test_vlm_quote_scales_with_pages_and_matches_dispatch_rate():
    one, three = _quote(), _quote(3)
    assert isinstance(one, PricedCostBasis)
    assert isinstance(three, PricedCostBasis)
    assert one.bound == Decimal("0.0114688")
    assert three.bound == one.bound * 3
    assert one.charge_authority == "provider_usage"
    live = live_cost_fact(
        capability="ocr",
        target_id="remote-api:gemini",
        engine="gemini/gemini-3.5-flash-lite",
        funding=OperatorBorne(),
        offering=None,
        hardware_class=None,
    )
    assert live["unit_rate"] == one.unit_rate
    assert live["charge_authority"] == one.charge_authority


def test_missing_pages_or_model_rates_remain_unknown():
    assert isinstance(_quote(None), UnpriceableCost)
    assert isinstance(_quote(engine="gemini/unpriced-model"), UnpriceableCost)


@pytest.mark.parametrize(
    "costs,expected",
    [
        ([0.02, 0.03], "0.05"),
        ([0.0, 0.0], "0"),
        ([0.02, None], None),
        ([0.02, -1], None),
        ([0.02, float("nan")], None),
        ([0.02, float("inf")], None),
        ([0.02], None),
        (None, None),
    ],
)
def test_vlm_settlement_uses_complete_actual_costs_not_estimate(costs, expected):
    basis = {"kind": "priced", **asdict(_quote(2))}
    receipt = settle(
        cost_basis=basis,
        metered_units=[{"pages": 1}, {"pages": 1}],
        provider_costs=costs,
        price_card_version=None,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert receipt["charge_usd"] == expected
    assert "rated_charge_usd" not in receipt


def test_attempt_settlement_reads_recorded_provider_costs():
    # Exercise the real settlement join; no attempt records are required by
    # this read-only boundary. Costs from other attempts must not leak in.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "CREATE TABLE model_calls (id, created_at, attempt_id, row_id, units, "
            "provider_cost_usd)"
        )
        db.executemany(
            "INSERT INTO model_calls VALUES (?, 0, ?, 1, '{\"pages\":1}', ?)",
            [(1, "ours", 0.03), (2, "ours", 0.02), (3, "other", 10)],
        )
        kwargs = dict(
            cost_basis={"kind": "priced", **asdict(_quote(2))},
            price_card_version=None,
            run_reclaimed=False,
            terminal_status="completed",
        )
        receipt = attempt_settlement(
            SimpleNamespace(db=db), attempt_id="ours", **kwargs
        )
        assert receipt["charge_usd"] == "0.05"
        assert receipt["rated_calls"] == 2
        missing = attempt_settlement(
            SimpleNamespace(db=db), attempt_id="missing", **kwargs
        )
        assert missing["charge_usd"] is None
    finally:
        db.close()
