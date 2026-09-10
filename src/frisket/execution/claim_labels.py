"""Trust-label display strings for claims-gate rendering.

CostGateModal renders claim lines with goldens-tested copy computed per
``(egress_class, operator, cost_posture)``. This module is the ONE place
that copy lives; ``tests/execution/test_claim_labels.py`` pins every string
as a golden, so a wording change is a reviewed diff, never drift.

Display-only by contract: nothing here enters hashed material (the promise
hash contract excludes display fields by its own field lists), so editing a
label can never change a promise-set hash or invalidate a recorded consent.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from frisket.execution.commercial import CommercialPresentation
from frisket.execution.promises import Promise
from frisket.execution.resolver import RouteRowFacts

# (egress_class, operator-shape) -> where the media goes. ``{operator}`` is
# substituted with the resolved route operator; "self" reads as the
# operator's own machine/infrastructure (the open edition's honest default).
_EGRESS_SELF = {
    "none": "Media stays on this machine.",
    "operator_lan": "Media leaves this machine for your own infrastructure (LAN service you operate).",
    "third_party_api": "Media is sent to a third-party API.",
}
_EGRESS_NAMED = {
    "none": "Media stays on this machine.",
    "operator_lan": "Media leaves this machine for infrastructure operated by {operator} (LAN).",
    "third_party_api": "Media is sent to {operator}, a third-party API.",
}

# cost_posture -> who pays, appended to cost claims (and to egress claims on
# free runs so a gated free sidecar run reads "no platform cost", §5.2).
_COST_POSTURE = {
    "operator_borne": "billed to your own account; no platform charge",
    "platform_metered": "metered by the platform charge authority",
    "org_key": "billed to your organization's provider key",
}


def _plain_decimal(value: Any) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _usd(value: Any) -> str | None:
    """Money rendering: full precision preserved (never rounded away — the
    bound IS the promise), padded to at least cents ($1.50, $0.000164)."""
    text = _plain_decimal(value)
    if text is None:
        return None
    if "." not in text:
        return f"{text}.00"
    decimals = len(text.partition(".")[2])
    return text + "0" * max(0, 2 - decimals)


def trust_label(
    egress_class: str,
    operator: str,
    cost_posture: str,
    presentation: CommercialPresentation | None = None,
) -> str:
    """The one-line trust label for a resolved route: where the media goes,
    per (egress_class, operator, cost_posture)."""
    if presentation is not None:
        venue = presentation.venue_label.rstrip(".")
        return f"Media is processed on {venue}."
    table = _EGRESS_SELF if operator == "self" else _EGRESS_NAMED
    template = table.get(
        egress_class, "Media is handled by an execution venue of class '{egress}'."
    )
    return template.format(operator=operator, egress=egress_class)


def cost_posture_label(
    cost_posture: str,
    presentation: CommercialPresentation | None = None,
) -> str:
    if presentation is not None:
        return presentation.billing_label
    return _COST_POSTURE.get(cost_posture, f"cost posture '{cost_posture}'")


def claim_display(
    promise: Promise,
    facts: RouteRowFacts,
    presentation: CommercialPresentation | None = None,
    *,
    show_billing: bool = True,
) -> str:
    """Render one user_claim promise row as gate copy.

    ``facts`` is the one route-facts type (the compiler-input twin
    ``ResolvedRouteFacts`` was merged into ``RouteRowFacts``), carrying the
    (egress_class, operator, cost_posture) triple the copy keys on.
    """
    operator = facts.operator
    egress = facts.egress_class
    posture = facts.cost_posture
    if promise.field == "egress_class":
        label = trust_label(egress, operator, posture, presentation)
        if show_billing:
            label += f" ({cost_posture_label(posture, presentation)})"
        basis = promise.basis or {}
        work_scope = basis.get("work_scope")
        if isinstance(work_scope, dict):
            row_count = work_scope.get("row_count")
            if isinstance(row_count, int) and not isinstance(row_count, bool):
                noun = "row" if row_count == 1 else "rows"
                label += f" Scope: the current source values for {row_count} {noun}."
        return label
    if promise.field == "cost":
        if promise.op == "unbounded":
            return (
                "Cost cannot be estimated for this run "
                f"({cost_posture_label(posture, presentation)})."
            )
        bound = _usd(promise.value)
        rendered = f"${bound}" if bound is not None else "an unknown amount"
        if (promise.basis or {}).get("ceiling_mode") == "consented_quantity":
            return f"Maximum cost: {rendered} ({cost_posture_label(posture, presentation)})."
        return (
            f"Estimated provider cost {rendered}; this pre-run estimate is not a spending cap "
            f"({cost_posture_label(posture, presentation)})."
        )
    # Open family: an unrecognized future claim still renders honestly.
    return f"This run claims {promise.field} = {promise.value!r}."
