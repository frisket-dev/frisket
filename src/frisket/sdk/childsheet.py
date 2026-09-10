"""Generate the CHILD-SHEET (direct-seam) wiring from an `@op` declaration.

The reserved-maprunner generator (maprunner.py) handles ops that derive COLUMNS on a
sheet. Child-sheet ops (derive.*/reduce.*/join.*) instead produce a NEW sheet with
parent-row provenance, through the shared action lifecycle. This is the SAME `@op`
declaration with a different output axis (`output_kind="child_sheet"`) -- not a separate
SDK: the op declares a source resolve + `present()` (the receipt's op-specific inputs +
evidence), and this module is the generic orchestrator (idempotency -> resolve ->
duplicate_sheet_name precheck -> write_single_parent_child_sheet -> the sheet/column/
rows outputs -> receipt envelope -> commit). The receipt reuses the same typed
`Provenance` (inputs + evidence); the output refs are the materialized-sheet family.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from frisket.contracts.action import (
    ActionError,
    ActionOutput,
    ActionResult,
    ReceiptIO,
)
from frisket.sdk.declaration import Op


def _duplicate_sheet_error(
    decl: Op, project: Any, target_name: str
) -> ActionError | None:
    existing = project.db.execute(
        "SELECT id FROM sheets WHERE name=?", (target_name,)
    ).fetchone()
    if existing is None:
        return None
    return ActionError(
        code="duplicate_sheet_name",
        message=f"{decl.kind} target sheet name already exists",
        action_kind=decl.kind,
        field=f"params.{decl.target_sheet_param}",
        details={"sheet_id": int(existing["id"]), "name": target_name},
    )


def _op_spec(
    decl: Op, action: Any, params: Any, params_hash: str, resolved: dict
) -> dict:
    payload = action.model_dump(mode="json")
    payload["params"] = params.model_dump(mode="json", by_alias=True)
    payload["params"]["params_hash"] = params_hash
    if decl.op_spec_extra is not None:
        payload["params"].update(decl.op_spec_extra(resolved))
    return payload


def _action_outputs(target_name: str, write: Any) -> list[ActionOutput]:
    outputs = [
        ActionOutput(
            kind="sheet",
            name=target_name,
            sheet_id=write.sheet_id,
            ref={
                "kind": "materialized_sheet",
                "sheet_id": write.sheet_id,
                "op_id": write.op_id,
            },
        )
    ]
    outputs.extend(
        ActionOutput(
            kind="column",
            name=name,
            sheet_id=write.sheet_id,
            column_id=column_id,
            ref={
                "kind": "materialized_column",
                "sheet_id": write.sheet_id,
                "column_id": column_id,
                "op_id": write.op_id,
            },
        )
        for name, column_id in write.column_ids.items()
    )
    outputs.append(
        ActionOutput(
            kind="rows",
            name="rows",
            sheet_id=write.sheet_id,
            row_ids=write.row_ids,
            ref={
                "kind": "materialized_rows",
                "sheet_id": write.sheet_id,
                "row_ids": write.row_ids,
                "parent_row_ids": write.parent_row_ids,
                "op_id": write.op_id,
            },
        )
    )
    return outputs


def _receipt_outputs(
    target_name: str, parent_sheet_id: int, write: Any
) -> list[ReceiptIO]:
    return [
        ReceiptIO(
            name=target_name,
            ref={
                "kind": "materialized_sheet",
                "sheet_id": write.sheet_id,
                "parent_sheet_id": parent_sheet_id,
                "op_id": write.op_id,
            },
        ),
        *[
            ReceiptIO(
                name=f"column.{name}",
                ref={
                    "kind": "materialized_column",
                    "sheet_id": write.sheet_id,
                    "column_id": column_id,
                    "op_id": write.op_id,
                },
            )
            for name, column_id in write.column_ids.items()
        ],
        ReceiptIO(
            name="rows",
            ref={
                "kind": "materialized_rows",
                "sheet_id": write.sheet_id,
                "row_ids": write.row_ids,
                "parent_row_ids": write.parent_row_ids,
                "op_id": write.op_id,
            },
        ),
    ]


def build_child_sheet_run_fn(decl: Op) -> Callable[..., ActionResult]:
    """The deterministic child-sheet adapter: wire the op's decl-derived seams onto an
    `_ActionCoreSpec` (body_kind="child_sheet_deterministic") and run the shared core.
    The core owns idempotency replay + the body (resolve -> duplicate precheck -> write
    child sheet -> receipt); this builder only supplies the op-specific callables."""

    def run_fn(
        project: Any,
        action: Any,
        params: Any,
        *,
        project_id: str,
    ) -> ActionResult:
        from frisket.engine.executor.action_inventory import (
            ExecutorContext,
            ExecutorDeps,
            _ActionCoreSpec,
        )
        from frisket.engine.executor.action_lifecycle import (
            _child_sheet_deterministic_result_from_existing,
            _run_action_core_spec,
        )

        spec = _ActionCoreSpec(
            kind=decl.kind,
            params_model=decl.params_model,
            body_kind="child_sheet_deterministic",
            result_from_existing_fn=_child_sheet_deterministic_result_from_existing(
                replay_validate_fn=decl.child_sheet_replay_validate
            ),
            child_sheet_target_name_fn=lambda p: getattr(p, decl.target_sheet_param),
            child_sheet_duplicate_error_fn=(
                lambda proj, name: _duplicate_sheet_error(decl, proj, name)
            ),
            child_sheet_resolve_fn=decl.child_sheet_resolve,
            child_sheet_present_fn=decl.receipt,
            child_sheet_op_spec_fn=(
                lambda act, p, ph, resolved: _op_spec(decl, act, p, ph, resolved)
            ),
            child_sheet_action_outputs_fn=_action_outputs,
            child_sheet_receipt_outputs_fn=_receipt_outputs,
            child_sheet_row_evidence_fn=decl.child_sheet_row_evidence,
        )
        ctx = ExecutorContext(
            project_id=project_id,
            deps=ExecutorDeps(),
        )
        return _run_action_core_spec(project, action, params, spec=spec, ctx=ctx)

    return run_fn


def build_deterministic_owned_write_child_sheet_run_fn(
    *,
    kind: str,
    params_model: Any,
    target_name_fn: Callable[[Any], Any],
    resolve_fn: Callable[[Any, Any], Any],
    duplicate_error_fn: Callable[[Any, Any], ActionError | None],
    write_in_txn_fn: Callable[..., ActionResult],
    replay_validate_fn: Callable[[Any, Any], ActionError | None],
    params_hash_fn: Callable[[Any], str],
) -> Callable[..., ActionResult]:
    """The deterministic OP-OWNED-WRITE child-sheet adapter (derive.join).

    A read-only resolve materialized into a MULTI-PARENT aggregate sheet, where the op
    owns the in-txn write (write_aggregate_sheet + the post-write `UPDATE ops SET spec`
    that injects the materialized refs + the receipt) rather than the generic
    single-parent write_single_parent_child_sheet. The core owns the idempotency replay +
    the BEGIN IMMEDIATE/recheck/commit envelope (body_kind
    "child_sheet_deterministic_owned_write"); the op supplies the resolve, target-name,
    duplicate, in-txn write, and replay-validate seams. Unlike build_child_sheet_run_fn,
    the idempotency replay validates staleness (`replay_validate_fn`) because the
    materialized aggregate has op-owned replay. Takes hand-written callables directly
    rather than an `@op` declaration."""

    def run_fn(
        project: Any,
        action: Any,
        params: Any,
        *,
        project_id: str,
    ) -> ActionResult:
        from frisket.engine.executor.action_inventory import (
            ExecutorContext,
            ExecutorDeps,
            _ActionCoreSpec,
        )
        from frisket.engine.executor.action_lifecycle import (
            _child_sheet_deterministic_result_from_existing,
            _run_action_core_spec,
        )

        spec = _ActionCoreSpec(
            kind=kind,
            params_model=params_model,
            body_kind="child_sheet_deterministic_owned_write",
            params_hash_fn=params_hash_fn,
            result_from_existing_fn=_child_sheet_deterministic_result_from_existing(
                replay_validate_fn=replay_validate_fn
            ),
            child_sheet_target_name_fn=target_name_fn,
            child_sheet_duplicate_error_fn=duplicate_error_fn,
            child_sheet_resolve_fn=resolve_fn,
            child_sheet_replay_validate_fn=replay_validate_fn,
            child_sheet_deterministic_write_in_txn_fn=write_in_txn_fn,
        )
        ctx = ExecutorContext(
            project_id=project_id,
            deps=ExecutorDeps(),
        )
        return _run_action_core_spec(project, action, params, spec=spec, ctx=ctx)

    return run_fn


def build_reserved_model_child_sheet_run_fn(decl: Op) -> Callable[..., ActionResult]:
    """The MODEL-backed / reservation-backed / cost-gated child-sheet orchestrator.

    The hardest child-sheet archetype (reduce.group_summary, join.*): a NEW sheet
    materialized from model output, with a running-receipt reservation, a cost
    confirmation gate, and replay validation of the materialized refs. This module owns
    ORCHESTRATION ONLY; the op owns the write + materialization (reduce uses
    write_aggregate_sheet, not write_single_parent_child_sheet) via `child_sheet_write`,
    which builds and finalizes the receipt itself. Composing existing seams, the flow
    reproduces the hand-written `_run_<op>`:

      idempotency/replay -> resolve -> duplicate_sheet_name precheck -> cost-gate ->
      reserve running receipt -> async model compute -> write_result (owns receipt) ->
      reservation cleanup.
    """
    reservation_kind = (
        decl.reservation_kind or f"{decl.underscored}_idempotency_reservation"
    )

    def run_fn(
        project: Any,
        action: Any,
        params: Any,
        *,
        project_id: str,
        router: Any | None,
    ) -> ActionResult:
        from frisket.engine.executor.action_inventory import (
            ExecutorContext,
            ExecutorDeps,
            _ActionCoreSpec,
        )
        from frisket.engine.executor.action_lifecycle import (
            _child_sheet_model_delete_fn,
            _child_sheet_model_reserve_fn,
            _child_sheet_model_result_from_existing,
            _run_action_core_spec,
        )
        from frisket.engine.executor.action_support import (
            _params_hash_without_confirmed,
        )

        base = _ActionCoreSpec(
            kind=decl.kind,
            params_model=decl.params_model,
            body_kind="child_sheet_model",
            params_hash_fn=_params_hash_without_confirmed,
            reservation_kind=reservation_kind,
            child_sheet_target_name_fn=lambda p: getattr(p, decl.target_sheet_param),
            child_sheet_duplicate_error_fn=(
                lambda proj, name: _duplicate_sheet_error(decl, proj, name)
            ),
            child_sheet_replay_validate_fn=decl.child_sheet_replay_validate,
            child_sheet_model_resolve_fn=decl.child_sheet_resolve,
            child_sheet_estimate_cost_fn=decl.child_sheet_estimate_cost,
            child_sheet_compute_fn=decl.child_sheet_compute,
            child_sheet_write_result_fn=decl.child_sheet_write,
        )
        spec = replace(
            base,
            result_from_existing_fn=_child_sheet_model_result_from_existing(base),
            reserve_fn=_child_sheet_model_reserve_fn(base),
            delete_reservation_fn=_child_sheet_model_delete_fn(base),
        )
        ctx = ExecutorContext(
            project_id=project_id,
            deps=ExecutorDeps(router=router),
        )
        return _run_action_core_spec(project, action, params, spec=spec, ctx=ctx)

    return run_fn
