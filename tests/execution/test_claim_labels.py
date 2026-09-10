"""Goldens for the claims-gate trust labels.

Every rendered string is pinned exactly: label copy is a product surface
computed per (egress_class, operator, cost_posture), and changing it must be
a reviewed diff here — never drift. Labels are display-only by contract, so
these goldens can change without touching any recorded hash.
"""

from frisket.execution.claim_labels import (
    claim_display,
    cost_posture_label,
    trust_label,
)
from frisket.execution.commercial import CommercialPresentation
from frisket.execution.resolve_for_action import gate_claims_payload
from frisket.execution.resolver import RouteRowFacts
from frisket.execution.promises import EGRESS_ORDER_REF, Promise

TRUST_LABEL_GOLDENS = {
    ("none", "self", "operator_borne"): "Media stays on this machine.",
    ("operator_lan", "self", "operator_borne"): (
        "Media leaves this machine for your own infrastructure "
        "(LAN service you operate)."
    ),
    ("operator_lan", "lanbox-owner", "operator_borne"): (
        "Media leaves this machine for infrastructure operated by lanbox-owner (LAN)."
    ),
    ("third_party_api", "acme-ai", "operator_borne"): (
        "Media is sent to acme-ai, a third-party API."
    ),
    ("third_party_api", "openai", "operator_borne"): (
        "Media is sent to openai, a third-party API."
    ),
    ("frisket_shared", "frisket", "platform_metered"): (
        "Media is handled by an execution venue of class 'frisket_shared'."
    ),
    ("frisket_dedicated_org", "frisket", "org_key"): (
        "Media is handled by an execution venue of class 'frisket_dedicated_org'."
    ),
}

COST_POSTURE_GOLDENS = {
    "operator_borne": "billed to your own account; no platform charge",
    "platform_metered": "metered by the platform charge authority",
    "org_key": "billed to your organization's provider key",
}


def test_trust_label_goldens():
    for (egress, operator, posture), expected in TRUST_LABEL_GOLDENS.items():
        assert trust_label(egress, operator, posture) == expected, (
            egress,
            operator,
            posture,
        )


def test_cost_posture_goldens():
    for posture, expected in COST_POSTURE_GOLDENS.items():
        assert cost_posture_label(posture) == expected


def test_injected_commercial_presentation_wins_over_neutral_route_copy():
    presentation = CommercialPresentation(
        venue_label="Acme-operated shared infrastructure",
        billing_label="metered and billed through Acme credits",
    )
    assert (
        trust_label(
            "frisket_shared",
            "frisket",
            "platform_metered",
            presentation,
        )
        == "Media is processed on Acme-operated shared infrastructure."
    )
    assert (
        cost_posture_label("platform_metered", presentation)
        == "metered and billed through Acme credits"
    )

    promise = Promise.make(
        "egress_class",
        "satisfies_order",
        "frisket_shared",
        order_ref=EGRESS_ORDER_REF,
        audience="user_claim",
    )
    assert claim_display(
        promise,
        _facts("frisket_shared", "frisket", "platform_metered"),
        presentation,
    ) == (
        "Media is processed on Acme-operated shared infrastructure. "
        "(metered and billed through Acme credits)"
    )


def _facts(egress: str, operator: str, posture: str) -> RouteRowFacts:
    return RouteRowFacts(
        target_id="local",
        engine="faster_whisper",
        operator=operator,
        egress_class=egress,
        region=None,
        credential_source="local",
        cost_posture=posture,
    )


def test_egress_claim_display_golden():
    promise = Promise.make(
        "egress_class",
        "satisfies_order",
        "operator_lan",
        order_ref=EGRESS_ORDER_REF,
        audience="user_claim",
    )
    assert claim_display(promise, _facts("operator_lan", "self", "operator_borne")) == (
        "Media leaves this machine for your own infrastructure "
        "(LAN service you operate). "
        "(billed to your own account; no platform charge)"
    )


def test_free_public_egress_claim_does_not_invent_billing_copy():
    promise = Promise.make(
        "egress_class",
        "satisfies_order",
        "third_party_api",
        order_ref=EGRESS_ORDER_REF,
        audience="user_claim",
    )

    [claim] = gate_claims_payload(
        [promise],
        _facts("third_party_api", "nominatim", "platform_metered"),
        has_cost_claim=False,
    )

    assert claim == {
        "field": "egress_class",
        "display": "Media is sent to nominatim, a third-party API.",
    }
    assert "metered" not in claim["display"]
    assert "billed" not in claim["display"]


def test_cost_le_claim_display_golden():
    promise = Promise.make(
        "cost",
        "le",
        "1.50",
        basis={
            "pricing_key": "test.acme.audio_second",
            "unit_rate": "0.02",
            "estimated_quantity": "75",
            "quantity_unit": "audio_second",
            "ceiling_mode": "none",
        },
        audience="user_claim",
    )
    assert claim_display(
        promise, _facts("third_party_api", "acme-ai", "operator_borne")
    ) == (
        "Estimated provider cost $1.50; this pre-run estimate is not a spending cap "
        "(billed to your own account; no platform charge)."
    )


def test_cost_le_claim_display_without_known_ceiling_is_variable_estimate():
    for basis in ({}, {"ceiling_mode": None}, {"ceiling_mode": "future_mode"}):
        promise = Promise.make("cost", "le", "1.50", basis=basis, audience="user_claim")
        display = claim_display(
            promise, _facts("third_party_api", "acme-ai", "operator_borne")
        )
        assert display == (
            "Estimated provider cost $1.50; this pre-run estimate is not a spending cap "
            "(billed to your own account; no platform charge)."
        )
        assert "up to" not in display
        assert "maximum" not in display.lower()


def test_cost_le_claim_display_with_consented_quantity_names_a_true_maximum():
    promise = Promise.make(
        "cost",
        "le",
        "1.50",
        basis={"ceiling_mode": "consented_quantity"},
        audience="user_claim",
    )
    display = claim_display(
        promise, _facts("third_party_api", "acme-ai", "operator_borne")
    )
    assert (
        display
        == "Maximum cost: $1.50 (billed to your own account; no platform charge)."
    )


def test_cost_unbounded_claim_display_golden():
    promise = Promise.make(
        "cost",
        "unbounded",
        None,
        audience="user_claim",
    )
    assert claim_display(
        promise, _facts("third_party_api", "openai", "operator_borne")
    ) == (
        "Cost cannot be estimated for this run "
        "(billed to your own account; no platform charge)."
    )


def test_unknown_future_claim_renders_honestly():
    promise = Promise.make("region", "eq", "eu-west", audience="system_promise")
    assert (
        claim_display(promise, _facts("none", "self", "operator_borne"))
        == "This run claims region = 'eu-west'."
    )


def test_trust_label_open_family_fallback():
    assert trust_label("future_class", "self", "operator_borne") == (
        "Media is handled by an execution venue of class 'future_class'."
    )
    assert cost_posture_label("future_posture") == "cost posture 'future_posture'"
