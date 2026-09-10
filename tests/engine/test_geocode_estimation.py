"""Typed routed estimation uses the existing single-invocation cost basis."""

from __future__ import annotations

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan


def _program():
    return _typed_map_rows_plan(
        BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("enrich.geocode"),
            ActionRequest(
                action_id="enrich.geocode",
                scope=SheetRows(sheet_id=1),
                params={"source": "address"},
                idempotency_key="quote",
            ),
        )
    ).program


def test_typed_routed_program_refuses_an_alternate_estimation_path():
    program = _program()
    assert program.consumes_resolution and program.execution_capability == "geocode"
    with pytest.raises(RuntimeError, match="invocation resolution"):
        program.estimate(None, {}, [{}])


def test_canonical_typed_identity_uses_declared_params_not_runner_state():
    from frisket.execution.action_identity import action_identity_hash

    spec = {
        "action_kind": "enrich.geocode",
        "sheet_id": 1,
        "params": {"source": "address"},
    }
    identity = action_identity_hash(spec)
    assert (
        action_identity_hash(
            {
                **spec,
                "params": {
                    "engine": "auto",
                    "source": "address",
                    "include_lat_lon": False,
                },
            }
        )
        == identity
    )
    assert (
        action_identity_hash(
            {
                **spec,
                "row_ids": [2],
                "confirmed": True,
                "halted": True,
                "flow_id": "runtime",
            }
        )
        == identity
    )
    assert action_identity_hash({**spec, "params": {"source": "another"}}) != identity
    assert (
        action_identity_hash(
            {**spec, "params": {"source": "address", "engine": "opencage"}}
        )
        != identity
    )


def _resolution(cost_basis, *, engine: str, rows: int | None):
    """A minimal resolved execution for the canonical preview projector."""
    from types import SimpleNamespace

    return SimpleNamespace(
        cost_basis=cost_basis,
        resolution=SimpleNamespace(
            facts=SimpleNamespace(engine=engine),
            estimate_basis=SimpleNamespace(
                quantity_hint=float(rows) if rows is not None else None
            ),
        ),
    )


def test_geocode_routed_estimate_priced_arm_opencage():
    """The priced arm carries one cost and one provider-price identity."""
    from frisket.execution.price_book import OperatorBorne, quote_geocode
    from frisket.execution.promise_compiler import PricedCostBasis

    basis = quote_geocode(
        target_id="opencage",
        engine="opencage",
        funding=OperatorBorne(),
        offering=None,
        rows=7,
    )
    assert isinstance(basis, PricedCostBasis)
    est = _program().estimate(
        None,
        {},
        [{}] * 7,
        resolution=_resolution(basis, engine="opencage", rows=7),
    )
    assert est == {
        "cost": pytest.approx(0.07),
        "cost_source": "pricing_data",
        "engine": "opencage",
        "pricing_key": "geocode.opencage.row",
    }


def test_geocode_routed_estimate_nominatim_has_no_price():
    """Nominatim is structurally known-zero without a made-up tariff."""
    from frisket.execution.price_book import OperatorBorne, quote_geocode
    from frisket.execution.promise_compiler import OperatorBorneZeroCost

    basis = quote_geocode(
        target_id="nominatim",
        engine="nominatim",
        funding=OperatorBorne(),
        offering=None,
        rows=4,
    )
    assert isinstance(basis, OperatorBorneZeroCost)
    est = _program().estimate(
        None,
        {},
        [{}] * 4,
        resolution=_resolution(basis, engine="nominatim", rows=4),
    )
    assert est == {
        "cost": 0.0,
        "cost_source": "free_public_api",
        "engine": "nominatim",
    }


def test_geocode_routed_estimate_priced_arm_preserves_a_sub_micro_rate():
    """A valid sub-micro per-row rate (the routed-capability contract): rounding
    to 6 places would report a real $0.0000004/row charge as a fabricated
    $0.00. The canonical projector preserves eight decimal places."""
    from frisket.execution.promise_compiler import PricedCostBasis

    basis = PricedCostBasis(
        pricing_key="geocode.opencage.row",
        unit_rate="0.0000004",
        estimated_quantity="1",
        quantity_unit="row",
        terms_version=None,
        quantity_rounding_mode="exact",
        quantity_rounding_decimal_places=None,
        meter_key="rows",
        meter_units_per_quantity_unit="1",
        ceiling_mode="none",
        row_settlement_mode="all_metered",
        charge_authority="provider_direct",
    )
    est = _program().estimate(
        None,
        {},
        [{}],
        resolution=_resolution(basis, engine="opencage", rows=1),
    )
    assert round(0.0000004, 6) == 0.0  # the bug this test catches, restated
    assert est["cost"] == pytest.approx(0.0000004)
    assert est["cost"] != 0.0


def test_geocode_routed_estimate_unpriceable_arm():
    """No fabricated number when the row count (or the tariff) is unknown —
    forces the explicit confirm, never a guess."""
    from frisket.execution.promise_compiler import UnpriceableCost

    est = _program().estimate(
        None,
        {},
        [],
        resolution=_resolution(UnpriceableCost(), engine="opencage", rows=None),
    )
    assert est["cost"] is None
    assert est["cost_source"] == "unknown"
    assert est["engine"] == "opencage"
    assert "warning" in est


def test_geocode_routed_estimate_free_local_arm():
    """Handled for completeness, mirroring OCR's three-arm renderer, even
    though today's roster never reaches it: every declared geocode venue
    bills a per-row SKU."""
    from frisket.execution.promise_compiler import OperatorBorneZeroCost

    est = _program().estimate(
        None,
        {},
        [{}] * 5,
        resolution=_resolution(OperatorBorneZeroCost(), engine="opencage", rows=5),
    )
    assert est["cost"] == 0.0
    assert est["cost_source"] == "free_local"
    assert est["engine"] == "opencage"
