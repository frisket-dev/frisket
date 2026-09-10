"""Compiler goldens and discrimination properties for transcription promises.

Each anchor-flavored input pins the EXACT compiled rows — a compiler change
that alters any promise a user would consent to must show up here as a
golden diff, never as a silent redefinition. The discrimination property
closes the spike-identified quiet hazard: a user_claim row that no binding
could ever violate is decoration, not a claim — every claim must reject at
least one candidate binding (op ``unbounded`` is the deliberate exception:
it promises nothing; its PRESENCE, requiring consent, is the claim).
"""

from __future__ import annotations

from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
    compile_route_promises,
)
from frisket.execution.resolver import RouteRowFacts
from frisket.execution.promises import (
    VIOLATED,
    evaluate,
)

import pytest


TEST_SETTLEMENT_TERMS = {
    "terms_version": "test.offer.v1",
    "quantity_rounding_mode": "exact",
    "quantity_rounding_decimal_places": None,
    "meter_key": "gpu_seconds",
    "meter_units_per_quantity_unit": "1",
    "ceiling_mode": "consented_quantity",
    "row_settlement_mode": "all_metered",
    "charge_authority": "test-charge-authority",
}


def _rows(promise_set) -> list[dict]:
    return [p.to_row() for p in promise_set.promises]


def _gates(promises) -> bool:
    """The gate renders exactly when at least one ``user_claim`` row exists.

    The production copy is inline in ``engine/runner/validation.py``, the one
    place that decides.
    """
    return any(p.audience == "user_claim" for p in promises)


# ---------------------------------------------------------------------------
# O1: local, free — zero user claims (the rent bound: no gate ever renders)
# ---------------------------------------------------------------------------

O1_LOCAL = RouteRowFacts(
    target_id="local",
    engine="faster_whisper",
    operator="self",
    egress_class="none",
    region=None,
    credential_source="local",
    cost_posture="operator_borne",
)


def test_golden_o1_local_free():
    ps = compile_route_promises(O1_LOCAL, OperatorBorneZeroCost())
    assert _rows(ps) == [
        {
            "field": "operator",
            "op": "eq",
            "value": "self",
            "basis": None,
            "order_ref": None,
            "audience": "system_promise",
        },
        {
            "field": "egress_class",
            "op": "satisfies_order",
            "value": "none",
            "basis": None,
            "order_ref": "egress.v1",
            "audience": "system_promise",
        },
    ]
    # Zero user claims: no cost row at all (absence, not a claim of zero),
    # no region row (honest absence — never eq null), no gate.
    assert not _gates(ps.promises)


# ---------------------------------------------------------------------------
# gateway: free but egressing — the egress claim alone gates
# ---------------------------------------------------------------------------

GATEWAY = RouteRowFacts(
    target_id="models-gateway",
    engine="moss",
    operator="self",
    egress_class="operator_lan",
    region=None,
    credential_source="local",
    cost_posture="operator_borne",
)


def test_golden_gateway_free_but_egressing():
    ps = compile_route_promises(GATEWAY, OperatorBorneZeroCost())
    assert _rows(ps) == [
        {
            "field": "operator",
            "op": "eq",
            "value": "self",
            "basis": None,
            "order_ref": None,
            "audience": "system_promise",
        },
        {
            "field": "egress_class",
            "op": "satisfies_order",
            "value": "operator_lan",
            "basis": None,
            "order_ref": "egress.v1",
            "audience": "user_claim",
        },
    ]
    assert _gates(ps.promises)  # egress != none gates, cost-free


# ---------------------------------------------------------------------------
# modal, priced — le cost row with the full pricing basis
# ---------------------------------------------------------------------------

MODAL = RouteRowFacts(
    target_id="modal:frisket",
    engine="parakeet-tdt",
    operator="frisket",
    egress_class="frisket_shared",
    region="us-east",
    credential_source="platform_key",
    cost_posture="platform_metered",
)
MODAL_COST = PricedCostBasis(
    pricing_key="test.shared_transcription.gpu_second",
    unit_rate="0.000164",
    estimated_quantity="1200",
    quantity_unit="gpu_second",
    **TEST_SETTLEMENT_TERMS,
    hardware_class="t4",
    throughput_ref="gpu_throughput.v1",
)


def test_golden_modal_priced():
    ps = compile_route_promises(MODAL, MODAL_COST)
    assert _rows(ps) == [
        {
            "field": "operator",
            "op": "eq",
            "value": "frisket",
            "basis": None,
            "order_ref": None,
            "audience": "system_promise",
        },
        {
            "field": "egress_class",
            "op": "satisfies_order",
            "value": "frisket_shared",
            "basis": None,
            "order_ref": "egress.v1",
            "audience": "user_claim",
        },
        {
            "field": "region",
            "op": "eq",
            "value": "us-east",
            "basis": None,
            "order_ref": None,
            "audience": "system_promise",
        },
        {
            "field": "cost",
            "op": "le",
            "value": "0.1968",
            "basis": {
                "pricing_key": "test.shared_transcription.gpu_second",
                "unit_rate": "0.000164",
                "estimated_quantity": "1200",
                "quantity_unit": "gpu_second",
                **TEST_SETTLEMENT_TERMS,
                "hardware_class": "t4",
                "throughput_ref": "gpu_throughput.v1",
            },
            "order_ref": None,
            "audience": "user_claim",
        },
    ]
    assert _gates(ps.promises)


# ---------------------------------------------------------------------------
# remote API, unpriceable — the unbounded claim (no silent free)
# ---------------------------------------------------------------------------

REMOTE = RouteRowFacts(
    target_id="remote-api:openai",
    engine="openai/whisper-1",
    operator="openai",
    egress_class="third_party_api",
    region=None,
    credential_source="org_byok",
    cost_posture="org_key",
)


