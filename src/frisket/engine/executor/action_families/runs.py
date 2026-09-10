"""Source-generation reconstruction and committed backfill facts."""

from __future__ import annotations

import json
import os
from typing import Any

from frisket.authoring.action_metadata import gated_capability_phrase
from frisket.contracts.action import (
    ActionError,
    ActionResultStatus,
    ActionSpec,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.ops.base import persisted_recipe_invocation_halt
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore
from frisket.engine.store.runs import (
    FAILURE_OUTCOMES,
    TERMINAL_FAILURE_OUTCOMES,
    outcome_sql_list,
)


def _run_backfill_terminal_projection(
    project: Project,
    action: ActionSpec,
    run_id: int,
    *,
    requested_row_ids: list[int],
    filled_row_ids: list[int],
) -> tuple[ActionResultStatus, list[ActionError]]:
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or str(run["status"] or "") != "cancelled":
        return "completed", []
    halt = persisted_recipe_invocation_halt(run["params"])
    if halt is None:
        return "cancelled", []
    code, detail = halt
    return "failed", [
        ActionError(
            code=code,
            message=detail,
            action_kind=action.kind,
            details={
                "run_id": run_id,
                # Backfill receipts describe this invocation's delta, not the
                # target run's cumulative history from earlier attempts.
                "completed_rows": len(filled_row_ids),
                "total_rows": len(requested_row_ids),
            },
        )
    ]


def _resolve_backfill_target(
    project: Project,
    *,
    sheet_id: int,
    column: str,
    row_ids: list[int] | None = None,
    confirmation: str | None = None,
) -> dict[str, Any] | ActionError:
    action_kind = "run.backfill"
    column_name = column.strip()
    column = next(
        (
            row
            for row in project.columns(sheet_id, include_hidden=True)
            if row["name"] == column_name
        ),
        None,
    )
    if column is None:
        return ActionError(
            code="column_not_found",
            message=f"no column '{column_name}'",
            action_kind=action_kind,
            field="params.column",
        )
    if not bool(column["ai_generated"]):
        return ActionError(
            code="column_not_ai_generated",
            message=f"column '{column_name}' is not AI-generated",
            action_kind=action_kind,
            field="params.column",
        )
    column_id = int(column["id"])
    all_row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position",
            (sheet_id,),
        )
    ]
    generation_store = ResultGenerationStore(project)
    heads = generation_store.read_cell_heads(column_id)
    if row_ids is not None:
        # An explicit retry inherits the action that produced those exact
        # cells. A scalar column pointer is only chronology after subset
        # publication and cannot identify their generation.
        requested = set(row_ids)
        missing = sorted(requested - set(all_row_ids))
        if missing:
            return ActionError(
                code="invalid_row_ref",
                message=(
                    "run.backfill row_ids must reference visible rows on the "
                    "target sheet"
                ),
                action_kind=action_kind,
                field="scope.row_ids",
                details={"invalid_rows": missing},
            )
        unrun_row_ids = [row_id for row_id in all_row_ids if row_id in requested]
        missing_heads = [row_id for row_id in unrun_row_ids if row_id not in heads]
        source_run_ids = sorted(
            {heads[row_id].run_id for row_id in unrun_row_ids if row_id in heads},
            reverse=True,
        )
        if missing_heads:
            return ActionError(
                code="backfill_source_unavailable",
                message=(
                    "run.backfill cannot reconstruct an action for rows without "
                    "published result heads"
                ),
                action_kind=action_kind,
                field="scope.row_ids",
                details={"missing_head_row_ids": missing_heads},
            )
    else:
        # An automatic sweep fills cells without successful heads. Its source
        # is representable only while the column has one active generation;
        # a mixed-origin column has no single action to apply to a new row.
        done_row_ids = {
            row_id
            for row_id, head in heads.items()
            if head.outcome not in FAILURE_OUTCOMES
        }
        unrun_row_ids = [row_id for row_id in all_row_ids if row_id not in done_row_ids]
        source_run_ids = generation_store.origin_run_ids(column_id, limit=2)
    if not source_run_ids:
        return ActionError(
            code="run_required",
            message=f"column '{column_name}' has no published generation; run it first",
            action_kind=action_kind,
            field="params.column",
        )
    if len(source_run_ids) != 1:
        return ActionError(
            code="mixed_origin_column_unsupported",
            message=(
                "run.backfill requires rows from one source generation; choose "
                "an exact row scope from a single generation"
            ),
            action_kind=action_kind,
            field="scope.row_ids" if row_ids is not None else "params.column",
            details={
                "column_id": column_id,
                "origin_run_ids": source_run_ids,
                "requested_row_ids": unrun_row_ids,
            },
        )
    run_id = source_run_ids[0]
    run_row = project.db.execute(
        "SELECT id, op_id, params FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if run_row is None:
        return ActionError(
            code="run_not_found",
            message="column source generation run was not found",
            action_kind=action_kind,
            field="params.column",
            details={"run_id": int(run_id)},
        )
    try:
        runner_spec = json.loads(run_row["params"] or "{}")
    except json.JSONDecodeError as exc:
        return ActionError(
            code="invalid_run_params",
            message="column source generation params are not valid JSON",
            action_kind=action_kind,
            field="params.column",
            details={"run_id": int(run_id), "error": str(exc)},
        )
    if not isinstance(runner_spec, dict):
        return ActionError(
            code="invalid_run_params",
            message="column source generation params must be an object",
            action_kind=action_kind,
            field="params.column",
            details={"run_id": int(run_id)},
        )
    runner_spec = dict(runner_spec)
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
        typed_request_hash,
    )

    try:
        original_bound = bound_typed_program_request_from_runner_spec(
            runner_spec, project=project
        )
    except (TypeError, ValueError) as exc:
        return ActionError(
            code="invalid_run_params",
            message=f"stored typed action parameters are invalid: {exc}",
            action_kind=action_kind,
            field="params.column",
        )
    if original_bound is not None:
        producer_kind = original_bound.action.action_id
        expected_hash = typed_request_hash(original_bound)
        producer_receipt = ReceiptStore(project).latest_for_run_action_statuses(
            run_id,
            producer_kind,
            FINISHED_RECEIPT_STATUSES,
        )
        matches_producer = (
            producer_receipt is not None
            and producer_receipt.params_hash == expected_hash
        )
        if not matches_producer:
            # Backfill can itself create the selected generation, including
            # through a custom action using RunBackfiller. Its host receipt
            # pins this exact successor program; do not infer it from an
            # action name, parent chain, or descriptive author return.
            successor = ReceiptStore(project).latest_for_run_statuses(
                run_id, FINISHED_RECEIPT_STATUSES
            )
            if successor is not None:
                try:
                    matches_producer = any(
                        evidence.ref.get("kind") == "backfill_source_generation"
                        and evidence.ref.get("successor_run_id") == run_id
                        and evidence.ref.get("successor_request_hash") == expected_hash
                        for evidence in successor.parsed().evidence
                    )
                except ValueError:
                    matches_producer = False
        if not matches_producer:
            return ActionError(
                code="invalid_run_params",
                message=(
                    "stored typed action identity does not match its producer receipt"
                ),
                action_kind=action_kind,
                field="params.column",
                details={"run_id": int(run_id)},
            )
        output_names = runner_spec.get("output_names")
        if (
            not isinstance(output_names, dict)
            or not output_names
            or not all(
                isinstance(logical, str) and logical and isinstance(role, str) and role
                for logical, role in output_names.items()
            )
            or len(output_names) != len(set(output_names.values()))
        ):
            return ActionError(
                code="invalid_run_params",
                message="stored typed action has no canonical output names",
                action_kind=action_kind,
                field="params.column",
            )
        bindings = generation_store.bindings_for_run(run_id)
        bindings_by_role = {binding.output_role: binding for binding in bindings}
        stored_roles = {str(name) for name in output_names.values()}
        if set(bindings_by_role) != stored_roles:
            return ActionError(
                code="invalid_run_params",
                message=(
                    "stored typed action outputs do not match its immutable "
                    "generation bindings"
                ),
                action_kind=action_kind,
                field="params.column",
                details={"run_id": int(run_id)},
            )
        columns_by_id = {
            int(item["id"]): item
            for item in project.columns(sheet_id, include_hidden=True)
        }
        missing_outputs = sorted(
            binding.column_id
            for binding in bindings
            if binding.column_id not in columns_by_id
        )
        if missing_outputs:
            return ActionError(
                code="backfill_source_unavailable",
                message="stored typed action output columns no longer exist",
                action_kind=action_kind,
                field="params.column",
                details={"column_ids": missing_outputs},
            )
        if column_id not in {binding.column_id for binding in bindings}:
            return ActionError(
                code="invalid_run_params",
                message="backfill target is not an output of its source generation",
                action_kind=action_kind,
                field="params.column",
                details={"run_id": int(run_id), "column_id": column_id},
            )
        stored_output_names = dict(output_names)
        output_names = {
            str(logical): str(
                columns_by_id[bindings_by_role[str(role)].column_id]["name"]
            )
            for logical, role in stored_output_names.items()
        }
        runner_spec["output_names"] = output_names
        runner_spec["output_target_preconditions"] = {
            output_names[str(logical)]: bindings_by_role[str(role)].column_id
            for logical, role in stored_output_names.items()
        }
        runner_spec["replace_existing"] = True
    runner_spec.pop("confirmed", None)
    # The claims-gate echo is per-invocation retry-flow
    # plumbing, exactly like ``confirmed`` above. The stored spec's echo
    # answered the ORIGINAL launch's claims; this invocation recompiles its
    # own, so replaying the old one can only ever be a stale echo — which the
    # gate correctly refuses, leaving a changed-claims backfill unconfirmable.
    # THIS call's echo, or none.
    runner_spec.pop("consented_promise_set_hash", None)
    if confirmation:
        runner_spec["consented_promise_set_hash"] = confirmation
    # These are runner-owned terminal markers, not recipe parameters.  The
    # durable resume transaction clears them from runs.params; strip the
    # in-memory copy too so a resumed recipe never observes stale halt state.
    runner_spec.pop("halted_code", None)
    runner_spec.pop("halted_reason", None)
    runner_spec.pop("_frisket_queued_action_run", None)
    # Retry-only state belongs to the old run. The successor is an ordinary
    # fresh run over an explicit row scope.
    runner_spec["row_ids"] = unrun_row_ids
    runner_spec["overwrite"] = True
    return {
        "column_id": column_id,
        "run_id": int(run_id),
        "op_id": int(run_row["op_id"]),
        "runner_spec": runner_spec,
        "unrun_row_ids": unrun_row_ids,
    }


