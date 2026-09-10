"""The MCP needs_confirmation envelope quotes the BILLED figure.

``run_action``'s docstring tells an agent to read ``estimate`` (USD) and
``estimate_known``, and to get a human's approval for that number. So those
two fields are a money gate's rendering, and they answer to the same
three-state selector every other gate compares with
(``confirmation_context.quoted_usd``):

* a policy rated it -> the billed figure, not the provider's cost;
* a policy DECLINED to rate it -> unknown, never a confident number;
* no complete policy verdict -> unknown, never the provider figure.

Before this, the renderer overwrote the billed scalar its caller had already
computed with ``estimate_details["cost"]`` — so under a 2x deployment tariff
the agent was shown, and asked its human to approve, half the price; and when
the deployment's policy refused to price the run at all, the agent was shown
that refused provider figure as a known estimate.
"""

from __future__ import annotations

from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.execution.pricing_policy import (
    QuoteFacts,
    Rating,
    RatedQuote,
    Unpriceable,
    _reset_pricing_policy_for_tests,
    install_pricing_policy,
)
from frisket.server.mcp import LocalBackend
from frisket.server.mcp.backends import cost_gate_response
from test_mcp_server import call, classify_action, seed_workspace


@pytest.fixture(autouse=True)
def _isolate_installed_pricing_policy():
    _reset_pricing_policy_for_tests()
    yield
    _reset_pricing_policy_for_tests()


class _CostPlusPolicy:
    """2x the provider's cost — a deployment tariff, in miniature."""

    policy_id = "test.mcp.cost_plus.v1"

    def rate(self, facts: QuoteFacts) -> Rating:
        if facts.provider_cost is None:
            return Unpriceable(reason="no provider cost", policy_id=self.policy_id)
        return RatedQuote(
            billed_cost=facts.provider_cost * 2,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


PROVIDER_COST_USD = 0.11

_RATED: dict[str, Any] = {
    "rows": 4,
    "cost": PROVIDER_COST_USD,
    "cost_source": "estimated",
    "pricing_key": "openai/gpt-4o-mini.tokens",
    "engine": "openai/gpt-4o-mini",
    "billed_cost": 220_000,
    "policy_id": _CostPlusPolicy.policy_id,
}


def _gate(estimate: Any, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return cost_gate_response(estimate, "confirm this run", details=details)


def test_the_envelope_quotes_the_billed_figure_not_the_provider_cost() -> None:
    """The demonstrated defect: 2x policy, $0.11 provider, $0.22 billed.

    The caller already passes the billed scalar; the renderer used to discard
    it and re-read ``cost`` off the details, so the agent approved $0.11 for a
    run its deployment bills $0.22 for.
    """
    out = _gate(0.22, {"estimate": _RATED})

    assert out["estimate"] == 0.22
    assert out["estimate_known"] is True
    # The provider's number stays on the details, untouched — it is the fact a
    # human approval surface renders beside the price, and a consumer that
    # marked IT up would double-charge.
    assert out["estimate_details"]["cost"] == PROVIDER_COST_USD
    assert out["estimate_details"]["billed_cost"] == 220_000


def test_a_declined_price_is_unknown_never_a_confident_number() -> None:
    """The seam's worst rendering bug: a policy said it cannot price this run,
    and the envelope quoted the provider figure it refused to bill from — with
    ``estimate_known: True`` beside it, which is the field an agent reads to
    decide whether the number means anything."""
    declined = dict(_RATED, billed_cost=None)

    out = _gate(None, {"estimate": declined})

    assert out["estimate"] is None
    assert out["estimate_known"] is False
    assert out["estimate_details"]["cost"] == PROVIDER_COST_USD


def test_an_unrated_envelope_refuses_the_provider_estimate() -> None:
    """The third state: an old producer cannot cross with the old shape.

    Every current 402 producer rates before this renderer. Treating a payload
    with no ``policy_id`` as a billed quote would reintroduce the routed seam:
    a provider figure presented as the deployment's charge with no rate
    authority.
    """
    out = _gate(PROVIDER_COST_USD, {"estimate": {"cost": PROVIDER_COST_USD}})
    assert out["estimate"] is None
    assert out["estimate_known"] is False
    assert out["estimate_details"]["cost"] == PROVIDER_COST_USD

    unknown = _gate(None, {"estimate": {"cost": None}})
    assert unknown["estimate"] is None and unknown["estimate_known"] is False


def test_an_unprojectable_envelope_gates_as_unknown_not_as_an_exception() -> None:
    """A gated run NEVER surfaces as an exception blob (the module contract).

    An envelope this build cannot project — a ``cost`` that is not a number —
    is UNKNOWN, which demands the same explicit confirmation an unpriced run
    always has.
    """
    out = _gate(None, {"estimate": {"cost": "eleven cents"}})
    assert out["estimate"] is None
    assert out["estimate_known"] is False
    assert out["status"] == "needs_confirmation"


def test_a_scalar_estimate_still_passes_through() -> None:
    """Non-vacuity for the other branch: with no estimate mapping at all there
    is nothing to project and the caller's scalar is the answer."""
    out = _gate(1.5)
    assert out["estimate"] == 1.5 and out["estimate_known"] is True
    assert out["estimate_details"] is None


def test_the_local_backends_gate_renders_the_installed_tariff(tmp_path) -> None:
    """End to end through the real tool surface, with a tariff installed.

    The unit cases above pin the renderer; this pins the WIRING — that the
    figure an agent is shown by ``run_action`` comes off the deployment's
    installed policy rather than the provider's price list.
    """
    install_pricing_policy(_CostPlusPolicy())
    sheet = seed_workspace(tmp_path / "ws", ["long text " * 200] * 300)
    action = classify_action(sheet, model="anthropic/claude-opus-4-8")
    backend = LocalBackend(
        tmp_path / "ws",
        router=ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off"),
    )

    out = call(backend, "run_action", {"project_id": "demo", "action": action})

    assert out["status"] == "needs_confirmation"
    details = out["estimate_details"]
    assert details["policy_id"] == _CostPlusPolicy.policy_id
    assert details["billed_cost"] == pytest.approx(
        details["cost"] * 2 * 1_000_000, rel=1e-9
    )
    assert out["estimate"] == details["billed_cost"] / 1_000_000
    assert out["estimate"] > details["cost"]
    assert out["estimate_known"] is True
