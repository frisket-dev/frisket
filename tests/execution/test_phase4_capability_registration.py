"""Execution capabilities close over actual recipe and typed-program lanes.

Registration, provider facts, route selection and quoted values must agree;
the typed Geocode/Census migration must not silently drop either routed lane.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.execution.provider import (
    CompositionFacts,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolver import Refusal, ResolutionRequest, resolve
from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
)

_PHASE_4 = (
    CAPABILITY_TRANSLATE,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_GEOCODE,
    CAPABILITY_CENSUS,
)
_MODEL_CALL_FACT_METHODS = frozenset({"provider_call", "cache_hit", "local", "sidecar"})


def _capability_keyword(call: ast.Call) -> ast.expr | None:
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == "capability"),
        None,
    )


def _is_model_call_fact_constructor(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "ModelCallMeta") or (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "ModelCallMeta"
        and func.attr in _MODEL_CALL_FACT_METHODS
    )


def _fact_capability_literals(source: str) -> set[str]:
    """Capability literals that can reach a ``ModelCallMeta`` fact.

    A producer may stamp directly or through a same-module helper forwarding
    its ``capability`` parameter. An unrelated keyword is not a fact producer.
    """
    tree = ast.parse(source)
    forwarding_families = {
        function.name
        for function in tree.body
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            _is_model_call_fact_constructor(call)
            and isinstance((capability := _capability_keyword(call)), ast.Name)
            and capability.id == "capability"
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
        )
    }
    return {
        capability.value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and (
            _is_model_call_fact_constructor(call)
            or isinstance(call.func, ast.Name)
            and call.func.id in forwarding_families
        )
        if (
            (capability := _capability_keyword(call)) is not None
            and isinstance(capability, ast.Constant)
            and isinstance(capability.value, str)
        )
    }


def _provider(**env):
    from frisket.execution.definitions import StaticExecutionTargetProvider

    return StaticExecutionTargetProvider(env=env)


def _typed_plan(action_id, *, sheet_id=1, params=None, project=None):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        build_typed_map_rows_plan,
    )

    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(action_id),
        ActionRequest(
            action_id=action_id,
            scope=SheetRows(sheet_id=sheet_id),
            params=params or {"source": "body"},
            idempotency_key="phase4-binding",
        ),
    )
    return (
        _typed_map_rows_plan(bound)
        if project is None
        else build_typed_map_rows_plan(project, bound)
    )


def _actual_programs():
    from frisket.actions.core import routed_capability
    from frisket.actions.registry import ACTION_REGISTRY

    programs = {}
    for registered in ACTION_REGISTRY.actions:
        if routed_capability(registered.definition.run) is None:
            continue
        examples = registered.catalog_entry()["examples"]
        assert examples, f"bind a real Params example for {registered.action_id}"
        programs[registered.action_id] = _typed_plan(
            registered.action_id, params=examples[0]["params"]
        ).program
    return programs


def test_exactly_the_landed_lanes_route_among_phase_4_capabilities():
    """The execution seam and recipe registry agree on the routed lanes."""
    registry = _actual_programs()
    declared = {
        name: recipe.execution_capability
        for name, recipe in registry.items()
        if recipe.execution_capability is not None
    }
    assert declared == {
        "media.ocr": CAPABILITY_OCR,
        "media.transcribe": CAPABILITY_TRANSCRIBE,
        "media.to_markdown": CAPABILITY_TO_MARKDOWN,
        "enrich.geocode": CAPABILITY_GEOCODE,
        "enrich.census_demographics": CAPABILITY_CENSUS,
    }, declared

    consuming = sorted(n for n, r in registry.items() if r.consumes_resolution)
    assert consuming == [
        "enrich.census_demographics",
        "enrich.geocode",
        "media.ocr",
        "media.to_markdown",
        "media.transcribe",
    ], consuming

    assert set(declared.values()) & set(_PHASE_4) == {
        CAPABILITY_TO_MARKDOWN,
        CAPABILITY_GEOCODE,
        CAPABILITY_CENSUS,
    }


def test_the_capability_token_is_the_one_the_fact_column_already_carries():
    """Each producer must stamp the canonical token used by fact settlement."""
    from frisket.execution.targets import EXECUTION_CAPABILITIES

    #: These facts share the column but do not route through this seam.
    non_seam = {"llm.complete", "llm.embed", "web_search"}

    #: Capability -> every producer that must stamp it.
    producers = {
        CAPABILITY_TRANSLATE: {
            "ops/integrations/translation_engine.py",
            "ops/integrations/translate_common.py",
        },
        CAPABILITY_TO_MARKDOWN: {
            "ops/integrations/datalab.py",
        },
        CAPABILITY_GEOCODE: {"engine/executor/geocode_capability.py"},
        CAPABILITY_CENSUS: {"engine/executor/census_capability.py"},
    }
    src = Path(__file__).resolve().parents[2] / "src" / "frisket"

    stamped: dict[str, set[str]] = {}
    for path in src.rglob("*.py"):
        rel = str(path.relative_to(src))
        # rule19: two-sources — producer call-site capability literals vs canonical roster
        for literal in _fact_capability_literals(path.read_text(encoding="utf-8")):
            stamped.setdefault(literal, set()).add(rel)

    # Every literal must be canonical, including when another producer uses the
    # same capability.
    allowed = set(EXECUTION_CAPABILITIES) | non_seam
    noncanonical = set(stamped) - allowed
    drifted = {
        literal: sorted(files)
        for literal, files in stamped.items()
        if literal in noncanonical
    }
    assert not drifted, (
        f"these capability literals are not canonical tokens: {drifted}. The "
        "fact column would carry two spellings for one kind of work."
    )

    for capability, modules in producers.items():
        assert stamped.get(capability, set()) >= modules, (
            f"{capability!r} is stamped by {sorted(stamped.get(capability, ()))} "
            f"but must be stamped by each of {sorted(modules)}"
        )


def test_fact_capability_scan_tracks_constructors_but_ignores_unrelated_apis():
    source = """
