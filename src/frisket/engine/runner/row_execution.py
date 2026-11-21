"""Per-row execution for the map runner (extracted from
``MapRunner._execute_row``/``MapRunner._llm_row``/``MapRunner._maybe_throttle``):
render one row's model/program call, classify its failure into a typed cell
error, and adapt concurrency-wide throttle state to 429s. Operates on explicit
``project``/``router``/``throttle`` rather than an implicit ``self`` so leaf
callers (including ``preview``) need not import the engine. Also carries
``_await_owned_tasks`` (extracted from map_runner.py's module scope), the
small task-group helper both the engine's row-worker loop and preview's
per-row fan-out use to join sibling row tasks."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, cast

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
from frisket.ai.llm.types import ModelRouterTransport
from frisket.ai.models.accounting import merge_accounting_meta, wire_accounting_meta
from frisket.engine.store.writer_fence import WriterFence
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.base import ActionContext, ActionProgram, ActionProgramInvocationHalt
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
from frisket.execution.purchases import PurchaseRecordingError
from frisket.engine.store.runs import TERMINAL_FAILURE_OUTCOMES
from frisket.execution.attempt import StaleAttemptWriter, routed_admission_in_scope
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
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
    program: ActionProgram,
    values: dict,
    spec: dict,
    ctx: ActionContext,
    *,
    row_id: int | None = None,
    recorder: TraceWriter | None = None,
    output_field_names: tuple[str, ...] | None = None,
    required_output_field_names: frozenset[str] = frozenset(),
    managed_publication: bool = False,
) -> dict[str, dict]:
    from contextlib import nullcontext

    policy = getattr(router, "model_call_policy", None)
    capture = getattr(policy, "operation_scope", None)
    with capture(ctx, row_id=row_id) if capture is not None else nullcontext():
        return await _execute_row(
            project,
            router,
            throttle,
            program,
            values,
            spec,
            ctx,
            row_id=row_id,
            recorder=recorder,
            output_field_names=output_field_names,
            required_output_field_names=required_output_field_names,
            managed_publication=managed_publication,
        )


def _build_row_context(
    ctx: ActionContext,
    router: ModelRouterTransport,
    row_id: int | None,
    *,
    include_router: bool,
) -> ActionContext:
    extras = (
        {**ctx.extras, "router": router, "row_id": row_id}
        if include_router
        else {**ctx.extras, "row_id": row_id}
    )
    return ActionContext(
        project=ctx.project,
        http=ctx.http,
        extras=extras,
        credential_use_context=ctx.credential_use_context,
        execution_limits=ctx.execution_limits,
        cloud_execution=ctx.cloud_execution,
    )


def _returned_error_accounting(
    error: BaseException, spec: dict, ctx: ActionContext
) -> dict[str, Any] | None:
    materialized = getattr(error, "_row_returned_accounting", None)
    if materialized is not None:
        return materialized
    # Capability owners already assigned durable IDs. Only unconsumed wire
    # responses need row-level facts; using their raw history again charges
    # the same purchase twice under a different ID.
    recorded = getattr(error, "accounting", None)
    raw_wire_calls = getattr(error, "wire_calls", None)
    paid = (
        [item for item in raw_wire_calls if isinstance(item, LLMResponse)]
        if isinstance(raw_wire_calls, (list, tuple))
        else []
    )
    if not paid:
        observation = getattr(error, "observation", None)
        purchase = getattr(error, "purchase_request", None)
        if recorded is None and observation is not None and purchase is not None:
            from frisket.ai.models.metadata import ModelCallMeta

            expense = observation.provider_cost_micro
            cost = None if expense is None else expense / 1_000_000
            fact = ModelCallMeta.provider_call(
                capability=purchase.capability,
                engine=purchase.engine,
                provider=purchase.provider,
                provider_kind="local_http"
                if purchase.provider == "frisket-models"
                else "platform_api",
                credential_source=purchase.credential_source,
                provider_reported_cost_usd=cost,
                provider_cost_usd=cost,
                units=observation.usage,
                cost_source="provider_reported" if cost is not None else "unknown",
                duration_ms=None,
            ).as_dict()
            admission = routed_admission_in_scope(ctx.extras)
            if admission is not None:
                fact = bind_fact_to_route(admission.route, fact)
            recorded = {"model_calls": [fact], "cost": cost}
            # The same exception may cross preview/row/agent consumers.
            # Mint this fact once rather than charging it under new IDs.
            setattr(error, "accounting", recorded)
        setattr(error, "_row_returned_accounting", recorded)
        return recorded
    raw = wire_accounting_meta(spec.get("model") or paid[-1].model, paid)
    merged = merge_accounting_meta(recorded, raw)
    setattr(error, "_row_returned_accounting", merged)
    return merged


def _retain_returned_before_halt(
    project: Project,
    error: BaseException,
    spec: dict,
    ctx: ActionContext,
    row_id: int | None,
) -> None:
    accounting = _returned_error_accounting(error, spec, ctx)
    if accounting is None:
        return
    from frisket.execution.attempt import attempt_in_scope
    from frisket.engine.store.runs import RunResultStore

    attempt = attempt_in_scope(ctx.extras)
    if attempt is None:
        return
    claim_token = ctx.extras.get("claim_token")
    RunResultStore(project).write_returned_call_accounting(
        attempt.run_id,
        [{"row_id": row_id, "column_id": None, **accounting}],
        fence=WriterFence(
            run_id=attempt.run_id,
            writer_attempt_id=attempt.attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claim_token is None,
            authorized_attempt_id=attempt.attempt_id,
        ),
        receipt_id=attempt.receipt_id if attempt.run_id is None else None,
    )


@dataclass(frozen=True, slots=True)
class _PublicationPolicy:
    required_output_field_names: frozenset[str]
    managed_publication: bool
    grounding_active: bool


@dataclass(frozen=True, slots=True)
class _RowCall:
    program: ActionProgram
    values: dict
    spec: dict
    ctx: ActionContext
    row_id: int | None
    field_names: list[str]
    policy: _PublicationPolicy


@dataclass(frozen=True, slots=True)
class _ResponseEnvelope:
    confidence: Any
    outcome: str | None
    generic_justification: str | None
    generic_error: str | None
    generic_error_code: str | None
    field_errors: dict[str, Any]


async def _invoke_program_row(
    router: ModelRouterTransport,
    throttle: AdaptiveThrottle,
    call: _RowCall,
) -> tuple[Any, dict[str, Any], bool]:
    run_agent = getattr(call.program, "run_agent", None)
    if callable(run_agent):
        row_ctx = _build_row_context(call.ctx, router, call.row_id, include_router=True)
        agent_result = run_agent(call.values, call.spec, row_ctx, router)
        if inspect.isawaitable(agent_result):
            data, meta = await agent_result
            return data, meta, False
        raise TypeError("ActionProgram.run_agent must return an awaitable")
    if call.program.is_llm(call.spec):
        data, meta = await llm_row(
            router, throttle, call.program, call.values, call.spec
        )
        return data, meta, True
    # Clone context because parallel rows must not share router state.
    row_ctx = _build_row_context(
        call.ctx,
        router,
        call.row_id,
        include_router=call.ctx.extras.get("router") is not None,
    )
    data = await call.program.execute(call.values, call.spec, row_ctx)
    meta: dict[str, Any] = {"tokens_in": None, "tokens_out": None, "cost": 0.0}
    if isinstance(data, tuple) and len(data) == 2:
        data, returned_meta = data
        meta = {**meta, **(returned_meta or {})}
    return data, meta, False


def _extract_response_envelope(data: Any) -> _ResponseEnvelope:
    if not isinstance(data, dict):
        return _ResponseEnvelope(None, None, None, None, None, {})
    confidence = next(
        (v for k, v in data.items() if k.endswith("_confidence")),
        data.get("confidence"),
    )
    raw_outcome = data.get("outcome")
    raw_just = data.get("justification")
    raw_error = data.get("error")
    generic_error = None
    generic_error_code = None
    if isinstance(raw_error, str):
        raw_error_code = data.get("error_code")
        safe = safe_error(
            raw_error_code if isinstance(raw_error_code, str) else "model_error",
            raw_error,
            max_chars=500,
        )
        generic_error = safe.detail
        generic_error_code = safe.code
    raw_field_errors = data.get("__field_errors__")
    return _ResponseEnvelope(
        confidence=confidence,
        outcome=raw_outcome if isinstance(raw_outcome, str) else None,
        generic_justification=raw_just if isinstance(raw_just, str) else None,
        generic_error=generic_error,
        generic_error_code=generic_error_code,
        field_errors=raw_field_errors if isinstance(raw_field_errors, dict) else {},
    )


def _apply_cell_errors(
    cell: dict[str, Any],
    name: str,
    *,
    value_was_returned: bool,
    envelope: _ResponseEnvelope,
    policy: _PublicationPolicy,
) -> bool:
    if envelope.generic_error is not None:
        cell["error"] = envelope.generic_error
        cell["error_code"] = envelope.generic_error_code
    field_error = envelope.field_errors.get(name)
    if isinstance(field_error, str):
        cell["error"] = safe_error("model_error", field_error, max_chars=500).detail
        cell["error_code"] = "model_error"
    if (
        not value_was_returned
        and policy.managed_publication
        and envelope.generic_error is None
        and name not in envelope.field_errors
    ):
        if name not in policy.required_output_field_names:
            return False
        missing = safe_error(
            "invalid_output",
            f"action did not return declared output field {name!r}",
            max_chars=500,
        )
        cell["error"] = missing.detail
        cell["error_code"] = missing.code
        cell["outcome"] = "invalid_output"
    return True


def _apply_cell_effect(
    cell: dict[str, Any], *, value_was_returned: bool, managed_publication: bool
) -> None:
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


def _materialize_field_cell(
    call: _RowCall,
    name: str,
    data: Any,
    meta: dict[str, Any],
    envelope: _ResponseEnvelope,
) -> dict[str, Any] | None:
    policy = call.policy
    value_was_returned = isinstance(data, dict) and name in data
    val = data.get(name) if value_was_returned else None
    grounding_sidecar: dict[str, Any] | None = None
    if policy.grounding_active:
        val, grounding_sidecar = unwrap_grounded_value(name, val)
    if (
        val is not None
        and envelope.generic_error is None
        and name not in envelope.field_errors
    ):
        val = call.program.postprocess_value(name, val, call.spec)
    cell: dict[str, Any] = {"value": val, **meta}
    if envelope.confidence is not None:
        cell["confidence"] = envelope.confidence
    if envelope.outcome is not None:
        cell["outcome"] = envelope.outcome
    if not _apply_cell_errors(
        cell,
        name,
        value_was_returned=value_was_returned,
        envelope=envelope,
        policy=policy,
    ):
        return None
    just = (data or {}).get(f"{name}_justification") or envelope.generic_justification
    if just and not name.endswith("_justification"):
        cell["justification"] = just
    if grounding_sidecar is not None:
        cell["justification"] = GROUNDING_SIDECAR_PREFIX + json.dumps(
            grounding_sidecar,
            sort_keys=True,
            separators=(",", ":"),
        )
    _apply_cell_effect(
        cell,
        value_was_returned=value_was_returned,
        managed_publication=policy.managed_publication,
    )
    return cell


def _materialize_row_outputs(
    call: _RowCall,
    data: Any,
    meta: dict[str, Any],
) -> dict[str, dict]:
    envelope = _extract_response_envelope(data)
    out: dict[str, dict] = {}
    for name in call.field_names:
        cell = _materialize_field_cell(call, name, data, meta, envelope)
        if cell is not None:
            out[name] = cell
    # Attribute row cost only once.
    for i, cell in enumerate(out.values()):
        if i > 0:
            cell["cost"] = 0.0
            cell["tokens_in"] = None
            cell["tokens_out"] = None
            cell.pop("model_calls", None)
    return out


def _bind_error_route_accounting(e: HostedEngineError, ctx: ActionContext) -> None:
    if e.accounting is None:
        return
    from frisket.cloud_execution import CloudExecutionRefused

    admission = routed_admission_in_scope(ctx.extras)
    calls = e.accounting.get("model_calls")
    if admission is None or not calls:
        return
    if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
        raise ActionProgramInvocationHalt(
            "promise_violation",
            "Hosted failure accounting has invalid model-call facts.",
        ) from e
    bound_calls = []
    bind_unobserved = isinstance(e, CloudExecutionRefused)
    for call in calls:
        observation = call.get(ROUTE_OBSERVATION_KEY)
        if observation is None:
            bound_calls.append(
                bind_fact_to_route(admission.route, call) if bind_unobserved else call
            )
            continue
        if (
            not isinstance(observation, dict)
            or observation.get("route_id") != admission.route.id
            or not isinstance(observation.get("observed"), dict)
        ):
            raise ActionProgramInvocationHalt(
                "promise_violation",
                "Hosted failure accounting differs from its admitted route.",
            ) from e
        bound_calls.append(call)
    e.accounting = {
        **e.accounting,
        "model_calls": bound_calls,
    }


def _failed_cells(
    field_names: list[str],
    *,
    code: str,
    message: Any,
    outcome: str,
    accounting: dict[str, Any] | None = None,
) -> dict[str, dict]:
    failed = {
        name: {
            "value": None,
            "error": safe_error(code, message, max_chars=500).detail,
            "error_code": code,
            "publication_effect": PUBLISH_ERROR,
            "outcome": outcome,
        }
        for name in field_names
    }
    if accounting is not None and field_names:
        failed[field_names[0]].update(accounting)
    return failed


async def _execute_row(
    project: Project,
    router: ModelRouter,
    throttle: AdaptiveThrottle,
    program: ActionProgram,
    values: dict,
    spec: dict,
    ctx: ActionContext,
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
    from frisket.execution.dispatch_control import dispatch_for_context

    exec_router = cast(
        ModelRouterTransport, router.for_execution(dispatch_for_context(ctx))
    )
    # This public entry point may be called without prior spec validation.
    admission = routed_admission_in_scope(ctx.extras or {})
    validation.assert_network_policy(
        project,
        program,
        spec,
        selected_egress_class=(
            admission.route.egress_class if admission is not None else None
        ),
    )
    field_names = (
        list(output_field_names)
        if output_field_names is not None
        else [f["name"] for f in program.output_fields(spec)]
    )
    # Do not ask a plain LLM to fabricate output from an empty source row.
    unavailable = getattr(values, "unavailable_inputs", ())
    if unavailable:
        from frisket.engine.store.input_availability import not_completed_result

        return {name: not_completed_result(unavailable[0]) for name in field_names}
    if (
        program.is_llm(spec)
        and not hasattr(program, "run_agent")
        and not program.allow_all_empty_input
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
    tracing_router = TracingRouter(exec_router, row_trace) if row_trace else None
    active_router: ModelRouterTransport = (
        tracing_router if tracing_router is not None else exec_router
    )

    def persist_trace(
        *, data: Any = None, meta: dict | None = None, error: str | None = None
    ) -> None:
        if recorder and row_trace and tracing_router is not None:
            recorder.write_row(tracing_router.record(data=data, meta=meta, error=error))

    publication = _PublicationPolicy(
        required_output_field_names=required_output_field_names,
        managed_publication=managed_publication,
        grounding_active=grounding_enabled(spec),
    )
    row_call = _RowCall(
        program=program,
        values=values,
        spec=spec,
        ctx=ctx,
        row_id=row_id,
        field_names=field_names,
        policy=publication,
    )

    meta: dict[str, Any] | None = None
    try:
        data, meta, needs_model_normalize = await _invoke_program_row(
            active_router, throttle, row_call
        )
        if needs_model_normalize:
            data = program.normalize_model_output(data, spec, row_values=values)
        if isinstance(data, PreparedRowPublication):
            persist_trace(data=data.trace_data, meta=meta)
            return finalize_prepared_row_publication(data, field_names, meta)
        persist_trace(data=data, meta=meta)
        return _materialize_row_outputs(row_call, data, meta)
    except asyncio.CancelledError as exc:
        persist_trace(error="cancelled: row execution cancelled")
        accounting = _returned_error_accounting(exc, spec, ctx)
        if ctx.extras.get("preview"):
            if accounting is not None:
                setattr(exc, "accounting", accounting)
            # The preview owner retains usage and propagates cancellation;
            # durable runs instead return a non-publishing accounting row.
            raise
        if accounting is None or not field_names:
            raise
        returned = {
            name: {
                "value": None,
                "error": "Run stopped before the next model request.",
                "error_code": "cancelled",
                "outcome": "not_completed",
                "publication_effect": "retain",
            }
            for name in field_names
        }
        returned[field_names[0]].update(accounting)
        return returned
    except ActionProgramInvocationHalt as e:
        persist_trace(error=safe_error(e.code, e, max_chars=500).text)
        _retain_returned_before_halt(project, e, spec, ctx, row_id)
        raise
    except SandboxTeardownError as e:
        persist_trace(
            error=safe_error("sandbox_teardown_failed", e, max_chars=500).text
        )
        raise
    except StaleAttemptWriter as e:
        persist_trace(error=safe_error("stale_attempt_writer", e, max_chars=500).text)
        raise
    except PurchaseRecordingError as e:
        persist_trace(
            data=e.provider_result,
            meta=_returned_error_accounting(e, spec, ctx),
            error=safe_error(
                "external_effect_reconciliation_required", e, max_chars=500
            ).text,
        )
        _retain_returned_before_halt(project, e, spec, ctx, row_id)
        raise ActionProgramInvocationHalt(
            "external_effect_reconciliation_required",
            "The provider returned this row, but its accounting could not be saved. "
            "Reconcile the returned result before retrying; another call could charge twice.",
        ) from e
    except HostedEngineError as e:
        persist_trace(error=safe_error(e.code, e, max_chars=500).text)
        _bind_error_route_accounting(e, ctx)
        if e.provider_job_accepted or e.post_egress_ambiguous:
            _retain_returned_before_halt(project, e, spec, ctx, row_id)
            # Preserve the reservation while an accepted provider job may finish.
            raise ActionProgramInvocationHalt(
                "external_effect_reconciliation_required",
                "The provider may have accepted and billed this row, but "
                "the result is unresolved; refusing another submission "
                "until the provider request is reconciled.",
            ) from e
        # Only explicitly terminal codes are excluded from automatic backfill.
        return _failed_cells(
            field_names,
            code=e.code,
            message=e.message,
            outcome=e.code if e.code in TERMINAL_FAILURE_OUTCOMES else "model_error",
            accounting=e.accounting or None,
        )
    except (LLMError, CacheMiss, ValueError, RuntimeError) as e:
        persist_trace(error=safe_error("model_error", e, max_chars=500).text)
        if isinstance(e, LLMError) and e.post_egress_ambiguous:
            _retain_returned_before_halt(project, e, spec, ctx, row_id)
            # Ambiguous egress keeps its reservation until reconciliation.
            raise ActionProgramInvocationHalt(
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
        accounting = _returned_error_accounting(e, spec, ctx)
        # Validation/postprocessing happens after a successful transport;
        # preserve its actual call and cost even when publication refuses
        # the returned value.
        cell_accounting = accounting if accounting is not None else meta
        return _failed_cells(
            field_names,
            code=remediated.code,
            message=remediated.message,
            outcome=outcome,
            accounting=cell_accounting,
        )


async def llm_row(
    router: Any,
    throttle: AdaptiveThrottle,
    program: ActionProgram,
    values: dict,
    spec: dict,
) -> tuple[dict, dict]:
    call = program.render(values, spec)
    model_name = spec["model"]
    wire_responses: list[LLMResponse]
    if call.schema is None:
        resp = await router.complete(
            LLMRequest(
                model=model_name,
                messages=call.messages,
                max_tokens=call.max_tokens,
            ),
            recipe_version=program.version,
        )
        row_data = program.model_response_data(resp, spec)
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
            recipe_version=program.version,
        )
        resp = result.response
        wire_responses = list(getattr(result, "wire_calls", []) or [])
        row_data = result.data
    throttle.decay()
    fact_responses = wire_responses or [resp]
    return row_data or {}, {
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
        **wire_accounting_meta(model_name, fact_responses),
    }
