"""The ESTIMATE-surface ``cost_source`` vocabulary is closed, and checked.

The defect these pin: ``frisket.ops.cost_source.CostSource`` was a ``Literal``
with ZERO importers whose docstring promised "a typo in a newly-added op's cost
gate is a Literal type-check failure". No such check existed — this repo runs
no static type checker, only ruff and the ``scripts/ci/lint_*`` gates — and by
the time anyone looked, live producers had drifted values past the list
(``external_metered_declared``, ``estimated``, ``hosted_char_count``). A
promised check that never runs is
worse than none: it makes a reader stop looking.

So the vocabulary is now enforced where estimates are BUILT
(``cost_estimate`` refuses an unlisted value at runtime), and the producers
that do not route through that constructor are pinned here instead of trusted.

Kept deliberately separate from the per-row FACT vocabulary that shares the
``cost_source`` KEY: ``provider_reported`` and ``cache_hit`` describe a call
that already happened and can never be part of a pre-run quote. Same name,
different meaning (CLAUDE.md recurring pattern 3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.runner.validation import unestimated_cost
from frisket.ops.base import Recipe
from frisket.ops.cost_source import (
    ESTIMATE_COST_SOURCES,
    cost_estimate,
    estimate_from_cost_basis,
    free_local_estimate,
    free_public_api_estimate,
    invalid_engine_estimate,
    unknown_cost_estimate,
)

# The per-row fact vocabulary. Listed so the separation is asserted, not
# merely described: a fact value leaking into the estimate list fails here.
FACT_ONLY_COST_SOURCES = frozenset({"provider_reported", "cache_hit"})


def test_estimate_and_fact_vocabularies_do_not_overlap() -> None:
    assert not (ESTIMATE_COST_SOURCES & FACT_ONLY_COST_SOURCES)


def test_cost_estimate_refuses_a_value_outside_the_vocabulary() -> None:
    """The fence itself. Disabling it must make this red (CLAUDE.md pattern 4)."""
    with pytest.raises(ValueError) as excinfo:
        cost_estimate(cost=0.0, cost_source="provider_reported")  # type: ignore[arg-type]
    assert "provider_reported" in str(excinfo.value)
    assert "estimate vocabulary" in str(excinfo.value)


@pytest.mark.parametrize(
    "malformed",
    [None, 42, ["free_local"], {"cost_source": "free_local"}, {"free_local"}],
)
def test_cost_estimate_refusal_is_one_kind_for_every_malformed_value(
    malformed,
) -> None:
    """ValueError, never TypeError — including for the unhashable ones.

    A bare ``in`` against the frozenset raises TypeError for a list or a dict,
    and the estimate endpoint catches only ``(KeyError, ValueError)``
    (``server/services/action_previews.py`` ``ActionPreviewService.estimate``).
    A producer that handed a list where a discriminator belongs would have
    become a 500 where a 400 belongs, and the 500 says nothing about which
    knob is wrong.
    """
    with pytest.raises(ValueError) as excinfo:
        cost_estimate(cost=0.0, cost_source=malformed)  # type: ignore[arg-type]
    assert "cost_source" in str(excinfo.value)


@pytest.mark.parametrize(
    "builder, expected",
    [
        (free_local_estimate, "free_local"),
        (free_public_api_estimate, "free_public_api"),
        (unknown_cost_estimate, "unknown"),
        (invalid_engine_estimate, "invalid_engine"),
    ],
)
def test_named_builders_emit_a_listed_value(builder, expected: str) -> None:
    assert builder("some-engine")["cost_source"] == expected
    assert expected in ESTIMATE_COST_SOURCES


def test_engine_is_omitted_rather_than_invented_when_absent() -> None:
    """``join.semantic`` has no engine axis. An empty-string engine would bind
    into the consent hash as a real fact."""
    assert "engine" not in free_local_estimate(external_api=False)
    assert free_local_estimate(external_api=False) == {
        "cost": 0.0,
        "cost_source": "free_local",
        "external_api": False,
    }


def test_cost_basis_projector_emits_only_the_canonical_preview_core() -> None:
    from types import SimpleNamespace

    from frisket.execution.promise_compiler import (
        OperatorBorneZeroCost,
        PricedCostBasis,
        UnpriceableCost,
    )

    def resolved(basis):
        return SimpleNamespace(
            cost_basis=basis,
            resolution=SimpleNamespace(facts=SimpleNamespace(engine="engine")),
        )

    priced = PricedCostBasis(
        pricing_key="provider.capability.unit",
        unit_rate="0.0000004",
        estimated_quantity="2",
        quantity_unit="unit",
        terms_version=None,
        quantity_rounding_mode="exact",
        quantity_rounding_decimal_places=None,
        meter_key="units",
        meter_units_per_quantity_unit="1",
        ceiling_mode="none",
        row_settlement_mode="all_metered",
        charge_authority="provider_direct",
    )
    assert estimate_from_cost_basis(resolved(priced)) == {
        "cost": 0.0000008,
        "engine": "engine",
        "cost_source": "pricing_data",
        "pricing_key": "provider.capability.unit",
    }
    assert estimate_from_cost_basis(
        resolved(OperatorBorneZeroCost()), zero_cost_source="free_public_api"
    ) == {
        "cost": 0.0,
        "engine": "engine",
        "cost_source": "free_public_api",
    }
    assert estimate_from_cost_basis(
        resolved(UnpriceableCost()), warning="cannot price"
    ) == {
        "cost": None,
        "engine": "engine",
        "cost_source": "unknown",
        "warning": "cannot price",
    }


class _FreeRecipe(Recipe):
    cost_class = "free"
    consumes_resolution = False
    name: str = "free-probe"


class _MeteredRecipe(Recipe):
    cost_class = "metered"
    consumes_resolution = False
    name: str = "metered-probe"


def test_estimate_run_llm_branches_stay_in_vocabulary(tmp_path: Path) -> None:
    """``estimate_run``'s two LLM branches — the last two estimate producers
    covered by neither ``cost_estimate``'s runtime check nor the pins above.

    An unpriced model cannot be quoted, so it is ``unknown`` and gates. A
    priced model is costed from token counts sampled over the first rows, but
    the rate still comes from ``pricing_data`` and carries its catalog key.
    ``avg_input_tokens`` represents the approximate quantity separately.
    """
    from frisket.engine.store import Project
    from typed_model_fixtures import estimate_model_spec

    project = Project.create(tmp_path / "llm-estimate.frisket", name="llm estimate")
    try:
        sheet_id = project.add_sheet("Docs")
        columns = {"text": project.add_column(sheet_id, "text", type="text")}
        project.add_rows(sheet_id, [{"text": "alpha beta"}], columns)
        # A typed map.classify request bound through the real plan builder;
        # ``estimate_run`` receives that plan's program exactly as the runner
        # facade passes it.
        spec = {
            "action_kind": "map.classify",
            "sheet_id": sheet_id,
            "input_columns": ["text"],
            "context": "what is this about?",
            "fields": [{"name": "topic", "type": "text"}],
        }

        unpriced = estimate_model_spec(project, {**spec, "model": "unknown/frontier"})
        assert unpriced["cost"] is None
        assert unpriced["cost_source"] == "unknown"
        assert "pricing_key" not in unpriced

        priced = estimate_model_spec(
            project, {**spec, "model": "anthropic/claude-haiku-4-5"}
        )
        assert priced["cost"] is not None
        assert priced["cost_source"] == "pricing_data"
        assert priced["pricing_key"] == "anthropic/claude-haiku-4-5.tokens"

        openrouter_base = estimate_model_spec(
            project, {**spec, "model": "openrouter/qwen/qwen3-8b"}
        )
        assert openrouter_base["cost"] is not None
        assert openrouter_base["cost_source"] == "pricing_data"
        assert openrouter_base["pricing_key"] == "openrouter/qwen/qwen3-8b.tokens"

        openrouter_variant = estimate_model_spec(
            project, {**spec, "model": "openrouter/qwen/qwen3-8b:free"}
        )
        assert openrouter_variant["cost"] is None
        assert openrouter_variant["cost_source"] == "unknown"
        assert "pricing_key" not in openrouter_variant

        assert {
            unpriced["cost_source"],
            priced["cost_source"],
            openrouter_base["cost_source"],
            openrouter_variant["cost_source"],
        } <= ESTIMATE_COST_SOURCES

        local = estimate_model_spec(project, {**spec, "model": "ollama/@desk/unlisted"})
        assert local["cost"] == 0.0
        assert local["cost_source"] == "free_local"
        assert "pricing_key" not in local
    finally:
        project.close()


def test_unestimated_cost_stays_in_vocabulary() -> None:
    """What a recipe that produced NO estimate costs — the runner's own
    estimate producer, and the reason an absent estimate still has a
    discriminator."""
    free = unestimated_cost(_FreeRecipe(name="free-probe"), {})
    assert free["cost_source"] == "free_local"
    metered = unestimated_cost(_MeteredRecipe(name="metered-probe"), {})
    assert metered["cost_source"] == "unknown"
    assert {free["cost_source"], metered["cost_source"]} <= ESTIMATE_COST_SOURCES