def stamp(capability):
    return ModelCallMeta.provider_call(capability=capability)

stamp(capability="translate")
ModelCallMeta.cache_hit(capability="invalid.fact.token")
ModelCallMeta(capability="invalid.constructor.token")
local_embedder(capability="providerless_classify")
"""

    literals = _fact_capability_literals(source)
    assert "providerless_classify" not in literals
    assert literals - {"translate"} == {
        "invalid.fact.token",
        "invalid.constructor.token",
    }


def test_the_epoch_invariant_will_notice_a_lane_that_flips_without_wiring_it():
    """Every routing recipe/typed program belongs to the epoch ground truth."""
    from frisket.engine.store.runs import RunResultStore

    registry = _actual_programs()
    routing = {
        recipe.execution_capability
        for recipe in registry.values()
        if recipe.consumes_resolution
    }
    assert set(RunResultStore._ROUTED_FACT_CAPABILITIES) == routing, (
        "the epoch invariant's ground truth and the set of routing recipes "
        "disagree. A routing capability missing from "
        "_ROUTED_FACT_CAPABILITIES records legacy facts under a routed run "
        "with no binding observation, silently; one listed there with no "
        "routing recipe is dead enforcement."
    )


def test_the_translate_seam_roster_is_the_contract_axis_minus_llm():
    """Two rosters, one vocabulary. ``maps.TRANSLATE_ENGINES`` stays the
    contract's authoritative engine axis; the seam table is its non-LLM subset.
    Add an engine to either and this names the gap — the two-surfaces-two-
    answers bug, caught at the only place both are visible."""
    from frisket.contracts.actions.schemas._engines import (
        TRANSLATE_ENGINE_TABLE,
        engine_ids,
    )
    from frisket.actions.translation_languages import TRANSLATE_ENGINES

    seam = set(engine_ids(TRANSLATE_ENGINE_TABLE))
    assert seam | {"llm"} == set(TRANSLATE_ENGINES), sorted(
        seam.symmetric_difference(set(TRANSLATE_ENGINES) - {"llm"})
    )
    assert "llm" not in seam


def test_llm_cannot_resolve_as_a_translate_route():
    """The deliberate blocker, asserted rather than commented.

    LLM translate is priced in TWO units (input and output tokens per million)
    and ``PricedCostBasis`` carries exactly one (rate, quantity, unit) triple.
    Until that ruling lands, an ``engine="llm"`` translate spec must refuse at
    resolution — never resolve onto a venue whose meter the cost basis cannot
    express, which would be a fabricated quote rather than a missing feature.

    This is also the gate on the translate LANE: ``TranslateRecipe`` defaults
    to ``engine="llm"``, so it cannot flip ``consumes_resolution`` while this
    refusal stands.
    """
    outcome = resolve(
        ResolutionRequest(engine="llm", options={}, capability=CAPABILITY_TRANSLATE),
        _provider(),
        CompositionFacts(),
    )
    assert isinstance(outcome, Refusal)
    assert outcome.family in {"no_capable_target", "unknown_engine"}, outcome


def test_the_to_markdown_seam_roster_is_the_shipped_engine_table():
    """to_markdown reuses the roster it already had — the seam did not mint a
    second one, which is why a to_markdown engine cannot be servable by the
    picker and unresolvable by the seam."""
    from frisket.contracts.actions.schemas._engines import (
        TO_MARKDOWN_ENGINE_TABLE,
        engine_ids,
    )
    from frisket.execution.definitions import build_static_targets

    rostered = set(engine_ids(TO_MARKDOWN_ENGINE_TABLE))
    served = {
        support.engine
        for target in build_static_targets()
        for support in target.engines
        if support.capability == CAPABILITY_TO_MARKDOWN
    }
    assert served == rostered, sorted(served.symmetric_difference(rostered))


def test_every_phase_4_engine_has_a_venue_that_declares_it():
    """The roster/venue join, per capability: an engine the contract accepts
    but no target declares refuses ``no_capable_target`` at run time."""
    from frisket.contracts.actions.schemas._engines import engine_ids
    from frisket.execution.definitions import build_static_targets
    from frisket.execution.resolver import capability_engine_table

    targets = build_static_targets()
    for capability in _PHASE_4:
        rostered = set(engine_ids(capability_engine_table(capability)))
        served = {
            support.engine
            for target in targets
            for support in target.engines
            if support.capability == capability
        }
        assert rostered == served, (
            f"{capability}: rostered {sorted(rostered)} but venues declare "
            f"{sorted(served)}"
        )


@pytest.mark.parametrize(
    "engine,options,expect_refusal",
    [
        # hy_mt2's prompt names only the target language: a source hint would
        # be silently dropped, so it refuses.
        ("hy_mt2", {"language": ["de"]}, True),
        ("hy_mt2", {"language": []}, False),
        # opus_mt loads a per-language-PAIR artifact: with no source there is
        # no model to pick.
        ("opus_mt", {"language": []}, True),
        ("opus_mt", {"language": ["de"]}, False),
    ],
)
def test_the_translate_ability_checker_refuses_in_both_directions(
    engine, options, expect_refusal
):
    """Both declared fields are load-bearing, and neither is derivable from the
    other: hy_mt2 is (source_language=False, auto_detect=True) and opus_mt is
    the exact inverse. Flip either declaration and one row here goes red."""
    outcome = resolve(
        ResolutionRequest(
            engine=engine, options=options, capability=CAPABILITY_TRANSLATE
        ),
        _provider(),
        CompositionFacts(),
    )
    if expect_refusal:
        assert isinstance(outcome, Refusal), outcome
        assert outcome.family == "no_capable_target"
    else:
        assert not isinstance(outcome, Refusal), outcome


def test_the_geocode_ability_checker_refuses_a_scoped_lookup_on_nominatim():
    """Nominatim's ``/search`` is called with ``q`` + ``format`` only. A scoped
    lookup authored against it would widen to a whole-world free-text match and
    return a confident wrong point — the silent-drop class, in the shape that
    produces bad data rather than an error."""
    refused = resolve(
        ResolutionRequest(
            engine="nominatim",
            options={"structured_query": True},
            capability=CAPABILITY_GEOCODE,
        ),
        _provider(),
        CompositionFacts(),
    )
    assert isinstance(refused, Refusal), refused
    assert refused.family == "no_capable_target"

    # And it resolves without the scoped option — the fence is not just "always
    # refuse".
    ok = resolve(
        ResolutionRequest(
            engine="nominatim", options={}, capability=CAPABILITY_GEOCODE
        ),
        _provider(),
        CompositionFacts(),
    )
    assert not isinstance(ok, Refusal), ok


# The seam, end to end, through the real resolve_for_action.
# Key-set closures over cost-basis / option-key registries are vacuous:
# they discard the values, so remapping the geocode seam entry to
# _census_cost_basis (quoting a paid OpenCage lookup as FREE) or dropping
# "structured_query" from the geocode option tuple (letting a scoped lookup
# escape onto Nominatim) leaves every one of them green. And resolving a
# hand-built ResolutionRequest is bug pattern #5 — it stubs above the dispatch,
# option forwarding and cost mint that are the things actually under test.
#
# So each capability gets a synthetic recipe — the REAL recipe class with only
# the two seam markers flipped — driven through resolve_for_action itself, and
# the assertions are on VALUES: which venue it bound, the exact pricing key and
# unit it quoted, and the quantity it measured.


@pytest.fixture()
def phase4_project(tmp_path):
    """One sheet carrying what the four quantity functions actually read: source
    text, a two-page document, an address, and a geo point."""
    from frisket.engine.store import Project
    from frisket.engine.store.media_blobs import media_cell

    project = Project.create(tmp_path / "phase4.frisket", name="phase4")
    sheet_id = project.add_sheet("Rows")
    text_col = project.add_column(sheet_id, "body", type="text")
    doc_col = project.add_column(sheet_id, "doc", type="file")
    point_col = project.add_column(sheet_id, "point", type="geo_point")
    blob = project.add_blob(
        b"%PDF-1.4 two-page-doc",
        filename="d.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "document", "pages": 2}),
    )
    project.add_rows(
        sheet_id,
        [
            {
                "body": "hello world",
                "point": {"lat": 38.9, "lon": -77.03},
                "doc": media_cell(
                    blob,
                    mime="application/pdf",
                    filename="d.pdf",
                ),
            }
        ],
        {"body": text_col, "doc": doc_col, "point": point_col},
    )
    try:
        yield project, sheet_id
    finally:
        project.close()


#: priced capability -> (recipe name, spec extras, expected venue, expected
#: pricing key, expected quantity unit, expected quantity). Census is pinned
#: separately below because its free public API has no priced basis or SKU.
_ROUTING_CASES = [
    pytest.param(
        "map.translate",
        CAPABILITY_TRANSLATE,
        {"engine": "deepl", "input_columns": ["body"], "language": ["de"]},
        "deepl",
        "deepl.translate.char",
        "character",
        "11",
        id="translate-deepl",
    ),
    pytest.param(
        "media.to_markdown",
        CAPABILITY_TO_MARKDOWN,
        {"engine": "datalab", "input_columns": ["doc"]},
        "datalab",
        "datalab.convert.page",
        "page",
        "2",
        id="to_markdown-datalab",
    ),
    pytest.param(
        "enrich.geocode",
        CAPABILITY_GEOCODE,
        {"engine": "opencage", "input_columns": ["body"]},
        "opencage",
        "geocode.opencage.row",
        "row",
        "1",
        id="geocode-opencage",
    ),
]


@pytest.mark.parametrize(
    "recipe_name,capability,extras,venue,sku,unit,quantity", _ROUTING_CASES
)
def test_a_routing_recipe_binds_its_venue_and_quotes_its_own_sku(
    phase4_project,
    monkeypatch,
    recipe_name,
    capability,
    extras,
    venue,
    sku,
    unit,
    quantity,
):
    """The end-to-end value assertion the key-set closures could not make.

    Remap this capability's cost-basis entry to another
    capability's cost-basis function and the pricing key goes wrong — usually
    to ``OperatorBorneZeroCost``, i.e. a paid third-party call quoted as free,
    which is the failure a key-set comparison cannot see.
    """
    from frisket.execution.promise_compiler import PricedCostBasis
    from frisket.execution.resolve_for_action import resolve_for_action

    for env_name in (
        "DEEPL_API_KEY",
        "GOOGLE_TRANSLATE_API_KEY",
        "OPENCAGE_API_KEY",
        "CENSUS_API_KEY",
        "DATALAB_API_KEY",
    ):
        monkeypatch.setenv(env_name, "secret")

    project, sheet_id = phase4_project
    params = {
        "source": extras["input_columns"]
        if recipe_name == "map.translate"
        else extras["input_columns"][0],
        **{key: value for key, value in extras.items() if key != "input_columns"},
    }
    plan = _typed_plan(recipe_name, sheet_id=sheet_id, params=params, project=project)
    spec, program = plan.spec_dict(), plan.program
    resolved = resolve_for_action(
        project,
        spec,
        program,
        composition=open_execution_composition(
            project, ModelRouter(), ExecutionCompositionContext.direct()
        ),
    )

    assert not isinstance(resolved, Refusal), resolved
    assert resolved is not None, "a routing recipe must resolve, not opt out"
    assert resolved.resolution.target.id == venue
    basis = resolved.cost_basis
    assert isinstance(basis, PricedCostBasis), basis
    assert basis.pricing_key == sku
    assert basis.quantity_unit == unit
    assert basis.estimated_quantity == quantity
    assert resolved.promise_set.promises


def test_census_route_has_known_zero_public_spend_and_no_priced_basis(
    phase4_project, monkeypatch
):
    """Census still routes and compiles its external-egress promise, but the
    public API's absent price is represented by absence, never a row SKU."""
    from frisket.execution.promise_compiler import OperatorBorneZeroCost
    from frisket.execution.resolve_for_action import resolve_for_action

    monkeypatch.setenv("CENSUS_API_KEY", "secret")
    project, sheet_id = phase4_project
    plan = _typed_plan(
        "enrich.census_demographics",
        sheet_id=sheet_id,
        params={"source": "point"},
        project=project,
    )
    resolved = resolve_for_action(
        project,
        plan.spec_dict(),
        plan.program,
        composition=open_execution_composition(
            project, ModelRouter(), ExecutionCompositionContext.direct()
        ),
    )

    assert not isinstance(resolved, Refusal), resolved
    assert resolved is not None
    assert resolved.resolution.target.id == "us-census"
    assert isinstance(resolved.cost_basis, OperatorBorneZeroCost)
    assert resolved.promise_set.promises


