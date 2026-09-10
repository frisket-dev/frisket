"""Grouped-summary model checkpoints, atomic publication, and receipts."""

from __future__ import annotations

import hashlib
import json
import logging
from functools import partial
from typing import Any

from frisket.local_model_ids import bare_model_name

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_families._errors import (
    receipt_stale_replay_error,
)
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.group_summary_plan import GroupSummaryPlan
from frisket.engine.executor.action_receipts import (
    _positive_ref_int,
    _receipt_ref,
    _result_from_receipt,
)
from frisket.engine.executor.action_reservations import (
    _receipt_for_idempotency,
    _running_receipt_stale_result,
)
from frisket.engine.executor.action_support import (
    _failed_result,
    _model_call_provider_use,
)
from frisket.engine.executor.project_run_terminalization import (
    CurrentWriterTerminalAuthority,
    terminalize_project_run,
)
from frisket.engine.runner.publication import (
    PUBLISH_ERROR,
    PUBLISH_NULL,
    PUBLISH_VALUE,
)
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.ai.models.metadata import model_calls_cost_actual
from frisket.engine.store.materialization import (
    AggregateColumnSpec,
    AggregateMaterializedRow,
    AggregateSheetPlan,
    active_materialized_row_sources,
    load_materialized_row_sources_for_op,
    materialized_row_sources_ref_matches,
    write_aggregate_sheet,
)
from frisket.ai.llm import LLMRequest, ModelRouter
from frisket.ops.group_summary import (
    estimate_group_summary,
    group_summary_messages,
    group_summary_rows,
)
from frisket.ai.models.metadata import ModelCallMeta
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import StaleAttemptWriter
from frisket.operability.trace import TraceWriter, TracingRouter


logger = logging.getLogger("frisket.executor")


def _reduce_group_summary_result_from_existing_receipt(
    project: Project,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _TypedProjectEnvelope,
) -> ActionResult:
    if existing["params_hash"] != params_hash:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="idempotency_conflict",
                message="idempotency_key was already used with different normalized params",
                action_kind=action.kind,
                field="idempotency_key",
            ),
        )
    stale = _running_receipt_stale_result(
        project,
        existing,
        params_hash=params_hash,
        project_id=project_id,
        action=action,
    )
    if stale is not None:
        return stale
    if existing["status"] == "running":
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="idempotency_in_progress",
                message="idempotency_key is already reserved by a running reduce.group_summary action",
                action_kind=action.kind,
                field="idempotency_key",
                details={"receipt_id": existing["id"]},
            ),
        )
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    replay_error = _reduce_group_summary_replay_error(project, receipt)
    if replay_error is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=replay_error,
        )
    return _result_from_receipt(receipt)


def _reduce_group_summary_replay_error(
    project: Project,
    receipt: Receipt,
) -> ActionError | None:
    stale = partial(receipt_stale_replay_error, receipt)
    replay = "reduce.group_summary replay "
    rows_ref = _receipt_ref(receipt, "materialized_rows")
    groups_ref = _receipt_ref(receipt, "reduce_group_summary_groups")
    membership_ref = _receipt_ref(receipt, "materialized_row_sources")
    if rows_ref is None or groups_ref is None or membership_ref is None:
        return stale(replay + "receipt lacks materialized refs")
    op_id = _positive_ref_int(rows_ref, "op_id")
    sheet_id = _positive_ref_int(rows_ref, "sheet_id")
    row_ids = _positive_int_list(rows_ref.get("row_ids"))
    if op_id is None or sheet_id is None or row_ids is None:
        return stale(replay + "materialized refs are invalid")
    if membership_ref.get("op_id") != op_id:
        return stale(replay + "membership op ref changed", op_id=op_id)
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    if sheet is None:
        return stale(
            replay + "materialized sheet is missing or hidden", sheet_id=sheet_id
        )
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows "
            f"WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
            [sheet_id, *row_ids],
        ).fetchall()
        if len(rows) != len(row_ids) or any(
            row["parent_row_id"] is not None for row in rows
        ):
            return stale(replay + "aggregate row parentage changed", row_ids=row_ids)
    live_membership = load_materialized_row_sources_for_op(
        project.db,
        op_id=op_id,
        roles=("aggregate_source",),
    )
    if not materialized_row_sources_ref_matches(membership_ref, live_membership):
        return stale(replay + "materialized row sources changed", op_id=op_id)
    groups = groups_ref.get("groups")
    if not isinstance(groups, list):
        return stale(replay + "group membership is invalid", op_id=op_id)
    expected_membership: dict[int, set[int]] = {}
    for group in groups:
        if not isinstance(group, dict):
            return stale(replay + "group membership is invalid", op_id=op_id)
        summary_row_id = group.get("summary_row_id")
        source_row_ids = _positive_int_list(group.get("source_row_ids"))
        if (
            not isinstance(summary_row_id, int)
            or isinstance(summary_row_id, bool)
            or summary_row_id <= 0
            or source_row_ids is None
        ):
            return stale(replay + "group membership is invalid", op_id=op_id)
        expected_membership[summary_row_id] = set(source_row_ids)
    active_membership: dict[int, set[int]] = {}
    for membership in active_materialized_row_sources(
        project.db,
        materialized_row_ids=row_ids,
        roles=("aggregate_source",),
    ):
        active_membership.setdefault(int(membership["materialized_row_id"]), set()).add(
            int(membership["source_row_id"])
        )
    if active_membership != expected_membership:
        return stale(replay + "active membership changed", op_id=op_id)
    return None