def test_golden_remote_unpriceable():
    ps = compile_route_promises(REMOTE, UnpriceableCost())
    assert _rows(ps) == [
        {
            "field": "operator",
            "op": "eq",
            "value": "openai",
            "basis": None,
            "order_ref": None,
            "audience": "system_promise",
        },
        {
            "field": "egress_class",
            "op": "satisfies_order",
            "value": "third_party_api",
            "basis": None,
            "order_ref": "egress.v1",
            "audience": "user_claim",
        },
        {
            "field": "cost",
            "op": "unbounded",
            "value": None,
            "basis": None,
            "order_ref": None,
            "audience": "user_claim",
        },
    ]
    assert _gates(ps.promises)


# ---------------------------------------------------------------------------
# compiler input validation
# ---------------------------------------------------------------------------


def test_compiler_input_validation():
    # Validation these negatives pin now lives on RouteRowFacts —
    # THE route-facts type — so it fences every construction site, including
    # the persisted-route load paths, not just the compiler's former
    # subset type.
    with pytest.raises(ValueError):
        RouteRowFacts(
            target_id="local",
            engine="faster_whisper",
            operator="self",
            egress_class="quantum_mesh",
            region=None,
            credential_source="local",
            cost_posture="x",
        )
    with pytest.raises(ValueError):
        RouteRowFacts(
            target_id="local",
            engine="faster_whisper",
            operator="self",
            egress_class="none",
            region="",  # blank is not honest absence
            credential_source="local",
            cost_posture="x",
        )
    with pytest.raises(ValueError):
        RouteRowFacts(
            target_id="local",
            engine="faster_whisper",
            operator=" ",
            egress_class="none",
            region=None,
            credential_source="local",
            cost_posture="x",
        )
    with pytest.raises(ValueError):
        PricedCostBasis(
            pricing_key="k",
            unit_rate="0.02",
            estimated_quantity=1200,
            quantity_unit="u",
            **TEST_SETTLEMENT_TERMS,
            hardware_class="t4",
            throughput_ref=None,
        )  # pin together
    with pytest.raises(ValueError):
        PricedCostBasis(
            pricing_key="k",
            unit_rate="0.02",
            estimated_quantity=12.5,
            quantity_unit="u",
            **TEST_SETTLEMENT_TERMS,
        )  # float qty
    with pytest.raises(ValueError):
        compile_route_promises(O1_LOCAL, cost="free")  # type: ignore[arg-type]


def test_priced_zero_is_still_a_user_claim():
    ps = compile_route_promises(
        GATEWAY,
        PricedCostBasis(
            pricing_key="k.v",
            unit_rate="0",
            estimated_quantity=100,
            quantity_unit="row",
            **TEST_SETTLEMENT_TERMS,
        ),
    )
    cost_rows = [p for p in ps.promises if p.field == "cost"]
    assert len(cost_rows) == 1
    assert cost_rows[0].value == "0"
    # A zero bound is the STRONGEST claim in the set, not an absent one: a
    # meter exists, so the user must see the cost line. Demoting it to a
    # system_promise let a truncated container header or a zero-page PDF
    # delete the cost claim from the confirm dialog entirely.
    assert cost_rows[0].audience == "user_claim"


# ---------------------------------------------------------------------------
# discrimination property: every user_claim row rejects at least one binding
# ---------------------------------------------------------------------------

_GOLDEN_INPUTS = [
    ("o1_local", O1_LOCAL, OperatorBorneZeroCost()),
    ("gateway", GATEWAY, OperatorBorneZeroCost()),
    ("modal", MODAL, MODAL_COST),
    ("remote", REMOTE, UnpriceableCost()),
]

_EGRESS_CANDIDATES = (
    "none",
    "operator_lan",
    "frisket_dedicated_org",
    "frisket_shared",
    "third_party_api",
)


def _candidate_facts(promise) -> list[dict]:
    """A pool of plausible candidate bindings for one promise's field."""
    field = promise.field
    if field == "egress_class":
        return [{field: value} for value in _EGRESS_CANDIDATES]
    if field == "cost":
        basis = promise.basis or {}
        pinned_terms = {
            key: basis.get(key)
            for key in (
                "terms_version",
                "quantity_unit",
                "quantity_rounding_mode",
                "quantity_rounding_decimal_places",
                "meter_key",
                "meter_units_per_quantity_unit",
                "ceiling_mode",
                "row_settlement_mode",
                "charge_authority",
            )
        }
        return [
            {
                field: {
                    "pricing_key": basis.get("pricing_key"),
                    "unit_rate": basis.get("unit_rate"),
                    "estimated_quantity": 10**9,
                    "hardware_class": basis.get("hardware_class"),
                    **pinned_terms,
                }
            },
            {
                field: {
                    "pricing_key": basis.get("pricing_key"),
                    "unit_rate": "999999",
                    "hardware_class": basis.get("hardware_class"),
                    **pinned_terms,
                }
            },
        ]
    return [{field: f"not-{promise.value}"}, {field: "other"}]


@pytest.mark.parametrize("name,route,cost", _GOLDEN_INPUTS)
def test_every_user_claim_discriminates(name, route, cost):
    ps = compile_route_promises(route, cost)
    for promise in ps.promises:
        if promise.audience != "user_claim":
            continue
        if promise.op == "unbounded":
            # The deliberate exception: unbounded promises nothing (no
            # binding can violate it); its presence is the consent claim.
            assert _gates([promise])
            continue
        statuses = [
            evaluate(promise, binding).status for binding in _candidate_facts(promise)
        ]
        assert VIOLATED in statuses, (
            f"{name}: user_claim row {promise.field}/{promise.op} rejects "
            f"no candidate binding — a claim that cannot fail is not a claim"
        )