def test_the_geocode_option_key_reaches_the_ability_checker_through_the_seam(
    phase4_project, monkeypatch
):
    """Drop ``"structured_query"`` from ``_CAPABILITY_OPTION_KEYS[geocode]``
    and every key-set closure stays green while ``authored_options`` silently
    stops forwarding it — so ``_geocode_inability`` never sees it and a scoped
    lookup routes to Nominatim, which answers a whole-world free-text match.

    This drives the REAL ``resolve_for_action``, so it fails on that deletion.
    The direct ``authored_options`` check beside it names the cause when it
    does.
    """
    from frisket.execution.resolve_for_action import (
        authored_options,
        resolve_for_action,
    )

    project, sheet_id = phase4_project
    plan = _typed_plan(
        "enrich.geocode",
        sheet_id=sheet_id,
        params={"source": "body", "engine": "nominatim"},
        project=project,
    )
    # This is an execution-seam option, not an authored Geocode Params field.
    # Even a caller below typed validation cannot silently lose the constraint.
    spec = {**plan.spec_dict(), "structured_query": True}
    assert authored_options(spec, CAPABILITY_GEOCODE) == {"structured_query": True}

    refused = resolve_for_action(
        project,
        spec,
        plan.program,
        composition=open_execution_composition(
            project, ModelRouter(), ExecutionCompositionContext.direct()
        ),
    )
    assert isinstance(refused, Refusal), refused
    assert refused.family == "no_capable_target"