def _resolve_reduce_group_summary_inputs(
    project: Project,
    params: GroupSummaryPlan,
) -> dict[str, Any] | ActionError:
    sheet = project.db.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (params.sheet_id,)
    ).fetchone()
    if sheet is None:
        return ActionError(
            code="invalid_input_ref",
            message="reduce.group_summary sheet_id does not identify a visible sheet",
            action_kind=params.action_kind,
            field="scope.sheet_id",
        )

    columns = {row["name"]: row for row in project.columns(params.sheet_id)}
    needed = list(
        dict.fromkeys(
            [*params.input_columns, *([params.group_by] if params.group_by else [])]
        )
    )
    missing = [name for name in needed if name not in columns]
    if missing:
        return ActionError(
            code="invalid_input_ref",
            message="Group summary source column does not exist",
            action_kind=params.action_kind,
            field="params.group_by"
            if params.group_by in missing
            and not any(name in params.input_columns for name in missing)
            else "params.source",
            details={"missing": missing},
        )

    if params.row_ids is not None:
        placeholders = ",".join("?" for _ in params.row_ids)
        row_records = project.db.execute(
            f"SELECT id FROM rows WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
            [params.sheet_id, *params.row_ids],
        ).fetchall()
        found = {int(row["id"]) for row in row_records}
        missing_rows = sorted(set(params.row_ids) - found)
        if missing_rows:
            return ActionError(
                code="invalid_input_ref",
                message="reduce.group_summary row_ids must belong to the target sheet",
                action_kind=params.action_kind,
                field="scope.row_ids",
                details={"missing": missing_rows},
            )
        row_ids = list(params.row_ids)
    else:
        row_ids = project.visible_row_ids(params.sheet_id)
    if not row_ids:
        return ActionError(
            code="invalid_input_ref",
            message="reduce.group_summary requires at least one visible source row",
            action_kind=params.action_kind,
            field="scope.row_ids",
        )

    values_by_column = {
        name: project.get_values(
            params.sheet_id, int(columns[name]["id"]), row_ids=row_ids
        )
        for name in needed
    }
    groups = group_summary_rows(
        {
            row_id: {
                name: values.get(row_id) for name, values in values_by_column.items()
            }
            for row_id in row_ids
        },
        source=params.input_columns,
        group_by=params.group_by,
    )

    source_columns = {
        name: {
            "column_id": int(columns[name]["id"]),
            "type": str(columns[name]["type"]),
            "ai_generated": bool(columns[name]["ai_generated"]),
            "current_run_id": columns[name]["current_run_id"],
        }
        for name in params.input_columns
    }
    group_column = None
    if params.group_by is not None:
        group_source = columns[params.group_by]
        group_column = {
            "name": params.group_by,
            "column_id": int(group_source["id"]),
            "type": str(group_source["type"]),
            "ai_generated": bool(group_source["ai_generated"]),
            "current_run_id": group_source["current_run_id"],
        }
    return {
        "sheet_id": int(sheet["id"]),
        "sheet_name": str(sheet["name"]),
        "source_row_ids": row_ids,
        "source_columns": source_columns,
        "group_column": group_column,
        "groups": groups,
    }


def _precheck_reduce_group_summary_target(
    project: Project,
    params: GroupSummaryPlan,
) -> ActionError | None:
    existing = project.db.execute(
        "SELECT id FROM sheets WHERE name=?",
        (params.target_sheet_name,),
    ).fetchone()
    if existing is None:
        return None
    return ActionError(
        code="duplicate_sheet_name",
        message="reduce.group_summary target sheet name already exists",
        action_kind=params.action_kind,
        field="sheet_name",
        details={
            "sheet_id": int(existing["id"]),
            "name": params.target_sheet_name,
        },
    )


def _reduce_group_summary_messages(
    params: GroupSummaryPlan,
    group: dict[str, Any],
) -> list[dict[str, str]]:
    return group_summary_messages(params.instruction, group)


def _estimate_reduce_group_summary_cost(
    params: GroupSummaryPlan,
    resolved: dict[str, Any],
) -> dict[str, Any]:
    resolved_messages = [
        _reduce_group_summary_messages(params, group) for group in resolved["groups"]
    ]
    return estimate_group_summary(params.model, resolved_messages)


#: Effect-checkpoint family coordinates. The group
#: key is derived from the normalized params — stable across the crash-retry
#: of one logical action, which mints a NEW receipt/run — and each group's
#: unit identity binds the exact provider request, so changed source data
#: refuses rather than substituting a stale paid response.
_REDUCE_EFFECT_FAMILY = "reduce_group_summary"
_REDUCE_EFFECT_KIND = "reduce.group_summary"


def _reduce_group_summary_effect_checkpoints_active(
    params: GroupSummaryPlan,
    router: ModelRouter,
) -> bool:
    """The family's paidness gate: checkpoint only work that can spend.

    Mirrors ``row_effect_spends_or_meters``'s LLM branch and reads the same
    one authority: strict replay proves nothing unless a cache is attached
    (``model_call_cannot_go_live``), because a cacheless ``replay_strict``
    router falls through to the live adapter and bills every group. A
    local-provider model (ollama) spends nothing — and a free run must
    produce zero durable checkpoint writes."""

    from frisket.ai.llm.router import model_call_cannot_go_live
    from frisket.ai.models.metadata import is_remote_provider

    if model_call_cannot_go_live(router):
        return False
    provider = params.model.split("/", 1)[0]
    return is_remote_provider(provider)


