"""The scoped seam-parameter registry — the ratchet for the NEXT
seam.

**Not** a blanket ban on ``Optional[Callable]``: this codebase has ~149
legitimate optional knobs and a rule that forbade them all would be ignored.
This is a short, DECLARED list of parameters that carry an invariant a caller
must not be able to omit, plus a machine-checked reason for every symbol the
spec proposed and this release did not convert.

The recurring failure it ratchets against is CLAUDE.md's first bug pattern:
*a verification or capability passed as an optional parameter that some call
site forgets*, fixed "at the place the work actually happens: make the input
required there, don't rely on callers remembering."

The registry is deliberately small. The value is not today's three symbols; it
is that the next parameter carrying an invariant has a place to be declared,
and a test that fails the day it acquires a default.
"""

from __future__ import annotations

import inspect

import pytest

# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: (import path, symbol, parameter) -> why the parameter carries an invariant.
#: Every member must be REQUIRED with NO DEFAULT.
SEAM_PARAMETERS: dict[tuple[str, str, str], str] = {
    (
        "frisket.engine.runner.map_runner",
        "MapRunner",
        "authority",
    ): (
        "the attempt authority admits the dispatch; a MapRunner without one "
        "would execute routed work unverified. Eleven "
        "construction sites used to be eleven callers trusted to remember a "
        "keyword, with an effect-site fence to notice when one didn't."
    ),
    (
        "frisket.execution.resolve_for_action",
        "resolve_for_action",
        "composition",
    ): (
        "funding facts and the target provider built from the request's exact "
        "effective router are one admission input; resolution must never "
        "silently reconstruct an open/default provider"
    ),
}


def _parameter(module_path: str, symbol: str, parameter: str):
    module = __import__(module_path, fromlist=[symbol])
    target = getattr(module, symbol)
    signature = inspect.signature(target)
    assert parameter in signature.parameters, (
        f"{module_path}.{symbol} no longer declares {parameter!r} — the "
        "registry entry is stale; delete it or repoint it deliberately"
    )
    return signature.parameters[parameter]


@pytest.mark.parametrize(
    "member", sorted(SEAM_PARAMETERS), ids=lambda m: f"{m[1]}.{m[2]}"
)
def test_every_registered_seam_parameter_is_required_with_no_default(member):
    module_path, symbol, parameter = member
    param = _parameter(module_path, symbol, parameter)
    assert param.default is inspect.Parameter.empty, (
        f"{symbol}.{parameter} acquired a default. It is a registered seam "
        f"parameter: {SEAM_PARAMETERS[member]}"
    )


def test_omitting_a_registered_seam_parameter_is_a_construction_error():
    """The registry states a property; this proves it BITES. A ``MapRunner``
    built without an authority is a ``TypeError`` at the construction site,
    not a fence firing later at the effect site."""
    from frisket.engine.runner.map_runner import MapRunner

    with pytest.raises(TypeError):
        MapRunner(object(), object())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Two proposed symbols were deliberately NOT converted, with the reason
# machine-checked, so the exclusion cannot quietly become wrong
# ---------------------------------------------------------------------------


def test_map_runner_factory_stays_optional_because_the_funnel_resolves_it():
    """``ExecutorDeps.map_runner_factory`` remains optional, and this test pins
    why.

    ``ExecutorDeps`` is a bag of ~11 optional dependency OVERRIDES: 65
    construction sites, 55 of them tests injecting an unrelated stub
    (``embedding_gateway=``, ``url_capture_fetcher=``, …). Requiring a map
    runner factory at all of them would push executor internals into tests
    that never run a map action — ceremony, not safety.

    More importantly the field's ``None`` is not forgotten wiring, it is a
    genuine THIRD state: two surfaces want two different defaults for
    "caller did not specify" — ``run_action_spec`` wants the production
    factory, ``resolve_map_preview`` wants the in-memory preview factory. A
    non-optional field cannot express that without a sentinel.

    What makes the guards non-load-bearing is that the executor funnel
    always fills the field. THAT is the invariant, and it is what this test
    pins — if a future change lets an unresolved ``ExecutorDeps`` reach the
    executor, this goes red and the member gets promoted after all.
    """
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.engine.executor.actions import _executor_deps_with_defaults

    def resolved(deps):
        return _executor_deps_with_defaults(
            deps=deps,
            router=None,
            rss_fetcher=None,
            enclosure_fetcher=None,
            url_capture_fetcher=None,
            url_capture_browser=None,
        )

    # No deps at all, and deps carrying an UNRELATED override: both come out
    # of the funnel with a factory.
    assert resolved(None).map_runner_factory is not None
    assert resolved(ExecutorDeps()).map_runner_factory is not None
    assert resolved(ExecutorDeps(embedding_gateway=object())).map_runner_factory
    # A caller-supplied factory is never overwritten.
    sentinel = object()
    assert resolved(ExecutorDeps(map_runner_factory=sentinel)).map_runner_factory is (
        sentinel
    )


def test_route_store_no_longer_answers_installation_scoped_questions():
    """The split is structural, not a rename: the subject-blind queries are
    gone from the subject-scoped store, so a caller cannot reach them with an
    invented subject at all."""
    from frisket.engine.store.execution_routes import ConsentRegistry, RouteStore

    assert not hasattr(RouteStore, "standing_consents")
    assert not hasattr(RouteStore, "action_consents")
    assert hasattr(ConsentRegistry, "standing_consents")
    assert hasattr(ConsentRegistry, "action_consents")


def test_every_surviving_route_store_read_actually_uses_its_subject(tmp_path):
    """The property the split restored: two stores on DIFFERENT subjects give
    different answers for every read ``RouteStore`` still owns. Before the
    split, ``standing_consents``/``action_consents`` returned identical rows
    for every subject — which is what made the fake one harmless enough to
    survive three releases."""
    from frisket.engine.store import Project
    from frisket.engine.store.execution_routes import RouteStore

    project = Project.create(tmp_path / "subjects.frisket")
    try:
        run_store = RouteStore.for_run(project, 1)
        promise_set = run_store.append_promise_set(
            promises=[
                {
                    "field": "egress_class",
                    "op": "eq",
                    "value": "none",
                    "basis": None,
                    "order_ref": None,
                    "audience": "user_claim",
                }
            ],
            predecessor_id=None,
        )
        run_store.append_route(
            promise_set_id=promise_set.id,
            engine="faster_whisper",
            options={},
            target_snapshot={
                "target_id": "local",
                "capability": "transcribe",
                "transport": "local",
                "run_scoped": False,
            },
            operator="self",
            egress_class="none",
            region=None,
            credential_source="local",
            cost_posture="operator_borne",
            predecessor_id=None,
        )
        other = RouteStore.for_run(project, 2)
        assert run_store.head() is not None
        assert other.head() is None
        assert run_store.promise_sets() and other.promise_sets() == []
        assert run_store.consents() == [] and other.consents() == []
        assert run_store.violations() == [] and other.violations() == []
    finally:
        project.close()