@pytest.mark.parametrize(
    "capability,engine,env_name",
    [
        (CAPABILITY_TRANSLATE, "deepl", "DEEPL_API_KEY"),
        (CAPABILITY_TRANSLATE, "google_translate", "GOOGLE_TRANSLATE_API_KEY"),
        (CAPABILITY_GEOCODE, "opencage", "OPENCAGE_API_KEY"),
    ],
)
def test_a_keyed_phase_4_venue_is_dead_without_its_key_and_names_it(
    capability, engine, env_name
):
    """Each venue is probed against the SAME env name its remedy tells the
    operator to set. Point the probe at one key and the remedy at another and
    this goes red — the failure is an operator setting the named key and the
    venue staying dead.

    Census is NOT parametrized here (the routed-capability contract): unlike
    these three, it is not dead without its key — see
    ``test_census_is_live_with_no_credential_and_still_live_with_one``.
    """
    dead = resolve(
        ResolutionRequest(engine=engine, options={}, capability=capability),
        _provider(),
        CompositionFacts(),
    )
    assert isinstance(dead, Refusal), dead
    assert dead.family == "no_live_target"
    assert env_name in (dead.remedy or ""), dead.remedy

    live = resolve(
        ResolutionRequest(engine=engine, options={}, capability=capability),
        _provider(**{env_name: "secret"}),
        CompositionFacts(),
    )
    assert not isinstance(live, Refusal), live