def _guard_backfill_run_spec(
    runner_spec: dict[str, Any],
    *,
    project: Project | None = None,
) -> ActionError | None:
    # Persisted runner specs carry the canonical ``action_kind``. WHAT is
    # gated comes from the action's
    # declared capabilities in the contract registry, not a list of kinds.
    code_id = runner_spec.get("action_kind")
    if runner_spec.get("implementation_identity") is not None and project is not None:
        from frisket.authoring.workbench.installed_actions import (
            resolve_installed_action,
        )

        resolve_installed_action(project, str(code_id))
        return None
    gated = gated_capability_phrase(code_id)
    if gated and os.environ.get("FRISKET_ALLOW_CODE_RECIPES", "1") != "1":
        return ActionError(
            code="code_action_disabled",
            message=(
                f"the '{code_id}' action declares {gated}, which is disabled on "
                "this server; enable it only on trusted/local deployments"
            ),
            action_kind="run.backfill",
            field="params.column",
        )
    return None


def _successful_result_row_ids(
    project: Project,
    *,
    run_id: int,
    column_id: int,
    row_ids: list[int],
) -> list[int]:
    if not row_ids:
        return []
    placeholders = ",".join("?" * len(row_ids))
    # "Filled" excludes terminal failures too: a deliberately-retried row that
    # comes back empty_output again failed honestly and must not be reported
    # as filled; terminal rows may have empty output.
    not_filled = outcome_sql_list(FAILURE_OUTCOMES + TERMINAL_FAILURE_OUTCOMES)
    return [
        int(row["row_id"])
        for row in project.db.execute(
            "SELECT DISTINCT row_id FROM results "
            f"WHERE run_id=? AND column_id=? AND row_id IN ({placeholders}) "
            f"AND outcome NOT IN ({not_filled}) ORDER BY row_id",
            (run_id, column_id, *row_ids),
        )
    ]