def _reduce_group_summary_effect_group_key(params: GroupSummaryPlan) -> str:
    payload = params.model_dump(mode="json", exclude_none=True)
    payload.pop("confirmed", None)
    payload.pop("consented_promise_set_hash", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "params:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reduce_group_summary_unit_plans(
    params: GroupSummaryPlan,
    resolved: dict[str, Any],
) -> list[dict[str, Any]]:
    """One checkpoint unit per group summary call: key, identity, messages."""

    plans: list[dict[str, Any]] = []
    for group in resolved["groups"]:
        messages = _reduce_group_summary_messages(params, group)
        unit_key = (
            "group:" + hashlib.sha256(str(group["name"]).encode("utf-8")).hexdigest()
        )
        identity_payload = {
            "schema_version": "frisket.reduce_group_summary_effect_identity.v1",
            "model": params.model,
            "action_version": "group_summary.v1",
            # The messages embed the group name, every source row id and every
            # input value, so a visibility/content change is a different
            # identity even when the group name is unchanged.
            "messages": messages,
        }
        identity = hashlib.sha256(
            json.dumps(
                identity_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        plans.append(
            {
                "group": group,
                "messages": messages,
                "unit_key": unit_key,
                "identity": identity,
            }
        )
    return plans


def _reduce_group_summary_prompt_hash(params: GroupSummaryPlan) -> str:
    return _text_hash(
        json.dumps(
            {
                "instruction": params.instruction,
                "input_columns": params.input_columns,
                "group_by": params.group_by,
            },
            sort_keys=True,
        )
    )


def _reduce_effect_reconciliation_error(message: str) -> ActionError:
    return ActionError(
        code="external_effect_reconciliation_required",
        message=message,
        action_kind=_REDUCE_EFFECT_KIND,
    )


def _reduce_group_summary_failure_computation(
    params: GroupSummaryPlan,
    router: ModelRouter,
    exc: Exception,
) -> dict[str, Any]:
    from frisket.ai.llm import (
        classify_llm_error,
        classify_resumable_provider_error,
    )

    provider = params.model.split("/", 1)[0]
    remediated = classify_resumable_provider_error(
        exc, provider=provider
    ) or classify_llm_error(
        exc,
        provider=provider,
        endpoint_origin=router.local_endpoint_url_for_model(params.model),
        model=params.model,
    )
    return {
        "summary": None,
        "error": remediated.message[:500],
        "error_code": remediated.code,
        "error_details": remediated.details,
        "tokens_in": None,
        "tokens_out": None,
        "cost": 0.0,
        "model_call": None,
    }


async def _complete_reduce_group_summaries(
    project: Project,
    params: GroupSummaryPlan,
    resolved: dict[str, Any],
    *,
    router: ModelRouter,
) -> list[dict[str, Any]] | ActionError:
    """Per-group provider calls, under effect-checkpoint authority when paid.

    This loop used to buy N provider responses and
    persist nothing until the result transaction after it — process death
    re-bought every group on retry, and the facts carried NULL attempt_id.
    A paid run now (1) establishes its durable accounting envelope (op +
    run + claimed execution attempt) BEFORE the first call, (2) reserves one
    checkpoint per group immediately before its egress, and (3) completes
    each checkpoint — provider fact, project-key cap spend, replay payload —
    in one transaction immediately after that group's response returns.
    Free/local/cached-strict-replay runs take the unfenced loop: zero durable
    checkpoint writes at $0 stakes."""

    if not _reduce_group_summary_effect_checkpoints_active(params, router):
        return await _complete_reduce_group_summaries_unfenced(
            project, params, resolved, router=router
        )
    return await _complete_reduce_group_summaries_checkpointed(
        project, params, resolved, router=router
    )


def _reduce_group_summary_trace_writer(
    project: Project, params: GroupSummaryPlan, run_id: int
) -> TraceWriter | None:
    try:
        return TraceWriter.open(
            project.path,
            run_id,
            action_kind=params.action_kind,
            model=params.model,
        )
    except Exception:  # noqa: BLE001 - tracing never owns the action outcome
        logger.warning("could not initialize group-summary trace")
        return None


def _write_reduce_group_summary_traces(
    project: Project,
    params: GroupSummaryPlan,
    run_id: int,
    row_ids: list[int] | None,
    computations: list[dict[str, Any]],
) -> None:
    try:
        if row_ids is None:
            pending = [
                (None, record)
                for computation in computations
                if isinstance(record := computation.get("_trace_record"), dict)
                and record.get("error") is not None
            ]
        else:
            pending = [
                (row_id, computation.get("_trace_record"))
                for row_id, computation in zip(row_ids, computations, strict=True)
                if computation.get("_trace_record") is not None
            ]
    except Exception:  # noqa: BLE001 - tracing never owns the action outcome
        logger.warning("could not bind group-summary traces to output rows")
        return
    if not pending:
        return
    recorder = _reduce_group_summary_trace_writer(project, params, run_id)
    if recorder is None:
        return
    try:
        for row_id, record in pending:
            bound_record = dict(record)
            bound_record["row_id"] = row_id
            bound_record["trace_id"] = recorder.trace_id
            recorder.write_row(bound_record)
    except Exception:  # noqa: BLE001 - tracing never owns the action outcome
        logger.warning("could not write group-summary traces")


async def _complete_reduce_group_summary_call(
    router: ModelRouter,
    recorder: TraceWriter | None,
    request: LLMRequest,
    trace_record: list[dict[str, Any] | None],
) -> Any:
    traced_router: Any = router
    row_trace = None
    if recorder is not None:
        try:
            row_trace = recorder.row_trace(None)
            traced_router = TracingRouter(router, row_trace)
        except Exception:  # noqa: BLE001 - preserve the provider call
            logger.warning("could not initialize group-summary call trace")
            row_trace = None
            traced_router = router
    try:
        response = await traced_router.complete(
            request, recipe_version="group_summary.v1"
        )
    except Exception as exc:
        if recorder is not None and row_trace is not None:
            trace_record[0] = traced_router.record(error=str(exc))
        raise
    if recorder is not None and row_trace is not None:
        trace_record[0] = traced_router.record(
            data=response.content if response.content is not None else response.data,
            meta={
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "cost": response.cost,
            },
        )
    return response


async def _complete_reduce_group_summaries_unfenced(
    project: Project,
    params: GroupSummaryPlan,
    resolved: dict[str, Any],
    *,
    router: ModelRouter,
) -> list[dict[str, Any]] | ActionError:
    from frisket.execution.attempt import AttemptClaimRefused, set_attempt_state
    from frisket.execution.attempt_authority import mint_direct_effect_attempt

    # Free/local calls need no replay checkpoint, but they still disclose
    # source data and later write run-scoped cells/facts.  Establish the same
    # durable dispatch claim before the first call; only the checkpoint
    # representation is omitted.
    estimate = _estimate_reduce_group_summary_cost(params, resolved)
    op_id = project.append_op(
        params.action_kind,
        {
            "kind": params.action_kind,
            "request": params.request,
        },
        label=f"reduce.group_summary {params.target_sheet_name}",
    )
    run_store = RunResultStore(project)
    run_id = run_store.start_run(
        op_id,
        int(resolved["sheet_id"]),
        params.action_kind,
        model=params.model,
        prompt_hash=_reduce_group_summary_prompt_hash(params),
        params={},
        total_rows=len(resolved["groups"]),
        cost_estimate=estimate["cost"],
    )
    try:
        commitment = mint_direct_effect_attempt(
            project,
            action_kind=params.action_kind,
            run_id=run_id,
            scope=tuple(int(row_id) for row_id in resolved["source_row_ids"]),
        )
    except AttemptClaimRefused as exc:
        return ActionError(
            code="model_run_failed",
            message=str(exc),
            action_kind=params.action_kind,
            details={"retryable": True},
        )
    envelope = {
        "op_id": op_id,
        "run_id": run_id,
        "attempt_id": commitment.attempt_id,
    }
    recorder = _reduce_group_summary_trace_writer(project, params, run_id)
    computations: list[dict[str, Any]] = []
    try:
        for group in resolved["groups"]:
            messages = _reduce_group_summary_messages(params, group)
            request = LLMRequest(
                model=params.model,
                messages=messages,
            )
            trace_record: list[dict[str, Any] | None] = [None]
            try:
                response = await _complete_reduce_group_summary_call(
                    router,
                    recorder,
                    request,
                    trace_record,
                )
            except Exception as exc:  # noqa: BLE001 - per-group failure is persisted
                computations.append(
                    {
                        "group": group,
                        **_reduce_group_summary_failure_computation(
                            params, router, exc
                        ),
                        "direct_effect": envelope,
                        "_trace_record": trace_record[0],
                    }
                )
                continue
            summary = response.content
            if summary is None:
                summary = json.dumps(response.data or {}, sort_keys=True)
            effective_cost = 0.0 if response.cached else response.cost
            computations.append(
                {
                    "group": group,
                    "summary": summary,
                    "error": None,
                    "tokens_in": response.tokens_in,
                    "tokens_out": response.tokens_out,
                    "cost": effective_cost,
                    "model_call": _reduce_group_summary_model_call(
                        params.model, response
                    ),
                    "direct_effect": envelope,
                    "_trace_record": trace_record[0],
                }
            )
    except BaseException:
        set_attempt_state(project, commitment.attempt_id, "halted")
        raise
    return computations


async def _complete_reduce_group_summaries_checkpointed(
    project: Project,
    params: GroupSummaryPlan,
    resolved: dict[str, Any],
    *,
    router: ModelRouter,
) -> list[dict[str, Any]] | ActionError:
    from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
    from frisket.execution.attempt import AttemptClaimRefused, set_attempt_state
    from frisket.execution.attempt_authority import mint_direct_effect_attempt

    store = EffectCheckpointStore(project.db)
    run_store = RunResultStore(project)
    group_key = _reduce_group_summary_effect_group_key(params)
    plans = _reduce_group_summary_unit_plans(params, resolved)
    plan_units = {plan["unit_key"] for plan in plans}
    existing = {
        checkpoint["unit_key"]: checkpoint
        for checkpoint in store.list_group(
            family=_REDUCE_EFFECT_FAMILY, group_key=group_key
        )
    }

    # Recovery decision tree, BEFORE any provider call or envelope mint: an
    # ambiguous or drifted unit refuses the whole action by name.
    stray = sorted(set(existing) - plan_units)
    if stray:
        return _reduce_effect_reconciliation_error(
            "a prior reduce.group_summary invocation recorded paid effects for "
            "groups this request no longer produces; refusing to abandon those "
            "effects or call the provider again until they are reconciled"
        )
    for plan in plans:
        checkpoint = existing.get(plan["unit_key"])
        if checkpoint is None:
            continue
        if (
            checkpoint["action_kind"] != params.action_kind
            or checkpoint["identity"] != plan["identity"]
        ):
            return _reduce_effect_reconciliation_error(
                "a prior group-summary effect was reserved for a different "
                "request or source value; refusing to substitute the current "
                "group or call the provider again"
            )
        if checkpoint["state"] == "reserved":
            return _reduce_effect_reconciliation_error(
                "a prior process may have reached the provider for this group, "
                "but no response was durably recorded; refusing another call "
                "until the effect is reconciled"
            )
        if checkpoint["state"] != "returned":
            return _reduce_effect_reconciliation_error(
                "a group-summary effect checkpoint has an invalid state; "
                "refusing another provider call until it is reconciled"
            )
        payload = checkpoint["payload"]
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("computation"), dict)
            or not isinstance(payload.get("effect"), dict)
        ):
            return _reduce_effect_reconciliation_error(
                "a returned group-summary effect checkpoint is incomplete or "
                "corrupt; refusing another provider call until it is reconciled"
            )

    # The durable accounting envelope: reused from the crashed invocation's
    # checkpoints when one exists (facts and spend already live on that run),
    # minted fresh — BEFORE any egress — otherwise.
    envelope: dict[str, Any] | None = None
    for checkpoint in existing.values():
        effect = (checkpoint["payload"] or {}).get("effect") or {}
        run_id = effect.get("run_id")
        op_id = effect.get("op_id")
        if (
            isinstance(run_id, int)
            and isinstance(op_id, int)
            and run_store.get_run(run_id) is not None
        ):
            envelope = {
                "op_id": op_id,
                "run_id": run_id,
                "attempt_id": effect.get("attempt_id"),
            }
            break
        return _reduce_effect_reconciliation_error(
            "a returned group-summary effect checkpoint no longer names a "
            "durable accounting run; refusing another provider call until it "
            "is reconciled"
        )
    if envelope is None:
        estimate = _estimate_reduce_group_summary_cost(params, resolved)
        op_id = project.append_op(
            params.action_kind,
            dict(params.request),
            label=f"reduce.group_summary {params.target_sheet_name}",
        )
        run_id = run_store.start_run(
            op_id,
            int(resolved["sheet_id"]),
            params.action_kind,
            model=params.model,
            prompt_hash=_reduce_group_summary_prompt_hash(params),
            params={},
            total_rows=len(plans),
            cost_estimate=estimate["cost"],
        )
        envelope = {"op_id": op_id, "run_id": run_id, "attempt_id": None}

    minted_attempt_id: str | None = None
    if plans:
        try:
            commitment = mint_direct_effect_attempt(
                project,
                action_kind=params.action_kind,
                run_id=int(envelope["run_id"]),
                scope=tuple(int(row_id) for row_id in resolved["source_row_ids"]),
            )
        except AttemptClaimRefused as exc:
            return ActionError(
                code="model_run_failed",
                message=str(exc),
                action_kind=params.action_kind,
                details={"retryable": True},
            )
        minted_attempt_id = commitment.attempt_id
        envelope["attempt_id"] = minted_attempt_id

    # The attempt covers both provider dispatch and the later project-result
    # transaction. It must remain dispatching until that transaction commits:
    # otherwise the effect-site fence would correctly reject its own writer.
    try:
        computations = await _reduce_group_summary_checkpointed_loop(
            params,
            plans,
            existing,
            envelope=envelope,
            group_key=group_key,
            store=store,
            run_store=run_store,
            router=router,
            recorder=_reduce_group_summary_trace_writer(
                project, params, int(envelope["run_id"])
            ),
        )
    except StaleAttemptWriter:
        raise
    except BaseException:
        if minted_attempt_id is not None:
            set_attempt_state(project, minted_attempt_id, "halted")
        raise
    if isinstance(computations, ActionError):
        if minted_attempt_id is not None:
            set_attempt_state(project, minted_attempt_id, "halted")
        return computations
    return computations


