"""Build typed action requests from the reusable model test fixture values."""

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    build_typed_map_rows_plan,
)


def model_request(spec, *, action_id=None):
    if "params" in spec:
        from frisket.engine.executor.map_rows_action import (
            bound_typed_program_request_from_runner_spec,
        )

        bound = bound_typed_program_request_from_runner_spec(spec)
        if bound is not None:
            return bound.request
    kind = action_id or spec.get("action_kind", "map.classify")
    source = (
        {"text": spec["input_template"]}
        if spec.get("input_template")
        else spec.get("input_columns", ["text"])
    )
    params = {"source": source}
    accepted = ACTION_REGISTRY.get(kind).definition.run.params_model.model_fields
    params.update(
        {
            key: value
            for key, value in spec.items()
            if key in accepted and key != "source"
        }
    )
    if kind == "map.classify":
        params.setdefault("engine", "llm")
    outputs = (
        {"entities": spec.get("output_name", "entities")} if kind == "map.ner" else {}
    )
    scope = {"kind": "sheet_rows", "sheet_id": spec.get("sheet_id", 1)}
    if spec.get("row_ids") is not None:
        scope["row_ids"] = spec["row_ids"]
    return ActionRequest(
        action_id=kind,
        scope=scope,
        params=params,
        output_names=outputs,
        idempotency_key=spec.get("idempotency_key", "typed-model-test"),
        replace_existing=spec.get("replace_existing", False),
    )


def model_plan(spec, project=None, *, action_id=None):
    request = model_request(spec, action_id=action_id)
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    return (
        build_typed_map_rows_plan(project, bound)
        if project is not None
        else _typed_map_rows_plan(bound)
    )


def render_classify(values, spec):
    plan = model_plan(spec, action_id="map.classify")
    return plan.program.render(values, plan.spec_dict())


def render_ner(values, spec):
    plan = model_plan(
        {
            **spec,
            "engine": "llm",
            "model": spec.get("model") or "anthropic/claude-haiku-4-5",
            "input_columns": list(values),
        },
        action_id="map.ner",
    )
    return plan.program.render(values, plan.spec_dict())


def _is_typed_action(spec):
    """Whether the runner spec names a typed built-in (``ACTION_REGISTRY``).

    Built-ins no longer register a legacy ``Recipe``; only plugin recipes are
    still resolved by ``action_kind`` through ``recipe_for_spec``.
    """
    kind = spec.get("action_kind")
    return isinstance(kind, str) and kind in ACTION_REGISTRY.action_ids


def _runner_binding(runner, spec):
    plan = model_plan(spec)
    runner.allow_action_lifecycle_only_recipes = True
    return {**spec, **plan.spec_dict()}, plan.program


def prepare_model_run(runner, spec, **kwargs):
    bound_spec, program = _runner_binding(runner, spec)
    return runner.prepare_run(bound_spec, program=program, **kwargs)


def estimate_model_run(runner, spec):
    bound_spec, program = _runner_binding(runner, spec)
    return runner.estimate(bound_spec, program=program)


def estimate_model_spec(project, spec, **kwargs):
    """``validation.estimate_run`` for a typed built-in runner spec.

    Binds the canonical typed request against the project (the same plan the
    executor admits) and estimates with that plan's program, exactly as the
    runner facade does; ``kwargs`` pass through to ``estimate_run``.
    """
    from frisket.engine.runner.validation import estimate_run

    plan = model_plan(spec, project)
    return estimate_run(
        project, {**spec, **plan.spec_dict()}, program=plan.program, **kwargs
    )


async def run_with_output_claim(runner, spec, **kwargs):
    from runner_test_helpers import run_with_output_claim as run

    if not _is_typed_action(spec):
        return await run(runner, spec, **kwargs)
    bound_spec, program = _runner_binding(runner, spec)
    return await run(runner, bound_spec, program=program, **kwargs)


def prepare_with_exact_confirmation(runner, spec):
    from frisket.engine.runner.validation import ClaimsGate

    try:
        return prepare_model_run(runner, spec, confirmed=False)
    except ClaimsGate as gate:
        return prepare_model_run(
            runner,
            {**spec, "consented_promise_set_hash": gate.promise_set_hash},
            confirmed=True,
        )


async def run_with_exact_confirmation(runner, spec, **kwargs):
    from frisket.engine.runner.validation import ClaimsGate

    try:
        return await run_with_output_claim(runner, spec, confirmed=False, **kwargs)
    except ClaimsGate as gate:
        return await run_with_output_claim(
            runner,
            {**spec, "consented_promise_set_hash": gate.promise_set_hash},
            confirmed=True,
            **kwargs,
        )
