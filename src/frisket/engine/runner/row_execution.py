"""Per-row execution for the map runner (extracted from
``MapRunner._execute_row``/``MapRunner._llm_row``/``MapRunner._maybe_throttle``):
render one row's model/recipe call, classify its failure into a typed cell
error, and adapt concurrency-wide throttle state to 429s. Operates on explicit
``project``/``router``/``throttle`` rather than an implicit ``self`` so leaf
callers (including ``preview``) need not import the engine. Also carries
``_await_owned_tasks`` (extracted from map_runner.py's module scope), the
small task-group helper both the engine's row-worker loop and preview's
per-row fan-out use to join sibling row tasks."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from frisket.ai.llm import (
    CacheMiss,
    LLMError,
    LLMRequest,
    LLMResponse,
    ModelRouter,
    StructuredCompleter,
    StructuredRequest,
    classify_llm_error,
    classify_resumable_provider_error,
)
from frisket.local_model_ids import bare_model_name
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ai.models.metadata import ModelCallMeta
from frisket.ops.base import OpContext, Recipe, RecipeInvocationHalt
from frisket.redaction import safe_error
from frisket.engine.runner import validation
from frisket.engine.runner.grounding import grounding_enabled, unwrap_grounded_value
from frisket.engine.runner.publication import (
    PUBLISH_ERROR,
    PUBLISH_NULL,
    PUBLISH_VALUE,
    PreparedRowPublication,
    finalize_prepared_row_publication,
)
from frisket.engine.runner.row_inputs import row_source_values_empty
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store import Project
from frisket.engine.store.runs import TERMINAL_FAILURE_OUTCOMES
from frisket.execution.attempt import StaleAttemptWriter
from frisket.operability.trace import TraceWriter, TracingRouter

# The sidecar prefix a grounded field's justification carries so
# frisket.sdk.ops.extract can tell a grounding payload apart from an
# ordinary justification string. Re-exported from map_runner (unchanged
# import site: frisket/sdk/ops/extract.py).
GROUNDING_SIDECAR_PREFIX = "__frisket_grounding_v1__:"


class AdaptiveThrottle:
    """429s slow the whole run down instead of killing it; successes decay the delay."""

    def __init__(self) -> None:
        self.delay = 0.0

    def backoff(self, e: Exception) -> None:
        if isinstance(e, LLMError) and e.status == 429:
            self.delay = min(5.0, (self.delay or 0.25) * 2)

    def decay(self) -> None:
        self.delay = max(0.0, self.delay - 0.1)

    async def wait(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)


async def _await_owned_tasks(*coroutines: Any) -> None:
    """Await a small explicit task set and join every sibling on any escape."""

    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        await asyncio.gather(*tasks)
    except BaseException as primary:
        for task in tasks:
            task.cancel()
        joined = await asyncio.gather(*tasks, return_exceptions=True)
        teardown_error = next(
            (result for result in joined if isinstance(result, SandboxTeardownError)),
            None,
        )
        if teardown_error is not None and teardown_error is not primary:
            teardown_error.add_note(
                "a concurrent sibling also failed while the sandbox process tree "
                "could not be verified as stopped"
            )
            raise teardown_error from primary
        raise


async def execute_row(
    project: Project,
    router: ModelRouter,
    throttle: AdaptiveThrottle,
    recipe: Recipe,
    values: dict,
    spec: dict,
    ctx: OpContext,
    *,
    row_id: int | None = None,
    recorder: TraceWriter | None = None,
    output_field_names: tuple[str, ...] | None = None,
    required_output_field_names: frozenset[str] = frozenset(),
    managed_publication: bool = False,
) -> dict[str, dict]:
    """Execute a row and return publication/accounting data by field.

    Every model call is captured best-effort through a ``TracingRouter``.
    """
    # This public entry point may be called without prior spec validation.
    validation.assert_network_policy(project, recipe, spec)
    field_names = (
        list(output_field_names)
        if output_field_names is not None
        else [f["name"] for f in recipe.output_fields(spec)]
    )
    # Do not ask a plain LLM to fabricate output from an empty source row.
    if (
        recipe.is_llm(spec)
        and not hasattr(recipe, "run_agent")
        and not recipe.allow_all_empty_input
        and row_source_values_empty(values)
    ):
        return {
            name: {
                "value": None,
                "tokens_in": None,
                "tokens_out": None,
                "cost": 0.0,
                "publication_effect": PUBLISH_NULL,
            }
            for name in field_names
        }
    row_trace = recorder.row_trace(row_id) if recorder else None
    router = TracingRouter(router, row_trace) if row_trace else router

    def persist_trace(
        *, data: Any = None, meta: dict | None = None, error: str | None = None
    ) -> None:
        if recorder and row_trace:
            recorder.write_row(router.record(data=data, meta=meta, error=error))

    meta: dict[str, Any] | None = None
    try:
        if hasattr(recipe, "run_agent"):
            row_ctx = OpContext(
                project=ctx.project,
                http=ctx.http,
                extras={**ctx.extras, "router": router, "row_id": row_id},
                credential_use_context=ctx.credential_use_context,
                execution_limits=ctx.execution_limits,
            )
            data, meta = await recipe.run_agent(values, spec, row_ctx, router)
        elif recipe.is_llm(spec):
            data, meta = await llm_row(router, throttle, recipe, values, spec)
            data = recipe.normalize_model_output(data, spec, row_values=values)
        else:
            # Clone context because parallel rows must not share router state.
            row_ctx = (
                OpContext(
                    project=ctx.project,
                    http=ctx.http,
                    extras={**ctx.extras, "router": router, "row_id": row_id},
                    credential_use_context=ctx.credential_use_context,
                    execution_limits=ctx.execution_limits,
                )
                if ctx.extras.get("router") is not None
                else OpContext(
                    project=ctx.project,
                    http=ctx.http,
                    extras={**ctx.extras, "row_id": row_id},
                    credential_use_context=ctx.credential_use_context,
                    execution_limits=ctx.execution_limits,
                )
            )
            data = await recipe.execute(values, spec, row_ctx)
            meta = {"tokens_in": None, "tokens_out": None, "cost": 0.0}
            if isinstance(data, tuple) and len(data) == 2:
                data, returned_meta = data
                meta = {**meta, **(returned_meta or {})}
        if isinstance(data, PreparedRowPublication):
            persist_trace(data=data.trace_data, meta=meta)
            return finalize_prepared_row_publication(
                data,
                field_names,
                meta,
            )
        persist_trace(data=data, meta=meta)
        out: dict[str, dict] = {}
        confidence = None
        outcome = None
        generic_justification = None
        generic_error = None
        generic_error_code = None
        if isinstance(data, dict):
            confidence = next(
                (v for k, v in data.items() if k.endswith("_confidence")),
                data.get("confidence"),
            )
            if isinstance(data.get("outcome"), str):
                outcome = data["outcome"]
            if isinstance(data.get("justification"), str):
                generic_justification = data["justification"]
            if isinstance(data.get("error"), str):
                safe = safe_error(
                    data.get("error_code")
                    if isinstance(data.get("error_code"), str)
                    else "model_error",
                    data["error"],
                    max_chars=500,
                )
                generic_error = safe.detail
                generic_error_code = safe.code
        field_errors: dict[str, Any] = {}
        if isinstance(data, dict) and isinstance(data.get("__field_errors__"), dict):
            field_errors = data["__field_errors__"]
        grounding_active = grounding_enabled(spec)
        for name in field_names:
            value_was_returned = isinstance(data, dict) and name in data
            val = data.get(name) if value_was_returned else None
            grounding_sidecar: dict[str, Any] | None = None
            if grounding_active:
                val, grounding_sidecar = unwrap_grounded_value(name, val)
            if val is not None and generic_error is None and name not in field_errors:
                val = recipe.postprocess_value(name, val, spec)
            cell: dict[str, Any] = {"value": val, **meta}
            if confidence is not None:
                cell["confidence"] = confidence
            if outcome is not None:
                cell["outcome"] = outcome
            if generic_error is not None:
                cell["error"] = generic_error
                cell["error_code"] = generic_error_code
            if name in field_errors and isinstance(field_errors[name], str):
                cell["error"] = safe_error(
                    "model_error", field_errors[name], max_chars=500
                ).detail
                cell["error_code"] = "model_error"
            if (
                not value_was_returned
                and managed_publication
                and generic_error is None
                and name not in field_errors
            ):
                if name not in required_output_field_names:
                    continue
                missing = safe_error(
                    "invalid_output",
                    f"recipe did not return declared output field {name!r}",
                    max_chars=500,
                )
                cell["error"] = missing.detail
                cell["error_code"] = missing.code
                cell["outcome"] = "invalid_output"
            just = (data or {}).get(f"{name}_justification")
            if just is None:
                just = generic_justification
            if just and not name.endswith("_justification"):
                cell["justification"] = just
            if grounding_sidecar is not None:
                cell["justification"] = GROUNDING_SIDECAR_PREFIX + json.dumps(
                    grounding_sidecar,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            if cell.get("error") is not None:
                if managed_publication:
                    cell["value"] = None
                cell["publication_effect"] = PUBLISH_ERROR
            elif cell["value"] is None:
                cell["publication_effect"] = PUBLISH_NULL
            elif value_was_returned or not managed_publication:
                cell["publication_effect"] = PUBLISH_VALUE
            else:  # pragma: no cover - missing values are converted to errors above
                raise AssertionError("unresolved output has no publication effect")
            out[name] = cell
        # Attribute row cost only once.
        for i, cell in enumerate(out.values()):
            if i > 0:
                cell["cost"] = 0.0
                cell["tokens_in"] = None
                cell["tokens_out"] = None
                cell.pop("model_calls", None)
        return out
    except RecipeInvocationHalt as e:
        persist_trace(error=safe_error(e.code, e, max_chars=500).text)
        raise
    except SandboxTeardownError as e:
        persist_trace(
            error=safe_error("sandbox_teardown_failed", e, max_chars=500).text
        )
        raise
    except StaleAttemptWriter as e:
        persist_trace(error=safe_error("stale_attempt_writer", e, max_chars=500).text)
        raise
    except HostedEngineError as e:
        persist_trace(error=safe_error(e.code, e, max_chars=500).text)
        if e.provider_job_accepted:
            # Preserve the reservation while an accepted provider job may finish.
            raise RecipeInvocationHalt(
                "external_effect_reconciliation_required",
                "The provider accepted this row as an asynchronous job, but "
                "the job did not return a consumable result; refusing another "
                "submission until the accepted job is reconciled.",
            ) from e
        # Only explicitly terminal codes are excluded from automatic backfill.
        failed = {
            name: {
                "value": None,
                "error": safe_error(e.code, e.message, max_chars=500).detail,
                "error_code": e.code,
                "publication_effect": PUBLISH_ERROR,
                "outcome": (
                    e.code if e.code in TERMINAL_FAILURE_OUTCOMES else "model_error"
                ),
            }
            for name in field_names
        }
        if e.accounting and field_names:
            failed[field_names[0]].update(e.accounting)
        return failed
    except (LLMError, CacheMiss, ValueError, RuntimeError) as e:
        persist_trace(error=safe_error("model_error", e, max_chars=500).text)
        if isinstance(e, LLMError) and e.post_egress_ambiguous:
            # Ambiguous egress keeps its reservation until reconciliation.
            raise RecipeInvocationHalt(
                "external_effect_reconciliation_required",
                "The provider may have accepted this row, but no response "
                "accounting was returned; refusing another call until the "
                "effect is reconciled.",
            ) from e
        throttle.backoff(e)
        model_name = spec.get("model") or ""
        provider = model_name.split("/", 1)[0] if model_name else None
        remediated = classify_resumable_provider_error(
            e, provider=provider
        ) or classify_llm_error(
            e,
            provider=provider,
            endpoint_origin=router.local_endpoint_url_for_model(model_name),
            model=model_name or None,
        )
        outcome = (
            remediated.code
            if remediated.code in ("invalid_output", "model_error")
            else "model_error"
        )
        failed = {
            name: {
                "value": None,
                "error": safe_error(
                    remediated.code, remediated.message, max_chars=500
                ).detail,
                "error_code": remediated.code,
                "publication_effect": PUBLISH_ERROR,
                "outcome": outcome,
            }
            for name in field_names
        }
        paid_wire_calls = list(getattr(e, "wire_calls", []) or [])
        if paid_wire_calls and field_names:
            failed[field_names[0]].update(
                _wire_accounting_meta(model_name, paid_wire_calls)
            )
        elif meta is not None and field_names:
            # Validation/postprocessing happens after a successful transport;
            # preserve its actual call and cost even when publication refuses
            # the returned value.
            failed[field_names[0]].update(meta)
        return failed


async def llm_row(
    router: Any,
    throttle: AdaptiveThrottle,
    recipe: Recipe,
    values: dict,
    spec: dict,
) -> tuple[dict, dict]:
    call = recipe.render(values, spec)
    model_name = spec["model"]
    wire_responses: list[LLMResponse]
    if call.schema is None:
        resp = await router.complete(
            LLMRequest(
                model=model_name,
                messages=call.messages,
                max_tokens=call.max_tokens,
            ),
            recipe_version=recipe.version,
        )
        row_data = resp.data
        wire_responses = [resp]
    else:
        result = await StructuredCompleter(router).complete(
            StructuredRequest(
                model=model_name,
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
                repair_attempts=3,
            ),
            recipe_version=recipe.version,
        )
        resp = result.response
        wire_responses = list(getattr(result, "wire_calls", []) or [])
        row_data = result.data
    throttle.decay()
    fact_responses = wire_responses or [resp]
    return row_data or {}, {
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
        **_wire_accounting_meta(model_name, fact_responses),
    }


def _wire_accounting_meta(
    model_name: str, wire_responses: list[LLMResponse]
) -> dict[str, Any]:
    """Build the run-cost and neutral fact payload for completed transports.

    Used by both successful rows and schema-exhausted rows: validation failure
    changes the cell outcome, not whether the provider call happened.
    """

    model_id = bare_model_name(model_name)
    calls: list[dict[str, Any]] = []
    for wire in wire_responses:
        units = {"tokens_in": wire.tokens_in, "tokens_out": wire.tokens_out}
        if wire.output_limited:
            units["output_limited"] = True
        if wire.cached:
            call = ModelCallMeta.cache_hit(
                capability="llm.complete",
                engine=model_name,
                provider=wire.provider,
                provider_kind="chat_api",
                model_ids=[model_id or model_name],
                units=units,
                warnings=[],
            ).as_dict()
        else:
            call = ModelCallMeta.provider_call(
                capability="llm.complete",
                engine=model_name,
                provider=wire.provider,
                provider_kind="chat_api",
                model_ids=[model_id or model_name],
                credential_source=wire.credential_source,
                provider_reported_cost_usd=wire.cost,
                provider_cost_usd=wire.cost,
                units=units,
                cost_source=wire.cost_source,
                warnings=[],
                duration_ms=wire.duration_ms,
            ).as_dict()
        calls.append(call)
    live_calls = [wire for wire in wire_responses if not wire.cached]
    if not live_calls:
        cost: float | None = 0.0
    elif any(wire.cost is None for wire in live_calls):
        cost = None
    else:
        cost = sum(wire.cost for wire in live_calls)
    return {
        "tokens_in": sum(wire.tokens_in for wire in wire_responses),
        "tokens_out": sum(wire.tokens_out for wire in wire_responses),
        "cost": cost,
        "model_calls": calls,
    }