async def _reduce_group_summary_checkpointed_loop(
    params: GroupSummaryPlan,
    plans: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    *,
    envelope: dict[str, Any],
    group_key: str,
    store: Any,
    run_store: RunResultStore,
    router: ModelRouter,
    recorder: TraceWriter | None,
) -> list[dict[str, Any]] | ActionError:
    from frisket.engine.store.effect_checkpoints import require_model_call_ids

    computations: list[dict[str, Any]] = []
    for plan in plans:
        unit_effect = {
            "family": _REDUCE_EFFECT_FAMILY,
            "group_key": group_key,
            "unit_key": plan["unit_key"],
            "identity": plan["identity"],
            **envelope,
        }
        prior = existing.get(plan["unit_key"])
        if prior is not None:
            replayed = dict(prior["payload"]["computation"])
            replayed["group"] = plan["group"]
            replayed["effect"] = {**unit_effect, "checkpoint_id": prior["id"]}
            computations.append(replayed)
            continue

        checkpoint_id = (
            "reduce_effect_"
            + hashlib.sha256(
                f"{group_key}|{plan['unit_key']}".encode("utf-8")
            ).hexdigest()
        )
        created = store.reserve(
            checkpoint_id,
            family=_REDUCE_EFFECT_FAMILY,
            group_key=group_key,
            unit_key=plan["unit_key"],
            action_kind=params.action_kind,
            identity=plan["identity"],
            authorized_attempt_id=str(envelope["attempt_id"]),
            payload={"effect": envelope},
            run_id=int(envelope["run_id"]),
            writer_attempt_id=str(envelope["attempt_id"]),
            claimless_direct_effect=True,
        )
        if not created:
            return _reduce_effect_reconciliation_error(
                "a concurrent invocation reserved this group's effect first; "
                "refusing to race it with another provider call"
            )

        request = LLMRequest(
            model=params.model,
            messages=plan["messages"],
        )
        trace_record: list[dict[str, Any] | None] = [None]
        try:
            response = await _complete_reduce_group_summary_call(
                router,
                recorder,
                request,
                trace_record,
            )
        except Exception as exc:  # noqa: BLE001 - per-group failure is persisted
            # The call may have reached the provider; the failure is durable
            # truth for THIS request and replays as the same failed group on
            # resume.  It is retired with the result write, so a later fresh
            # action with the same params buys the group again normally.
            failure = _reduce_group_summary_failure_computation(params, router, exc)
            store.complete(
                checkpoint_id,
                family=_REDUCE_EFFECT_FAMILY,
                group_key=group_key,
                unit_key=plan["unit_key"],
                action_kind=params.action_kind,
                identity=plan["identity"],
                payload={"effect": envelope, "computation": failure},
                run_id=int(envelope["run_id"]),
                writer_attempt_id=str(envelope["attempt_id"]),
                claimless_direct_effect=True,
            )
            computation = {"group": plan["group"], **failure}
            computation["effect"] = {**unit_effect, "checkpoint_id": checkpoint_id}
            computation["_trace_record"] = trace_record[0]
            computations.append(computation)
            continue

        summary = response.content
        if summary is None:
            summary = json.dumps(response.data or {}, sort_keys=True)
        call = _reduce_group_summary_model_call(params.model, response)
        # The durable, deterministic call id minted at call time: replay
        # dedup (INSERT OR IGNORE by id) is what makes a crash-replayed
        # accounting batch charge the key exactly once.  The run id keeps it
        # unique per purchase — a later action that legitimately re-buys the
        # same content mints a distinct fact.
        call["id"] = f"reduce_group_summary_{int(envelope['run_id'])}_" + (
            hashlib.sha256(
                f"{group_key}|{plan['unit_key']}|{plan['identity']}".encode("utf-8")
            ).hexdigest()
        )
        call["run_id"] = int(envelope["run_id"])
        accounting_batch = [{"row_id": None, "column_id": None, "model_calls": [call]}]
        require_model_call_ids(accounting_batch)
        # The stored replay carries zero row cost and no fact: spend and the
        # provider fact become durable HERE, in the checkpoint transaction,
        # so the later result transaction can never book them again.
        replay_computation = {
            "summary": summary,
            "error": None,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "cost": 0.0,
            "model_call": None,
        }

        def accrue(
            checkpoint: dict[str, Any],
            *,
            _run_id: int = int(envelope["run_id"]),
            _batch: list[dict[str, Any]] = accounting_batch,
        ) -> float:
            return run_store.write_returned_call_accounting(
                _run_id,
                _batch,
                writer_attempt_id=str(envelope["attempt_id"]),
                claimless_direct_effect=True,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
                commit=False,
            )

        store.complete(
            checkpoint_id,
            family=_REDUCE_EFFECT_FAMILY,
            group_key=group_key,
            unit_key=plan["unit_key"],
            action_kind=params.action_kind,
            identity=plan["identity"],
            payload={"effect": envelope, "computation": replay_computation},
            accrue=accrue,
            run_id=int(envelope["run_id"]),
            writer_attempt_id=str(envelope["attempt_id"]),
            claimless_direct_effect=True,
        )
        computation = {"group": plan["group"], **replay_computation}
        computation["effect"] = {**unit_effect, "checkpoint_id": checkpoint_id}
        computation["_trace_record"] = trace_record[0]
        computations.append(computation)
    return computations


