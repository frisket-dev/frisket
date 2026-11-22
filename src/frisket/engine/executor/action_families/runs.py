"""Source-generation reconstruction and committed backfill facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.contracts.action import (
    ActionError,
    ActionResultStatus,
    Receipt,
    ReceiptRecord,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import _ActionExecutionEnvelope
from frisket.engine.executor.selection_scope import (
    evaluate_sheet_selection_row_ids,
    evaluate_sheet_view_row_ids,
    stored_view_id_from_runner_spec,
)
from frisket.engine.views.specs import SelectionUnavailable
from frisket.ops.base import persisted_action_program_invocation_halt
from frisket.engine.store import Project
from frisket.engine.store.result_generations import (
    ResultGenerationStore,
    _generation_publication_predicate,
)
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore
from frisket.engine.store.runs import (
    FAILURE_OUTCOMES,
    TERMINAL_FAILURE_OUTCOMES,
    outcome_sql_list,
)


def typed_run_has_matching_producer_receipt(
    project: Project,
    *,
    run_id: int,
    bound: Any,
) -> bool:
    """Accept the original producer or an exact backfill successor receipt."""

    from frisket.engine.executor.map_rows_action import typed_request_hash

    expected_hash = typed_request_hash(bound)
    receipts = ReceiptStore(project)
    producer = receipts.latest_for_run_action_statuses(
        run_id,
        bound.action.action_id,
        FINISHED_RECEIPT_STATUSES,
    )
    if producer is not None and producer.params_hash == expected_hash:
        return True
    successor = receipts.latest_for_run_statuses(run_id, FINISHED_RECEIPT_STATUSES)
    if successor is None:
        return False
    try:
        return any(
            supporting_record.ref.get("kind") == "backfill_source_generation"
            and supporting_record.ref.get("successor_run_id") == run_id
            and supporting_record.ref.get("successor_request_hash") == expected_hash
            for supporting_record in successor.parsed().records
        )
    except ValueError:
        return False


def _run_backfill_terminal_projection(
    project: Project,
    action: _ActionExecutionEnvelope,
    run_id: int,
    *,
    requested_row_ids: list[int],
    filled_row_ids: list[int],
) -> tuple[ActionResultStatus, list[ActionError]]:
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or str(run["status"] or "") != "cancelled":
        return "completed", []
    halt = persisted_action_program_invocation_halt(run["params"])
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


def _backfill_parent_run_id(receipts: ReceiptStore, run_id: int) -> int | None:
    stored = receipts.latest_for_run_statuses(run_id, FINISHED_RECEIPT_STATUSES)
    if stored is None:
        return None
    try:
        parsed = stored.parsed()
    except ValueError:
        return None
    for record in parsed.records:
        ref = record.ref
        if (
            ref.get("kind") == "backfill_source_generation"
            and ref.get("successor_run_id") == run_id
            and isinstance(ref.get("run_id"), int)
            and not isinstance(ref.get("run_id"), bool)
        ):
            return int(ref["run_id"])
    return None


def _lineage_root(
    receipts: ReceiptStore,
    run_id: int,
    parent_cache: dict[int, int | None],
) -> int:
    current = run_id
    visited: set[int] = set()
    while current not in visited:
        visited.add(current)
        if current not in parent_cache:
            parent_cache[current] = _backfill_parent_run_id(receipts, current)
        next_id = parent_cache[current]
        if next_id is None:
            return current
        current = next_id
    return current


def _inherited_lineage_view_id(
    project: Project,
    *,
    run_id: int,
    runner_spec: dict[str, Any],
) -> int | None:
    root_id = _lineage_root(ReceiptStore(project), run_id, {})
    if root_id == run_id:
        return stored_view_id_from_runner_spec(runner_spec)
    root_row = project.db.execute(
        "SELECT params FROM runs WHERE id=?", (int(root_id),)
    ).fetchone()
    if root_row is None:
        return stored_view_id_from_runner_spec(runner_spec)
    try:
        root_spec = json.loads(root_row["params"] or "{}")
    except (TypeError, ValueError):
        return stored_view_id_from_runner_spec(runner_spec)
    if not isinstance(root_spec, dict):
        return stored_view_id_from_runner_spec(runner_spec)
    return stored_view_id_from_runner_spec(root_spec)


def _empty_view_generation_origin(project: Project, column_id: int) -> list[int]:
    predicate = _generation_publication_predicate(
        generation_alias="generation", op_alias="op"
    )
    empty_gen_rows = project.db.execute(
        "SELECT generation.run_id, run.params FROM run_output_generations generation "
        "JOIN runs run ON run.id=generation.run_id "
        "JOIN changes op ON op.id=run.change_id "
        "JOIN run_scopes scope ON scope.run_id=generation.run_id "
        f"WHERE generation.column_id=? AND {predicate} "
        "AND scope.row_count=0 ORDER BY generation.run_id DESC",
        (int(column_id),),
    ).fetchall()
    if not empty_gen_rows:
        return []
    receipts = ReceiptStore(project)
    parent_cache: dict[int, int | None] = {}
    roots = {
        _lineage_root(receipts, int(row["run_id"]), parent_cache)
        for row in empty_gen_rows
    }
    if len(roots) != 1:
        return []
    root_id = next(iter(roots))
    params_by_run = {int(row["run_id"]): row["params"] for row in empty_gen_rows}
    root_params = params_by_run.get(root_id)
    if root_params is None:
        root_row = project.db.execute(
            "SELECT params FROM runs WHERE id=?", (int(root_id),)
        ).fetchone()
        root_params = root_row["params"] if root_row is not None else None
    try:
        stored_spec = json.loads(root_params or "{}")
    except (TypeError, ValueError):
        return []
    if (
        isinstance(stored_spec, dict)
        and stored_view_id_from_runner_spec(stored_spec) is not None
    ):
        return [int(empty_gen_rows[0]["run_id"])]
    return []


def _single_backfill_lineage_run_id(
    project: Project,
    generation_store: ResultGenerationStore,
    column_id: int,
) -> list[int]:
    """Return a single origin run_id when all column origins form one backfill lineage."""
    rows = project.db.execute(
        "SELECT DISTINCT run_id FROM generated_result_candidates "
        "WHERE column_id=? ORDER BY run_id DESC",
        (int(column_id),),
    ).fetchall()
    origin_ids = [int(row["run_id"]) for row in rows]
    if not origin_ids:
        return _empty_view_generation_origin(project, column_id)
    if len(origin_ids) == 1:
        return origin_ids

    receipts = ReceiptStore(project)
    parent_cache: dict[int, int | None] = {}
    roots = {_lineage_root(receipts, run_id, parent_cache) for run_id in origin_ids}
    if len(roots) == 1:
        return [origin_ids[0]]
    return generation_store.origin_run_ids(column_id, limit=2)


@dataclass(frozen=True)
class _BackfillSweepContext:
    sheet_id: int
    run_id: int
    all_row_ids: list[int]
    heads: dict[int, Any]
    view_id: int | None
    selection: dict[str, Any] | None
    action_kind: str = "run.backfill"


def _apply_backfill_sweep_view_scope(
    project: Project,
    runner_spec: dict[str, Any],
    ctx: _BackfillSweepContext,
) -> list[int] | ActionError:
    try:
        stored_view_id_from_runner_spec(runner_spec)
        inherited_view_id = _inherited_lineage_view_id(
            project,
            run_id=ctx.run_id,
            runner_spec=runner_spec,
        )
    except ValueError as exc:
        return ActionError(
            code="invalid_run_params",
            message=str(exc),
            action_kind=ctx.action_kind,
            field="params.column",
            details={"run_id": int(ctx.run_id)},
        )
    # Preserve the column lineage's inherited default row_scope across ad-hoc
    # backfill overrides so a one-off selection/view backfill never widens or
    # narrows future plain backfills.
    if inherited_view_id is not None:
        runner_spec["row_scope"] = {
            "kind": "view",
            "view_id": int(inherited_view_id),
        }
    else:
        runner_spec.pop("row_scope", None)
    done_row_ids = {
        row_id
        for row_id, head in ctx.heads.items()
        if head.outcome not in FAILURE_OUTCOMES
    }
    if ctx.selection is not None:
        try:
            matched_rows = set(
                evaluate_sheet_selection_row_ids(
                    project,
                    sheet_id=ctx.sheet_id,
                    selection=ctx.selection,
                )
            )
        except SelectionUnavailable as exc:
            return ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=ctx.action_kind,
                field="scope.selection",
            )
        return [
            row_id
            for row_id in ctx.all_row_ids
            if row_id in matched_rows and row_id not in done_row_ids
        ]
    effective_view_id = ctx.view_id if ctx.view_id is not None else inherited_view_id
    if effective_view_id is not None:
        try:
            matched_rows = set(
                evaluate_sheet_view_row_ids(
                    project,
                    sheet_id=ctx.sheet_id,
                    view_id=effective_view_id,
                )
            )
        except SelectionUnavailable as exc:
            return ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=ctx.action_kind,
                field="scope.view_id" if ctx.view_id is not None else "params.column",
                details={"view_id": int(effective_view_id)},
            )
        return [
            row_id
            for row_id in ctx.all_row_ids
            if row_id in matched_rows and row_id not in done_row_ids
        ]
    return [row_id for row_id in ctx.all_row_ids if row_id not in done_row_ids]


def _resolve_backfill_target(
    project: Project,
    *,
    sheet_id: int,
    column: str,
    row_ids: list[int] | None = None,
    view_id: int | None = None,
    selection: dict[str, Any] | None = None,
    confirmation: str | None = None,
    source_run_id: int | None = None,
) -> dict[str, Any] | ActionError:
    action_kind = "run.backfill"
    column_name = column.strip()
    column_row = next(
        (
            row
            for row in project.columns(sheet_id, include_hidden=True)
            if row["name"] == column_name
        ),
        None,
    )
    if column_row is None:
        return ActionError(
            code="column_not_found",
            message=f"no column '{column_name}'",
            action_kind=action_kind,
            field="params.column",
        )
    if not bool(column_row["generated"]):
        return ActionError(
            code="column_not_generated",
            message=f"column '{column_name}' is not a generated column",
            action_kind=action_kind,
            field="params.column",
        )
    column_id = int(column_row["id"])
    all_row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position",
            (sheet_id,),
        )
    ]
    generation_store = ResultGenerationStore(project)
    heads = generation_store.read_cell_heads(column_id)
    if source_run_id is not None:
        requested = set(row_ids or [])
        if not requested.issubset(all_row_ids):
            return ActionError(
                code="invalid_row_ref", message="Retry rows must remain visible"
            )
        unrun_row_ids = [row_id for row_id in all_row_ids if row_id in requested]
        source_run_ids = [source_run_id]
    elif row_ids is not None:
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
            # Unavailable outcomes deliberately are not published heads, but
            # their retained owner still identifies a whole-column transform.
            from frisket.authoring.action_metadata import action_supports_row_retry

            from frisket.engine.store.change_schema import generated_candidates_sql

            owners = project.db.execute(
                f"SELECT DISTINCT run.action_kind FROM ({generated_candidates_sql()}) head "
                "JOIN runs run ON run.id=head.run_id "
                "WHERE head.column_id=? "
                "AND head.outcome='not_completed' "
                "AND head.row_id IN (SELECT value FROM json_each(?))",
                (column_id, json.dumps(missing_heads)),
            )
            if any(not action_supports_row_retry(row[0]) for row in owners):
                return ActionError(
                    code="run_required",
                    message="This action transforms the whole column; run the action again instead of backfilling rows.",
                    action_kind=action_kind,
                    field="params.column",
                )
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
        # is representable only while the column has one active generation
        # lineage; a mixed-origin column has no single action to apply to a new row.
        done_row_ids = {
            row_id
            for row_id, head in heads.items()
            if head.outcome not in FAILURE_OUTCOMES
        }
        unrun_row_ids = [row_id for row_id in all_row_ids if row_id not in done_row_ids]
        source_run_ids = _single_backfill_lineage_run_id(
            project, generation_store, column_id
        )
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
        "SELECT id, change_id, params, action_kind FROM runs WHERE id=?",
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
    from frisket.authoring.action_metadata import action_supports_row_retry

    if not action_supports_row_retry(run_row["action_kind"]):
        return ActionError(
            code="run_required",
            message="This action transforms the whole column; run the action again instead of backfilling rows.",
            action_kind=action_kind,
            field="params.column",
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
        if not typed_run_has_matching_producer_receipt(
            project, run_id=run_id, bound=original_bound
        ):
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
        stored_roles = {str(name) for name in output_names}
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
                message="stored typed action generated columns no longer exist",
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
                columns_by_id[bindings_by_role[str(logical)].column_id]["name"]
            )
            for logical in stored_output_names
        }
        runner_spec["output_names"] = output_names
        runner_spec["output_target_preconditions"] = {
            output_names[str(logical)]: bindings_by_role[str(logical)].column_id
            for logical in stored_output_names
        }
        runner_spec["replace_existing"] = True
    runner_spec.pop("execution_root_run_id", None)
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
    # These are runner-owned terminal markers, not program parameters.  The
    # durable resume transaction clears them from runs.params; strip the
    # in-memory copy too so a resumed program never observes stale halt state.
    runner_spec.pop("halted_code", None)
    runner_spec.pop("halted_reason", None)
    runner_spec.pop("_frisket_queued_action_run", None)
    # Retry-only state belongs to the old run. The successor is an ordinary
    # fresh run over an explicit row scope (preserving View lineage when
    # the source run or backfill request is view-scoped).
    if source_run_id is None and row_ids is None:
        scoped = _apply_backfill_sweep_view_scope(
            project,
            runner_spec,
            _BackfillSweepContext(
                sheet_id=sheet_id,
                run_id=int(run_id),
                all_row_ids=all_row_ids,
                heads=heads,
                view_id=view_id,
                selection=selection,
                action_kind=action_kind,
            ),
        )
        if isinstance(scoped, ActionError):
            return scoped
        unrun_row_ids = scoped
    runner_spec["row_ids"] = unrun_row_ids
    runner_spec["overwrite"] = True
    return {
        "column_id": column_id,
        "run_id": int(run_id),
        "change_id": int(run_row["change_id"]),
        "runner_spec": runner_spec,
        "unrun_row_ids": unrun_row_ids,
    }


def _guard_backfill_run_spec(
    runner_spec: dict[str, Any],
    *,
    project: Project | None = None,
    edition: str | None = None,
) -> ActionError | None:
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.execution.action_admission import action_admission_error

    code_id = str(runner_spec.get("action_kind") or "")
    registered = None
    binding = None
    if runner_spec.get("implementation_identity") is not None and project is not None:
        from frisket.authoring.workbench.installed_actions import (
            resolve_installed_action,
        )

        installed = resolve_installed_action(project, code_id)
        if installed is not None:
            registered, binding = installed
    if registered is None:
        try:
            registered = ACTION_REGISTRY.get(code_id)
        except KeyError:
            return None  # The typed reconstruction owns unknown action refusal.
    error = action_admission_error(
        project, registered, edition=edition, runtime_binding=binding
    )
    if error is not None:
        return error.model_copy(
            update={"action_kind": "run.backfill", "field": "params.column"}
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
    action: _ActionExecutionEnvelope,
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
        records=[
            ReceiptRecord(
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
