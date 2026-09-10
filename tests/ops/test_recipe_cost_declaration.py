"""``Recipe.cost_class`` is a REQUIRED declaration, and it is what the cost
gate reads when a recipe produced no estimate.

The defect these pin: ``estimate_run`` turned every ``Recipe.estimate() ->
None`` into ``{"cost": 0.0}``, so absence and zero shared one representation.
An unestimated recipe priced at $0.00 — under every threshold — and the
``est["cost"] is None`` branch of the gate was unreachable on both the fresh
launch and the run.backfill path. A run that spent real money launched without
a word.

The naive repair (absence always refuses) is a trap of its own: ``map.api_call``,
``media.fetch_url`` and ``media.ytdlp_download`` can NEVER produce an estimate,
because an author-supplied URL has no published price. Refusing them makes them
permanently unrunnable. So the fix is a declaration, not a rule: each recipe
says what absence MEANS for it, and an op whose answer is "unpriceable" is
wired for an explicit unknown-cost consent instead of being priced at zero.

Three things are pinned here:

1. **Closure.** Every published row-action example binds a program that
   declares ``cost_class``, as does any remaining SDK runner.
2. **Agreement.** Each bound program's declaration matches the cost policy its
   action publishes, and the host catalog preserves that policy,
   so the estimator and the catalog cannot answer differently.
3. **The effect site.** Only a declared-free recipe gets a number from an
   absent estimate; metered and unpriceable get ``cost: None``, which gates.
The fourth half of the fix — that an unpriceable op is still RUNNABLE, i.e.
that the gate routes to a consent the schema accepts rather than bricking the
action — lives with the other confirmation-wiring invariants in
tests/engine/test_cost_policy_confirmation_invariant.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.core import MapRows, ModelRows
from frisket.actions.system import typed_action_for_request
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.contracts.action import ActionError
from frisket.engine.executor import resolve_map_preview
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
from frisket.engine.runner import validation
from frisket.execution.pricing_policy import default_pricing_policy
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.engine.runner.confirmation_context import ConsentQuoteRefused
from frisket.engine.store import Project
from frisket.ops.base import Recipe
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec

# The catalog vocabulary each cost class is allowed to publish. "metered" and
# "unpriceable" both gate; they differ in the REMEDY the catalog documents (a
# readable meter you could price later vs. a price that will never exist), and
# that difference is exactly what this table holds still.
_CATALOG_KINDS: dict[str, set[str]] = {
    "free": {"none"},
    "metered": {"model_metered", "external_metered"},
    "unpriceable": {"unknown"},
}


def _registry() -> dict[str, Recipe]:
    registry = {}
    registry.update({name: plan.program for name, plan in _typed_plans()})
    return registry


def _typed_plans():
    for action in ACTION_REGISTRY.actions:
        if not isinstance(action.definition.run, (MapRows, ModelRows)):
            continue
        for index, example in enumerate(action.catalog_entry()["examples"]):
            plan = _typed_map_rows_plan(typed_action_for_request(example))
            yield f"{action.action_id}@{index}", plan


def _declared_cost_class(recipe: Recipe) -> str | None:
    return getattr(recipe, "cost_class", None)


# ---------------------------------------------------------------------------
# 1. Closure: the declaration is required of everything registered
# ---------------------------------------------------------------------------


def test_every_registered_recipe_declares_a_cost_class() -> None:
    registry = _registry()
    assert registry
    assert {"map.classify@0", "map.ner@0", "map.translate@0"} <= registry.keys()

    undeclared = sorted(
        name
        for name, recipe in registry.items()
        if _declared_cost_class(recipe) is None
    )
    assert undeclared == [], (
        f"these registered recipes never declared cost_class: {undeclared}. "
        "Add ``cost_class = 'free' | 'metered' | 'unpriceable'`` to the class "
        "(see frisket.ops.base.Recipe.cost_class). Undeclared used to mean "
        "$0.00 and no gate."
    )
    for name, recipe in sorted(registry.items()):
        assert _declared_cost_class(recipe) in _CATALOG_KINDS, name


def test_base_recipe_declares_nothing_to_inherit() -> None:
    # The annotation carries no value, so nothing inherits a cost answer by
    # accident — an omission is an AttributeError at the first estimate.
    assert "cost_class" not in vars(Recipe)
    assert "cost_class" in Recipe.__annotations__


def test_undeclared_recipe_raises_instead_of_pricing_itself_free() -> None:
    class _Undeclared(Recipe):
        consumes_resolution = False
        name: str = "undeclared_cost"

    with pytest.raises(AttributeError, match="cost_class"):
        validation.unestimated_cost(_Undeclared(), {})


# ---------------------------------------------------------------------------
# 2. Agreement: recipe declaration <-> op declaration <-> live catalog
# ---------------------------------------------------------------------------


def test_recipe_declaration_agrees_with_the_action_catalog() -> None:
    mismatched: list[str] = []
    for kind, plan in _typed_plans():
        cost_class = _declared_cost_class(plan.program)
        assert cost_class is not None, kind
        cost_kind = plan.action.catalog_entry()["cost_policy"]["kind"]
        if cost_kind not in _CATALOG_KINDS[cost_class]:
            mismatched.append(f"{kind}: program={cost_class} catalog={cost_kind}")
    assert mismatched == [], (
        "the estimator and the catalog disagree about what these actions cost: "
        f"{mismatched}"
    )


def test_op_declaration_agrees_with_the_live_catalog_entry() -> None:
    """Host placement and catalog projection preserve each typed cost policy."""
    from frisket.actions.system import root_action_catalog

    served_catalog = {entry.kind: entry for entry in root_action_catalog().actions}
    mismatched: list[str] = []
    for action in ACTION_REGISTRY.actions:
        kind = action.action_id
        declared = action.catalog_entry()["cost_policy"]
        served = served_catalog[kind].cost_policy
        if (served.kind, served.requires_confirmation) != (
            declared["kind"],
            declared["requires_confirmation"],
        ):
            mismatched.append(
                f"{kind}: declaration={declared['kind']}/"
                f"{declared['requires_confirmation']} served={served.kind}/"
                f"{served.requires_confirmation}"
            )
    assert mismatched == [], mismatched


# ---------------------------------------------------------------------------
# 3. The effect site: what an ABSENT estimate costs
# ---------------------------------------------------------------------------


def test_absent_estimate_is_free_only_when_the_recipe_declared_free(
    text_project,
) -> None:
    project, sheet_id = text_project
    plan = _free_plan(project, sheet_id)
    free = validation.unestimated_cost(plan.program, plan.runner_spec)
    assert free["cost"] == 0.0
    assert free["cost_source"] == "free_local"
    assert not free.get("requires_confirmation")

    api = resolve_map_preview(
        project,
        {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"request": {"url": "https://example.com/{{text}}"}},
            "idempotency_key": "api-unknown-cost",
        },
    )
    assert not isinstance(api, ActionError), api
    unknown = validation.unestimated_cost(api.program, api.runner_spec)
    assert unknown["cost"] is None
    assert unknown["requires_confirmation"] is True
    assert unknown["cost_source"] == "unknown"

    fetch = _fetch_plan(project, sheet_id)
    unknown = validation.unestimated_cost(fetch.program, fetch.spec_dict())
    assert unknown["cost"] is None
    assert unknown["requires_confirmation"] is True
    media = _fetch_plan(project, sheet_id, action_id="media.ytdlp_download")
    unknown = validation.unestimated_cost(media.program, media.spec_dict())
    assert unknown["cost"] is None
    assert unknown["requires_confirmation"] is True


def test_local_ner_stays_free_while_the_llm_engine_is_metered() -> None:
    """The engine axis, which a static per-recipe declaration could not express:
    map.ner's catalog policy is model_metered because ONE of its engines is, but
    a spaCy run spends nothing and must launch with no dialog."""
    from typed_model_fixtures import model_plan

    for engine in ("spacy", "gliner", "llm"):
        plan = model_plan(
            {
                "action_kind": "map.ner",
                "engine": engine,
                "labels": ["person"],
                **({"model": "anthropic/claude-haiku-4-5"} if engine == "llm" else {}),
            }
        )
        assert plan.program.cost_class_for(plan.spec_dict()) == (
            "metered" if engine == "llm" else "free"
        )
        if engine != "llm":
            assert (
                validation.unestimated_cost(plan.program, plan.spec_dict())["cost"]
                == 0.0
            )


@pytest.fixture()
def text_project(tmp_path: Path):
    project = Project.create(tmp_path / "p.frisket")
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "text", type="text")
    project.add_rows(sheet_id, [{"text": "https://example.com/a"}], {"text": col})
    try:
        yield project, sheet_id
    finally:
        project.close()


def _fetch_plan(project: Project, sheet_id: int, *, action_id="media.fetch_url"):
    request = ActionRequest(
        action_id=action_id,
        scope={"kind": "sheet_rows", "sheet_id": sheet_id},
        params={"source": "text"},
        idempotency_key="fetch-cost-gate",
    )
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    return build_typed_map_rows_plan(project, bound)


def _free_plan(project: Project, sheet_id: int):
    plan = resolve_map_preview(
        project,
        v1_action_from_canonical_run_spec(
            queued_python_run_spec(sheet_id, "text", "matched")
        ),
    )
    assert not isinstance(plan, ActionError), plan
    return plan


def _validate(
    project, spec: dict, *, confirmed: bool, resume_run_id=None, program=None
):
    # ``pricing_policy`` is required and keyword-only at this seam: a 402 whose
    # figure no policy rated is the defect the pricing-policy port exists to
    # prevent, so omitting it is a TypeError here rather than an unrated quote
    # reaching a user.
    router = ModelRouter()
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    run_store = validation.RunResultStore(project)
    try:
        return validation.validate_spec(
            project,
            router,
            run_store,
            spec,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            pricing_policy=default_pricing_policy(),
            composition=composition,
            program=program,
        )
    except validation.CostGate as exc:
        if not confirmed or exc.promise_set_hash is None:
            raise
        # Tests that request a confirmed admission still use the same exact
        # challenge/echo protocol as a real caller.  The boolean alone remains
        # insufficient; the server-authored scope/quote hash is load-bearing.
        retry_spec = {
            **spec,
            "consented_promise_set_hash": exc.promise_set_hash,
        }
        return validation.validate_spec(
            project,
            router,
            run_store,
            retry_spec,
            confirmed=True,
            resume_run_id=resume_run_id,
            pricing_policy=default_pricing_policy(),
            composition=composition,
            program=program,
        )


def test_unpriceable_run_gates_and_a_confirm_admits_it(text_project) -> None:
    project, sheet_id = text_project
    plan = _fetch_plan(project, sheet_id)
    with pytest.raises(validation.CostGate) as exc:
        _validate(project, plan.spec_dict(), program=plan.program, confirmed=False)
    assert exc.value.estimate is None
    assert "unknown" in str(exc.value)

    validated = _validate(
        project, plan.spec_dict(), program=plan.program, confirmed=True
    )
    assert validated.est["cost"] is None
    assert validated.est["requires_confirmation"] is True


def test_free_run_never_asks(text_project) -> None:
    project, sheet_id = text_project
    plan = _free_plan(project, sheet_id)
    validated = _validate(
        project, plan.runner_spec, program=plan.program, confirmed=False
    )
    assert validated.est["cost"] == 0.0
    assert validated.est["cost_source"] == "free_local"


def test_the_backfill_path_gates_on_the_same_absence(text_project) -> None:
    """The second gate (run.backfill extends a run to newly visible rows). It
    reads the SAME estimate, so the manufactured zero disarmed it too — an
    earlier repair plan missed this one entirely."""
    from tests.execution_composition_helpers import open_attempt_authority
    from frisket.engine.runner import MapRunner

    project, sheet_id = text_project
    plan = _fetch_plan(project, sheet_id)
    spec = plan.spec_dict()
    runner = MapRunner(
        project, ModelRouter(), authority=open_attempt_authority(project)
    )
    # Exercise the host-bound typed program's prepare/resume cost gate directly.
    runner.allow_action_lifecycle_only_recipes = True
    with pytest.raises(validation.CostGate) as challenge:
        runner.prepare_run(spec, program=plan.program, confirmed=True)
    assert challenge.value.promise_set_hash is not None
    confirmed_spec = {
        **spec,
        "consented_promise_set_hash": challenge.value.promise_set_hash,
    }
    run_id = runner.prepare_run(
        confirmed_spec, program=plan.program, confirmed=True
    ).run_id
    backfill = {**spec, "row_ids": validation.target_rows(project, spec)}
    with pytest.raises(validation.CostGate) as exc:
        _validate(
            project,
            backfill,
            program=plan.program,
            confirmed=False,
            resume_run_id=run_id,
        )
    assert exc.value.estimate is None
    # ...and the same backfill, confirmed, is admitted.
    _validate(
        project,
        backfill,
        program=plan.program,
        confirmed=True,
        resume_run_id=run_id,
    )


# ---------------------------------------------------------------------------
# 4. The consent-quote projection is scoped to runs that HAVE a quote
#
# ``confirmation_context_hash`` refuses an estimate carrying a key nobody has
# classified as money or display — the fence that stops a new ``surcharge``
# key from leaving a $1.00 consent echo-valid for a $5.00 dispatch. That
# refusal must reach only runs being gated. A free run asks nobody anything,
# has no quote, and mints no consent; refusing it would turn a strictness
# rule about approvals into a launch failure for work that was never
# approved in the first place. ``register_recipe`` is an open extension
# point, so this is also what keeps an out-of-tree recipe's unfamiliar
# estimate key from bricking its free runs.


def test_a_free_ungated_run_launches_with_an_unclassified_estimate_key(
    text_project, monkeypatch
) -> None:
    project, sheet_id = text_project
    plan = _free_plan(project, sheet_id)
    spec, recipe = plan.runner_spec, plan.program
    assert recipe.cost_class_for(spec) == "free"

    monkeypatch.setattr(
        type(recipe),
        "estimate",
        lambda self, project, spec, rows: {
            "cost": 0.0,
            "cost_source": "free_local",
            "throughput_hint_rows_per_second": 12.5,
        },
        raising=False,
    )

    # No CostGate, no ConsentQuoteRefused: the run is admitted.
    validated = _validate(project, spec, program=recipe, confirmed=False)
    assert validated.unrouted_confirmation_hash is None

    # ...and the same key on a GATED run is still refused, so this is a
    # narrowing of the fence, not a removal of it.
    metered_plan = _fetch_plan(project, sheet_id)
    metered, metered_recipe = metered_plan.spec_dict(), metered_plan.program
    monkeypatch.setattr(
        type(metered_recipe),
        "estimate",
        lambda self, project, spec, rows: {
            "cost": None,
            "cost_source": "unknown",
            "requires_confirmation": True,
            "throughput_hint_rows_per_second": 12.5,
        },
        raising=False,
    )
    with pytest.raises(ConsentQuoteRefused) as refused:
        _validate(project, metered, program=metered_recipe, confirmed=False)
    assert refused.value.key == "throughput_hint_rows_per_second"