def _reduce_group_summary_model_call(model: str, response: Any) -> dict[str, Any]:
    model_id = bare_model_name(model)
    units = {"tokens_in": response.tokens_in, "tokens_out": response.tokens_out}
    if response.cached:
        return ModelCallMeta.cache_hit(
            capability="llm.complete",
            engine=model,
            provider=response.provider,
            provider_kind="chat_api",
            model_ids=[model_id or model],
            units=units,
            warnings=[],
        ).as_dict()
    return ModelCallMeta.provider_call(
        capability="llm.complete",
        engine=model,
        provider=response.provider,
        provider_kind="chat_api",
        model_ids=[model_id or model],
        credential_source=response.credential_source,
        provider_reported_cost_usd=response.cost,
        provider_cost_usd=response.cost,
        units=units,
        cost_source=response.cost_source,
        warnings=[],
        # The router already measured this live wire call; carry it by
        # value rather than reporting an honest-looking but false NULL.
        duration_ms=response.duration_ms,
    ).as_dict()


def _write_reduce_group_summary_result(
    project: Project,
    action: _TypedProjectEnvelope,
    params: GroupSummaryPlan,
    *,
    params_hash: str,
    project_id: str,
    action_id: str,
    receipt_id: str,
    resolved: dict[str, Any],
    estimate: dict[str, Any],
    computations: list[dict[str, Any]],
) -> ActionResult:
    # Effect-checkpointed computations carry their envelope (op/run/attempt)
    # and per-unit checkpoint coordinates; the write must reuse that envelope
    # and retire every checkpoint in the SAME transaction as the results.
    effects = [computation.get("effect") for computation in computations]
    direct_effects = [computation.get("direct_effect") for computation in computations]
    effect: dict[str, Any] | None = None
    if any(isinstance(item, dict) for item in effects):
        if not all(isinstance(item, dict) for item in effects):
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message=(
                        "reduce.group_summary computations mix checkpointed and "
                        "unfenced effects; refusing an inconsistent write"
                    ),
                    action_kind=action.kind,
                ),
            )
        effect = effects[0]
    direct_effect: dict[str, Any] | None = None
    if any(isinstance(item, dict) for item in direct_effects):
        if not all(isinstance(item, dict) for item in direct_effects):
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message=(
                        "reduce.group_summary computations mix claimed and "
                        "unclaimed direct effects; refusing an inconsistent write"
                    ),
                    action_kind=action.kind,
                ),
            )
        direct_effect = direct_effects[0]
    authority_effect = effect or direct_effect
    trace_run_id = (
        int(authority_effect["run_id"])
        if authority_effect is not None and authority_effect.get("run_id") is not None
        else None
    )
    traces_bound = False

    cur = project.db.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is None:
            project.db.rollback()
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message="reduce.group_summary idempotency reservation was lost",
                    action_kind=action.kind,
                ),
            )
        if existing["id"] != receipt_id:
            project.db.rollback()
            return _reduce_group_summary_result_from_existing_receipt(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
            )
        if existing["params_hash"] != params_hash:
            project.db.rollback()
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="idempotency_conflict",
                    message="idempotency_key was already used with different normalized params",
                    action_kind=action.kind,
                    field="idempotency_key",
                ),
            )
        duplicate = _precheck_reduce_group_summary_target(project, params)
        if duplicate is not None:
            project.db.rollback()
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=duplicate,
            )

        op_spec = dict(params.request)
        op_spec["params"] = dict(op_spec["params"])
        op_spec["params"].pop("confirmed", None)
        op_spec["params"].pop("consented_promise_set_hash", None)
        op_spec["params"]["params_hash"] = params_hash
        from frisket.actions.group_summary_types import GROUP_SUMMARY_COLUMNS

        physical_names = {
            "group": params.group_column_name,
            "rows": params.row_count_column_name,
            "summary": params.summary_column_name,
        }
        column_specs = [
            (physical_names[column.key], column.type, column.key == "summary")
            for column in GROUP_SUMMARY_COLUMNS
        ]
        write = write_aggregate_sheet(
            cur,
            AggregateSheetPlan(
                action_kind=params.action_kind,
                label=f"reduce.group_summary {params.target_sheet_name}",
                target_sheet_name=params.target_sheet_name,
                parent_sheet_id=int(resolved["sheet_id"]),
                op_spec=op_spec,
                columns=[
                    AggregateColumnSpec(
                        name=name,
                        type=type_name,
                        ai_generated=ai_generated,
                    )
                    for name, type_name, ai_generated in column_specs
                ],
                rows=[
                    AggregateMaterializedRow(
                        values={
                            params.group_column_name: computation["group"]["name"],
                            params.row_count_column_name: len(
                                computation["group"]["source_row_ids"]
                            ),
                        },
                        source_row_ids=computation["group"]["source_row_ids"],
                    )
                    for computation in computations
                ],
                reuse_op_id=(
                    int(authority_effect["op_id"])
                    if authority_effect is not None
                    else None
                ),
            ),
        )
        op_id = write.op_id
        summary_sheet_id = write.sheet_id
        column_ids = write.column_ids
        summary_row_ids = write.row_ids

        run_store = RunResultStore(project)
        if authority_effect is not None:
            # The accounting envelope was minted before egress;
            # repoint the SAME run at the materialized summary sheet and the
            # final op spec instead of minting a second run.
            run_id = int(authority_effect["run_id"])
            run_store.repoint_run_for_result(
                run_id,
                sheet_id=summary_sheet_id,
                params=op_spec["params"],
                total_rows=len(computations),
                cost_estimate=estimate["cost"],
            )
        else:
            run_id = run_store.start_run(
                op_id,
                summary_sheet_id,
                params.action_kind,
                model=params.model,
                prompt_hash=_reduce_group_summary_prompt_hash(params),
                params=op_spec["params"],
                total_rows=len(computations),
                cost_estimate=estimate["cost"],
                commit=False,
            )
        summary_column_id = column_ids[params.summary_column_name]
        claim_token = f"output-claim:{receipt_id}"
        output_field = {
            "name": params.summary_column_name,
            "column_type": "text",
        }
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=summary_sheet_id,
            output_names=[params.summary_column_name],
            action_kind=action.kind,
            receipt_id=receipt_id,
            run_id=run_id,
            op_id=op_id,
            claim_token=claim_token,
            details={"output_fields": [output_field]},
            commit=False,
        )
        if conflict is not None or len(claims) != 1:
            raise StaleAttemptWriter(
                "reduce.group_summary could not acquire its exact output claim"
            )
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
            expected_output_names=[params.summary_column_name],
            commit=False,
        )
        generations = ResultGenerationStore(project)
        generations.declare(
            run_id,
            summary_column_id,
            output_role=params.summary_column_name,
            compatibility_key=_compatibility_key(field=output_field),
            write_mode="create",
            claim_token=claim_token,
            commit=False,
        )
        result_batch: list[dict[str, Any]] = []
        failed_groups = 0
        for row_id, computation in zip(summary_row_ids, computations, strict=True):
            error = computation.get("error")
            if error:
                failed_groups += 1
            result: dict[str, Any] = {
                "row_id": row_id,
                "column_id": summary_column_id,
                "value": computation.get("summary"),
                "tokens_in": computation.get("tokens_in"),
                "tokens_out": computation.get("tokens_out"),
                "confidence": None,
                "justification": None,
                "error": error,
                "error_code": computation.get("error_code"),
                "cost": float(computation.get("cost") or 0.0),
                "publication_effect": (
                    PUBLISH_ERROR
                    if error
                    else (
                        PUBLISH_NULL
                        if computation.get("summary") is None
                        else PUBLISH_VALUE
                    )
                ),
            }
            call = computation.get("model_call")
            if isinstance(call, dict):
                call = dict(call)
                call["run_id"] = run_id
                call["row_id"] = row_id
                call["column_id"] = summary_column_id
                result["model_calls"] = [call]
            result_batch.append(result)
        run_store.write_results(
            run_id,
            result_batch,
            writer_attempt_id=(
                str(authority_effect["attempt_id"])
                if authority_effect is not None and authority_effect.get("attempt_id")
                else None
            ),
            authorized_attempt_id=(
                str(authority_effect["attempt_id"])
                if authority_effect is not None and authority_effect.get("attempt_id")
                else None
            ),
            claim_token=claim_token,
            claimless_direct_effect=False,
            commit=False,
        )
        run_status = "failed" if failed_groups == len(computations) else "completed"
        generations.seal(
            run_id,
            [summary_column_id],
            claim_token=claim_token,
            terminal_disposition=run_status,
            commit=False,
        )
        run_store.point_column_at_run(op_id, summary_column_id, run_id, commit=False)
        if effect is not None:
            # Retire every group's checkpoint in the SAME transaction as its
            # result: the summary can never commit while replay authority
            # survives, and a rollback keeps both for the next resume.
            from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

            checkpoint_store = EffectCheckpointStore(project.db)
            for computation in computations:
                unit_effect = computation["effect"]
                checkpoint_store.consume_and_retire(
                    str(unit_effect["checkpoint_id"]),
                    family=str(unit_effect["family"]),
                    group_key=str(unit_effect["group_key"]),
                    unit_key=str(unit_effect["unit_key"]),
                    action_kind=params.action_kind,
                    identity=str(unit_effect["identity"]),
                    run_id=run_id,
                    writer_attempt_id=str(effect["attempt_id"]),
                    claim_token=claim_token,
                    claimless_direct_effect=False,
                    commit=False,
                )
        action_status = _reduce_group_summary_action_status(
            failed_groups, len(computations)
        )
        outputs = _reduce_group_summary_outputs(
            params=params,
            op_id=op_id,
            run_id=run_id,
            summary_sheet_id=summary_sheet_id,
            column_ids=column_ids,
            summary_row_ids=summary_row_ids,
            computations=computations,
        )
        run = run_store.get_run(run_id)
        model_calls = run_store.model_calls(run_id)
        receipt = _reduce_group_summary_receipt(
            action=action,
            action_id=action_id,
            project_id=project_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            status=action_status,
            op_id=op_id,
            run_id=run_id,
            params=params,
            resolved=resolved,
            outputs=outputs,
            summary_sheet_id=summary_sheet_id,
            summary_row_ids=summary_row_ids,
            column_ids=column_ids,
            computations=computations,
            model_calls=model_calls,
            run=run,
            failed_groups=failed_groups,
            membership_ref=write.materialized_row_sources_ref,
        )
        writer_attempt_id = (
            str(authority_effect["attempt_id"])
            if authority_effect is not None and authority_effect.get("attempt_id")
            else None
        )
        if writer_attempt_id is None:
            raise StaleAttemptWriter(
                "reduce.group_summary terminal write supplied no writer attempt"
            )
        terminalization = terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id=receipt_id,
            status=action_status,
            run_status=run_status,
            authority=CurrentWriterTerminalAuthority(
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=False,
            ),
            terminal_receipt=receipt,
            receipt_source_statuses={"running"},
            commit=False,
        )
        if (
            terminalization.disposition != "terminalized"
            or terminalization.receipt_disposition != "updated"
            or terminalization.run_status != run_status
            or terminalization.receipt_status != action_status
        ):
            raise StaleAttemptWriter(
                terminalization.reason
                or (
                    "reduce.group_summary terminal tuple refused: "
                    f"{terminalization.disposition}/"
                    f"{terminalization.receipt_disposition}"
                )
            )
        project.db.commit()
        _write_reduce_group_summary_traces(
            project,
            params,
            run_id,
            summary_row_ids,
            computations,
        )
        traces_bound = True
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status=action_status,
            project_id=project_id,
            run_id=run_id,
            op_ids=[op_id],
            outputs=outputs,
            receipt_id=receipt_id,
            errors=receipt.errors,
            warnings=receipt.warnings,
        )
    except StaleAttemptWriter:
        project.db.rollback()
        raise
    except Exception:
        project.db.rollback()
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
        )
    finally:
        if not traces_bound and trace_run_id is not None:
            _write_reduce_group_summary_traces(
                project,
                params,
                trace_run_id,
                None,
                computations,
            )