def test_nominatim_is_live_with_no_credential_at_all():
    """The honest answer for a venue that takes no key at all: a
    dead-without-key probe would refuse a path that works for everyone."""
    outcome = resolve(
        ResolutionRequest(
            engine="nominatim", options={}, capability=CAPABILITY_GEOCODE
        ),
        _provider(),
        CompositionFacts(),
    )
    assert not isinstance(outcome, Refusal), outcome
    assert outcome.target.id == "nominatim"
    assert outcome.facts.egress_class == "third_party_api"


def test_census_is_live_with_no_credential_and_still_live_with_one():
    """Census is unlike Nominatim (no credential slot at all) and unlike
    OpenCage/DeepL/Google (dead without their key): it HAS a credential env
    (``CENSUS_API_KEY_ENV``) that raises the rate limit when used, but the
    Census API tolerates an unauthenticated request, so the venue
    resolves live either way (the routed-capability contract — the parent's
    anonymous MapRunner census flow must keep working after routing)."""
    anonymous = resolve(
        ResolutionRequest(
            engine="us_census_acs", options={}, capability=CAPABILITY_CENSUS
        ),
        _provider(),
        CompositionFacts(),
    )
    assert not isinstance(anonymous, Refusal), anonymous
    assert anonymous.target.id == "us-census"
    assert anonymous.facts.egress_class == "third_party_api"

    keyed = resolve(
        ResolutionRequest(
            engine="us_census_acs", options={}, capability=CAPABILITY_CENSUS
        ),
        _provider(CENSUS_API_KEY="secret"),
        CompositionFacts(),
    )
    assert not isinstance(keyed, Refusal), keyed
    assert keyed.target.id == "us-census"


