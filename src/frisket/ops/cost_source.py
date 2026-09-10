"""Shared ``cost_source`` vocabulary and constructors for the per-op
``estimate()`` pre-run cost gate.

Scope: the ESTIMATE surface only — the dicts ``Recipe.estimate()`` returns,
which ``engine/runner/confirmation_context.py`` projects through
``ConsentQuote`` into the hash a user's approval is compared by. The per-row FACT surface (the
``(data, meta)`` cost annotation and ``model_calls`` rows) shares the
``cost_source`` KEY but is a different vocabulary — ``provider_reported``
and ``cache_hit`` are facts about a call that already happened and can never
appear in a pre-run quote. Same name, different meaning; keep them apart.

``sdk/ops/ocr.py``, ``sdk/ops/transcribe_engines.py``, and ``sdk/ops/translate.py``'s
``TranslateRecipe`` each independently hand-rolled the same small string
vocabulary: a genuinely-free local/sidecar engine (``"free_local"``), a
billable engine whose exact pre-run cost can't be known yet
(``"unknown"``), and an invalid/unrecognized engine name that should fail
per-row rather than block the whole run (``"invalid_engine"``).

``cost_estimate`` below is the one place an estimate dict is built, and it
CHECKS ``cost_source`` against ``ESTIMATE_COST_SOURCES`` at runtime. The
check is a real one: this repo runs no static type checker (CI is ruff plus
the four ``scripts/ci/lint_*`` gates), so the ``Literal`` alone would be
decoration — which is exactly how the vocabulary silently drifted five
values out of date before. ``tests/ops/test_cost_source_vocabulary.py``
closes the loop from the other side, going red when a producer emits a
value this module has not classified.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

CostSource = Literal[
    # No meter at all: the operator already owns the compute (local model,
    # sidecar, their own gateway, pure in-process arithmetic).
    "free_local",
    # A real external request to a public API that bills nobody.  This is not
    # ``free_local`` (data still leaves the operator's infrastructure) and not
    # ``unknown`` (the provider spend is known to be zero).
    "free_public_api",
    # Billable, but the pre-run figure is genuinely unknowable — never a
    # fabricated number. Forces the explicit confirm.
    "unknown",
    # An unrecognized symbolic engine name: a validation/runtime error, not
    # an unknown billable model.
    "invalid_engine",
    # A real number, computed from a rate this run is pinned to.
    "pricing_data",
    # A real number sampled/approximated from the input (token counts), not
    # a quoted rate.
    "estimated",
    # No dollar figure at all; the quantity that gates is source characters
    # about to egress to a hosted translator.
    "hosted_char_count",
    # A plugin declared the action externally metered without supplying a
    # number, so the plugin's own declaration is the discriminator.
    "external_metered_declared",
]

ESTIMATE_COST_SOURCES: frozenset[str] = frozenset(get_args(CostSource))


def cost_estimate(
    *,
    cost: float | None,
    cost_source: CostSource,
    engine: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one estimate dict, with its discriminator checked.

    ``engine`` is omitted from the result when it is ``None`` — a recipe
    with no engine axis (``enrich.to_geo_point``, ``join.semantic``) still
    gets a truthful ``cost_source``
    without inventing an engine name for the consent hash to bind.
    """
    # ``isinstance`` FIRST, and the refusal is one kind: a bare membership
    # test raises TypeError on an unhashable value (a producer that handed a
    # list or a dict where a discriminator belongs), and the estimate endpoint
    # catches only KeyError/ValueError (server/services/action_previews.py
    # `estimate`) — so a malformed producer would 500 where it should 400.
    if not isinstance(cost_source, str) or cost_source not in ESTIMATE_COST_SOURCES:
        raise ValueError(
            f"cost_source {cost_source!r} is not in the estimate vocabulary; "
            "add it to frisket.ops.cost_source.CostSource with the reason, or "
            "use an existing value (per-row FACT values like "
            "'provider_reported' are a different vocabulary and do not belong "
            "on a pre-run estimate)"
        )
    out: dict[str, Any] = {"cost": cost}
    if engine is not None:
        out["engine"] = engine
    out["cost_source"] = cost_source
    out.update(extra)
    return out


def free_local_estimate(engine: str | None = None, **extra: Any) -> dict[str, Any]:
    """A genuinely free local/sidecar engine: the operator already owns the
    compute, so ``0.0`` is a fact, not a fabricated number."""
    return cost_estimate(cost=0.0, cost_source="free_local", engine=engine, **extra)


def free_public_api_estimate(engine: str | None = None, **extra: Any) -> dict[str, Any]:
    """A public external API with known-zero provider spend.

    ``external_api`` is part of the result so a zero-cost line can never be
    mistaken for local execution or suppress the independent egress consent.
    """

    return cost_estimate(
        cost=0.0,
        cost_source="free_public_api",
        engine=engine,
        external_api=True,
        **extra,
    )


def invalid_engine_estimate(engine: str | None = None, **extra: Any) -> dict[str, Any]:
    """An unrecognized symbolic engine name: a validation/runtime error, not
    an unknown billable model — let execution fail the affected rows instead
    of blocking the whole run behind the cost gate."""
    return cost_estimate(cost=0.0, cost_source="invalid_engine", engine=engine, **extra)


def unknown_cost_estimate(engine: str | None = None, **extra: Any) -> dict[str, Any]:
    """A billable engine whose exact pre-run cost is unknowable (forces the
    explicit run-path confirm; never a fabricated number, per the
    no-fabricated-costs rule)."""
    return cost_estimate(cost=None, cost_source="unknown", engine=engine, **extra)


def estimate_from_cost_basis(
    resolution: Any,
    *,
    zero_cost_source: Literal["free_local", "free_public_api"] = "free_local",
    warning: str | None = None,
    **presentation: Any,
) -> dict[str, Any]:
    """Project one resolved ``CostBasis`` into the canonical preview core.

    The complete basis remains the promise/attempt/settlement authority.  A
    preview carries only the provider-cost bound, its source and identity;
    capability-specific rate/quantity aliases would be a second, weaker copy
    of the same money facts.
    """
    from frisket.execution.promise_compiler import (
        OperatorBorneZeroCost,
        PricedCostBasis,
        UnpriceableCost,
    )

    basis = resolution.cost_basis
    engine = resolution.resolution.facts.engine
    if isinstance(basis, OperatorBorneZeroCost):
        return cost_estimate(
            cost=0.0,
            cost_source=zero_cost_source,
            engine=engine,
            **presentation,
        )
    if isinstance(basis, UnpriceableCost):
        extra = dict(presentation)
        if warning is not None:
            extra["warning"] = warning
        return unknown_cost_estimate(engine, **extra)
    if isinstance(basis, PricedCostBasis):
        if basis.bound is None:
            extra = {"pricing_key": basis.pricing_key, **presentation}
            if warning is not None:
                extra["warning"] = warning
            return unknown_cost_estimate(engine, **extra)
        return cost_estimate(
            cost=round(float(basis.bound), 8),
            cost_source="pricing_data",
            engine=engine,
            pricing_key=basis.pricing_key,
            **presentation,
        )
    raise ValueError(f"unsupported resolved cost basis {type(basis).__name__}")