def _reduce_group_summary_action_status(
    failed_groups: int,
    total_groups: int,
) -> str:
    if failed_groups >= total_groups:
        return "failed"
    if failed_groups:
        return "partial"
    return "completed"


def _reduce_group_summary_outputs(
    *,
    params: GroupSummaryPlan,
    op_id: int,
    run_id: int,
    summary_sheet_id: int,
    column_ids: dict[str, int],
    summary_row_ids: list[int],
    computations: list[dict[str, Any]],
) -> list[ActionOutput]:
    outputs = [
        ActionOutput(
            kind="sheet",
            name=params.target_sheet_name,
            sheet_id=summary_sheet_id,
            ref={
                "kind": "materialized_sheet",
                "sheet_id": summary_sheet_id,
                "op_id": op_id,
            },
        )
    ]
    for name, column_id in column_ids.items():
        kind = (
            "reduce_summary_output_column"
            if name == params.summary_column_name
            else "materialized_column"
        )
        ref = {
            "kind": kind,
            "sheet_id": summary_sheet_id,
            "column_id": column_id,
            "column_name": name,
            "op_id": op_id,
        }
        if kind == "reduce_summary_output_column":
            ref["run_id"] = run_id
            ref["row_ids"] = summary_row_ids
            ref["role"] = "summary"
        outputs.append(
            ActionOutput(
                kind="column",
                name=name,
                sheet_id=summary_sheet_id,
                column_id=column_id,
                row_ids=summary_row_ids
                if kind == "reduce_summary_output_column"
                else [],
                ref=ref,
            )
        )
    outputs.append(
        ActionOutput(
            kind="rows",
            name="rows",
            sheet_id=summary_sheet_id,
            row_ids=summary_row_ids,
            ref={
                "kind": "materialized_rows",
                "sheet_id": summary_sheet_id,
                "row_ids": summary_row_ids,
                "source_row_ids_by_group": [
                    computation["group"]["source_row_ids"]
                    for computation in computations
                ],
                "op_id": op_id,
            },
        )
    )
    return outputs