def test_the_canonical_preview_envelope_is_fully_classified() -> None:
    """Every canonical money or rendered presentation key is classified."""
    from frisket.engine.runner.confirmation_context import (
        KNOWN_DISPLAY_KEYS,
        ConsentQuote,
    )

    lane_envelope_keys = {
        "cost",
        "cost_source",
        "engine",
        "pricing_key",
        "warning",
        "audio_seconds",
        "avg_input_tokens",
        "billing_label",
        "venue_label",
        "rows",
        "claims",
        "promise_set_hash",
        # Rated money fields are classified separately from display keys.
        "billed_cost",
        "policy_id",
    }
    unclassified = sorted(
        lane_envelope_keys - set(ConsentQuote.model_fields) - KNOWN_DISPLAY_KEYS
    )
    assert not unclassified, (
        f"the Phase-4 lanes will emit unclassified estimate keys: "
        f"{unclassified}. Each needs a money-or-display decision in "
        "confirmation_context.py BEFORE the lane ships, or its consent hash "
        "refuses at mint."
    )


def test_a_phase_4_lane_envelope_projects_into_a_consent_quote():
    """The projection itself, end to end, for one priced lane envelope — the
    check the test above makes over key names, executed. A key that is
    classified but malformed still refuses, which is why this drives
    ``from_estimate`` rather than re-checking the sets."""
    from frisket.engine.runner.confirmation_context import ConsentQuote, rate_estimate
    from frisket.execution.price_book import OperatorBorne, quote_geocode
    from frisket.execution.pricing_policy import IDENTITY_PRICING_POLICY
    from frisket.execution.promise_compiler import PricedCostBasis
    from frisket.ops.cost_source import cost_estimate

    basis = quote_geocode(
        target_id="opencage",
        engine="opencage",
        funding=OperatorBorne(),
        offering=None,
        rows=10,
    )
    assert isinstance(basis, PricedCostBasis)
    envelope = cost_estimate(
        cost=round(float(basis.bound), 6),
        cost_source="pricing_data",
        engine="opencage",
        pricing_key=basis.pricing_key,
        rows=10,
    )
    # Through the RATING site, as every real mint is: ``policy_id`` is a
    # required ConsentQuote field, so an unrated envelope refuses at the
    # projection. A lane that renders a cost basis and skips ``rate_estimate``
    # would fail exactly here, which is the point of the requirement.
    quote = ConsentQuote.from_estimate(
        rate_estimate(envelope, policy=IDENTITY_PRICING_POLICY)
    )
    assert quote.pricing_key == "geocode.opencage.row"
    assert quote.rows == 10
    assert quote.cost == pytest.approx(0.1)
    assert quote.policy_id == IDENTITY_PRICING_POLICY.policy_id