def _run_backfill_output_ref(
    *,
    sheet_id: int,
    column: str,
    column_id: int,
    run_id: int,
    requested_row_ids: list[int],
    filled_row_ids: list[int],
) -> dict[str, Any]:
    return {
        "kind": "run_backfill",
        "sheet_id": sheet_id,
        "column_id": column_id,
        "column_name": column,
        "run_id": run_id,
        "requested_row_ids": requested_row_ids,
        "filled_row_ids": filled_row_ids,
        "filled": len(filled_row_ids),
    }


def _run_backfill_receipt(
    *,
    action: ActionSpec,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    output_ref: dict[str, Any],
    status: ActionResultStatus = "completed",
    errors: list[ActionError] | None = None,
) -> Receipt:
    run_id = int(output_ref["run_id"])
    column_id = int(output_ref["column_id"])
    column_name = str(output_ref["column_name"])
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        run_id=run_id,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status=status,
        inputs=[
            ReceiptIO(
                name="target_column",
                ref={
                    "kind": "run_backfill_target_column",
                    "sheet_id": int(output_ref["sheet_id"]),
                    "column_id": column_id,
                    "column_name": column_name,
                    "run_id": run_id,
                },
            )
        ],
        outputs=[ReceiptIO(name=column_name, ref=output_ref)],
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": "run_backfill_scope",
                    "run_id": run_id,
                    "column_id": column_id,
                    "requested_row_ids": list(output_ref["requested_row_ids"]),
                    "filled_row_ids": list(output_ref["filled_row_ids"]),
                    "filled": int(output_ref["filled"]),
                },
                retention="pinned",
            )
        ],
        errors=list(errors or []),
    )