def _reduce_group_summary_receipt(
    *,
    action: _TypedProjectEnvelope,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    status: str,
    op_id: int,
    run_id: int,
    params: GroupSummaryPlan,
    resolved: dict[str, Any],
    outputs: list[ActionOutput],
    summary_sheet_id: int,
    summary_row_ids: list[int],
    column_ids: dict[str, int],
    computations: list[dict[str, Any]],
    model_calls: list[Any],
    run: Any,
    failed_groups: int,
    membership_ref: dict[str, Any],
) -> Receipt:
    inputs = [
        ReceiptIO(
            name="source_rows",
            ref={
                "kind": "reduce_group_summary_source_rows",
                "sheet_id": resolved["sheet_id"],
                "sheet_name": resolved["sheet_name"],
                "row_ids": resolved["source_row_ids"],
            },
        ),
        *[
            ReceiptIO(
                name=f"input_column.{name}",
                ref={
                    "kind": "reduce_group_summary_input_column",
                    "sheet_id": resolved["sheet_id"],
                    "column_id": info["column_id"],
                    "name": name,
                    "type": info["type"],
                    "ai_generated": info["ai_generated"],
                    "current_run_id": info["current_run_id"],
                },
            )
            for name, info in resolved["source_columns"].items()
        ],
    ]
    if resolved.get("group_column") is not None:
        group_column = resolved["group_column"]
        inputs.append(
            ReceiptIO(
                name=f"group_column.{group_column['name']}",
                ref={
                    "kind": "reduce_group_summary_group_column",
                    "sheet_id": resolved["sheet_id"],
                    **group_column,
                },
            )
        )
    output_refs = [
        ReceiptIO(name=output.name or output.kind, ref=output.ref) for output in outputs
    ]
    model_call_ids = [call["id"] for call in model_calls]
    receipt_cost_actual = model_calls_cost_actual(model_calls)
    evidence = [
        ReceiptEvidence(
            ref={
                "kind": "reduce_group_summary_groups",
                "sheet_id": resolved["sheet_id"],
                "summary_sheet_id": summary_sheet_id,
                "groups": [
                    {
                        "name": computation["group"]["name"],
                        "source_row_ids": computation["group"]["source_row_ids"],
                        "summary_row_id": summary_row_ids[idx],
                        "source_row_count": len(computation["group"]["source_row_ids"]),
                    }
                    for idx, computation in enumerate(computations)
                ],
            },
            retention="materialized",
        ),
        ReceiptEvidence(
            ref={
                "kind": "reduce_group_summary_output_roles",
                "columns": {
                    params.group_column_name: {
                        "column_id": column_ids[params.group_column_name],
                        "role": "group_key",
                    },
                    params.row_count_column_name: {
                        "column_id": column_ids[params.row_count_column_name],
                        "role": "source_row_count",
                    },
                    params.summary_column_name: {
                        "column_id": column_ids[params.summary_column_name],
                        "role": "generated_summary",
                        "run_id": run_id,
                    },
                },
            }
        ),
        ReceiptEvidence(
            ref={
                "kind": "reduce_group_summary_model_calls",
                "model_call_ids": model_call_ids,
                "model_call_count": len(model_call_ids),
                "failed_groups": failed_groups,
                "op_id": op_id,
                "run_id": run_id,
            },
            retention="pinned",
        ),
        ReceiptEvidence(
            ref={
                "kind": "reduce_group_summary_prompt",
                "instruction_hash": _text_hash(params.instruction),
                "model": params.model,
                "group_count": len(computations),
                "params_hash": params_hash,
                "op_id": op_id,
                "run_id": run_id,
            },
            retention="pinned",
        ),
        ReceiptEvidence(
            ref={
                "kind": "reduce_group_summary_run_counts",
                "total_groups": len(computations),
                "completed_groups": len(computations),
                "failed_groups": failed_groups,
                "cost_actual": receipt_cost_actual,
                "op_id": op_id,
                "run_id": run_id,
            }
        ),
        ReceiptEvidence(ref=membership_ref, retention="pinned"),
    ]
    errors = []
    if status == "failed":
        # Promote the typed resumable provider vocabulary (rate limit,
        # exhausted key, invalid key — frisket.llm.remediation
        # RESUMABLE_PROVIDER_ERROR_DETAILS) from per-group computations to the
        # receipt-level ActionError, but only when every group failed the same
        # way; mixed failures keep the generic aggregate below.
        from frisket.ai.llm import RESUMABLE_PROVIDER_ERROR_DETAILS

        typed_provider_errors = [
            computation
            for computation in computations
            if computation.get("error_code") in RESUMABLE_PROVIDER_ERROR_DETAILS
        ]
        typed_codes = {
            computation["error_code"] for computation in typed_provider_errors
        }
        if (
            computations
            and len(typed_provider_errors) == len(computations)
            and len(typed_codes) == 1
        ):
            first = typed_provider_errors[0]
            errors.append(
                ActionError(
                    code=str(first["error_code"]),
                    message=str(
                        first.get("error")
                        or "provider capacity is temporarily unavailable"
                    ),
                    action_kind=action.kind,
                    details=dict(first.get("error_details") or {}),
                )
            )
        else:
            errors.append(
                ActionError(
                    code="model_run_failed",
                    message="reduce.group_summary failed for every group",
                    action_kind=action.kind,
                    details={
                        "total_groups": len(computations),
                        "failed_groups": failed_groups,
                    },
                )
            )
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        run_id=run_id,
        op_ids=[op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status=status,
        inputs=inputs,
        outputs=output_refs,
        provider_use=_model_call_provider_use(
            model_calls,
            model=params.model,
            run=run,
        ),
        evidence=evidence,
        errors=errors,
    )


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _positive_int_list(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0
        for item in value
    ):
        return None
    return list(value)
