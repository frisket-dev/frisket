"""Grouped input admission and receipts for shared row execution."""

from __future__ import annotations

from collections.abc import Mapping

import hashlib
import logging
from functools import partial
from typing import Any

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
    receipt_status_from_failures,
    _positive_ref_int,
    _receipt_ref,
)
from frisket.engine.executor.action_reservations import (
    _receipt_for_idempotency,
    _reserved_receipt_result_from_existing,
)
from frisket.engine.executor.action_support import (
    ActionCtx,
    _model_call_provider_use,
)
from frisket.engine.executor.project_run_terminalization import (
    CurrentWriterTerminalAuthority,
    terminalize_project_run,
)
from frisket.ai.models.metadata import model_calls_cost_actual
from frisket.engine.store.materialization import (
    active_materialized_row_sources,
    load_materialized_row_sources_for_op,
    materialized_row_sources_ref_matches,
)
from frisket.ops.group_summary import (
    estimate_group_summary,
    group_summary_messages,
)
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import StaleAttemptWriter


logger = logging.getLogger("frisket.executor")


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
    *,
    capture_inputs: bool = False,
    cohort: list[int] | None = None,
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

    requested_rows = cohort if cohort is not None else params.row_ids
    if requested_rows is not None:
        found = set()
        for offset in range(0, len(requested_rows), 500):
            part = requested_rows[offset : offset + 500]
            placeholders = ",".join("?" for _ in part)
            found.update(
                int(row[0])
                for row in project.db.execute(
                    f"SELECT id FROM rows WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
                    [params.sheet_id, *part],
                )
            )
        missing_rows = sorted(set(requested_rows) - found)
        if missing_rows:
            return ActionError(
                code="invalid_input_ref",
                message="reduce.group_summary row_ids must belong to the target sheet",
                action_kind=params.action_kind,
                field="scope.row_ids",
                details={"missing": missing_rows},
            )
        row_ids = list(requested_rows)
    else:
        row_ids = project.visible_row_ids(params.sheet_id)
    if not row_ids and params.refresh_target is None:
        return ActionError(
            code="invalid_input_ref",
            message="reduce.group_summary requires at least one visible source row",
            action_kind=params.action_kind,
            field="scope.row_ids",
        )

    from contextlib import nullcontext, closing
    from frisket.engine.store.consumed_source_pages import iter_consumed_source_pages
    from .group_input_stage import GroupInputStage

    groups = GroupInputStage(
        row_ids,
        source=params.input_columns,
        group_by=params.group_by,
        group_type=str(columns[params.group_by]["type"])
        if params.group_by is not None
        else None,
    )
    captures = []
    context = (
        nullcontext(project) if project.db.in_transaction else project.read_snapshot()
    )
    try:
        with context as snapshot:
            if capture_inputs:
                pages = iter_consumed_source_pages(
                    snapshot,
                    params.sheet_id,
                    [int(columns[name]["id"]) for name in needed],
                    row_ids=row_ids,
                    scope_intent="selected" if params.row_ids is not None else "whole",
                    captures=captures,
                )
            else:

                def advisory_pages():
                    # Advisory quotes read values in bounded pages, without
                    # creating source-identity captures or approval snapshots.
                    for offset in range(0, len(row_ids), 256):
                        part = row_ids[offset : offset + 256]
                        yield (
                            part,
                            {
                                int(columns[name]["id"]): snapshot.get_values(
                                    params.sheet_id,
                                    int(columns[name]["id"]),
                                    row_ids=part,
                                )
                                for name in needed
                            },
                            {},
                        )

                pages = advisory_pages()
            with closing(pages):
                for page, values, _refs in pages:
                    for row_id in page:
                        groups.add(
                            row_id,
                            {
                                name: values[int(columns[name]["id"])].get(row_id)
                                for name in needed
                            },
                        )
        groups.seal()
    except BaseException:
        groups.close()
        raise
    consumed_capture = captures[0] if capture_inputs else None

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
        "consumed_capture": consumed_capture,
    }


def _recoverable_group_run(project, receipt, params_hash):
    if (
        receipt is None
        or receipt["status"] != "running"
        or receipt["params_hash"] != params_hash
        or receipt["run_id"] is None
    ):
        return None
    return project.db.execute(
        "SELECT run.* FROM runs run JOIN ops op ON op.id=run.op_id "
        "LEFT JOIN execution_attempts attempt ON attempt.id=run.current_attempt_id "
        "WHERE run.id=? AND run.status='running' AND run.cancel_requested_at IS NULL "
        "AND op.status='applied' AND op.publication_closed=0 "
        "AND (attempt.id IS NULL OR attempt.state IN ('abandoned','halted'))",
        (receipt["run_id"],),
    ).fetchone()


def _precheck_reduce_group_summary_target(
    project: Project,
    params: GroupSummaryPlan,
) -> ActionError | None:
    if params.refresh_target is not None:
        from frisket.engine.executor.group_summary_refresh import validate_target

        return validate_target(project, params)
    existing = project.db.execute(
        "SELECT id,parent_op_id FROM sheets WHERE name=?",
        (params.target_sheet_name,),
    ).fetchone()
    if existing is None:
        return None
    receipt = _receipt_for_idempotency(project, params.request["idempotency_key"])
    if receipt is not None:
        run = _recoverable_group_run(project, receipt, receipt["params_hash"])
        if run is not None and (
            int(run["sheet_id"]) == int(existing["id"])
            or existing["parent_op_id"] == run["op_id"]
        ):
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
    resolved_messages = (
        _reduce_group_summary_messages(params, group) for group in resolved["groups"]
    )
    return estimate_group_summary(params.model, resolved_messages)


def _group_source_binding(resolved: Mapping[str, Any]) -> dict[str, list[int]]:
    from frisket.engine.store.source_dependencies import execution_source_binding

    columns = [item["column_id"] for item in resolved["source_columns"].values()]
    if resolved.get("group_column") is not None:
        columns.append(resolved["group_column"]["column_id"])
    return execution_source_binding(
        sheet_ids=[resolved["sheet_id"]], column_ids=columns
    )


async def _complete_reduce_group_summaries(
    project, params, resolved, *, router, publication, composition
):
    """Materialize group identities, then execute ordinary model rows."""
    from frisket.engine.runner.map_runner import MapRunner
    from frisket.engine.runner.preparation import PreparedRun
    from frisket.engine.runner.row_inputs import RowInputValues
    from frisket.execution.attempt import claim
    from frisket.execution.attempt_authority import AttemptAuthority
    from .group_summary_program import GroupSummaryProgram

    resolved["groups"].close()
    current = _resolve_reduce_group_summary_inputs(
        project, params, capture_inputs=True, cohort=resolved["source_row_ids"]
    )
    if isinstance(current, ActionError):
        return current
    resolved.clear()
    resolved.update(current)
    program = GroupSummaryProgram(params, publication)
    spec = {
        "action_kind": params.action_kind,
        "model": params.model,
        "instruction": params.instruction,
        "input_columns": [],
        "fields": program.output_fields({}),
    }
    store = RunResultStore(project)
    if publication.recovery_run is not None:
        op_id = int(publication.recovery_run["op_id"])
        run_id = int(publication.recovery_run["id"])
    else:
        op_id = project.append_op(
            params.action_kind,
            {**params.request, "execution_sources": _group_source_binding(resolved)},
            label=f"Grouped summary {params.target_sheet_name}",
        )
        run_id = store.start_run(
            op_id,
            resolved["sheet_id"],
            params.action_kind,
            action_version=program.version,
            model=params.model,
            prompt_hash=_text_hash(params.instruction),
            params={},
            total_rows=len(resolved["groups"]),
            cost_estimate=_estimate_reduce_group_summary_cost(params, resolved)["cost"],
        )
    authority = AttemptAuthority(project, composition=composition)
    commitment = authority.mint(
        recipe=program,
        spec=spec,
        run_id=run_id,
        scope=tuple(resolved["source_row_ids"]),
    )
    # The same writer owns deterministic membership and model results: one Undo.
    claim(
        project,
        commitment,
        claim_token=publication.recovery_claim_token(),
        claimless_direct_effect=publication.recovery_claim_token() is None,
    )
    publication.start(
        resolved,
        {"op_id": op_id, "run_id": run_id, "attempt_id": commitment.attempt_id},
    )
    write = publication.write
    spec["sheet_id"] = write.sheet_id
    program.group_keys = {row_id: key for key, row_id in publication.positions.items()}
    from frisket.engine.store.consumed_sources import (
        capture_fingerprint,
        row_capture,
        store_capture,
    )

    with project.transaction_scope():
        store_capture(
            project,
            resolved["consumed_capture"],
            owner_op_id=op_id,
            output_sheet_id=write.sheet_id,
            run_id=run_id,
            shared=bool(write.row_ids),
        )
    fingerprint = capture_fingerprint(resolved["consumed_capture"])

    def read_inputs(row_id):
        capture = row_capture(
            sheet_id=write.sheet_id,
            row_id=row_id,
            source_cells={},
            descriptors={},
            revisions={},
        )
        capture["shared_inputs"] = [fingerprint]
        group = resolved["groups"].by_identity(program.group_keys[row_id])
        return RowInputValues(
            {"messages": group_summary_messages(params.instruction, group)},
            source_cells_empty=False,
            consumed_capture=capture,
        )

    prepared = PreparedRun(
        recipe=program,
        sheet_id=write.sheet_id,
        col_map={},
        row_ids=write.row_ids,
        out_cols={
            params.summary_column_name: write.column_ids[params.summary_column_name]
        },
        output_fields=tuple(program.output_fields(spec)),
        created_output_column_ids=frozenset()
        if params.refresh_target
        else frozenset([write.column_ids[params.summary_column_name]]),
        op_id=op_id,
        run_id=run_id,
        row_count=len(write.row_ids),
        run_scope_run_id=run_id,
        read_inputs=read_inputs,
    )
    runner = MapRunner(
        project,
        router,
        authority=authority,
        execution_composition=composition,
        defer_cancel_terminal_close=True,
    )
    async with publication.provider_lease():
        progress = await runner.run(
            spec,
            prepared_run=prepared,
            admitted_attempt=commitment,
            dispatch_claimed=True,
            claim_token=publication.claim_token,
            defer_attempt_close=True,
            defer_generation_seal=True,
            defer_output_publication=False,
        )
    if progress.halted_code:
        return ActionError(
            code=progress.halted_code,
            message=progress.halted_reason,
            action_kind=params.action_kind,
        )
    computations = [
        publication.saved({key: value for key, value in group.items() if key != "rows"})
        for group in resolved["groups"]
    ]
    publication.completed = sum(item is not None for item in computations)
    return [item for item in computations if item is not None]


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
    publication,
) -> ActionResult:
    ctx = ActionCtx(project_id, action.kind)
    authority_effect = publication.envelope
    if not any(
        item.get("error") for item in computations
    ) and publication.completed == len(resolved["groups"]):
        publication.complete_membership()
    cur = project.db.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is None:
            project.db.rollback()
            return ctx.fail(
                "project_write_failed",
                "reduce.group_summary idempotency reservation was lost",
            )
        if existing["id"] != receipt_id:
            project.db.rollback()
            return _reserved_receipt_result_from_existing(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
                replay_error_fn=partial(_reduce_group_summary_replay_error, project),
            )
        if existing["params_hash"] != params_hash:
            project.db.rollback()
            return ctx.fail(
                "idempotency_conflict",
                "idempotency_key was already used with different normalized params",
                field="idempotency_key",
            )
        run_store = RunResultStore(project)
        if authority_effect is not None and run_store.cancellation_requested(
            int(authority_effect["run_id"])
        ):
            result = publication.finish_cancelled()
            project.db.commit()
            return result

        write = publication.write
        op_id, summary_sheet_id, column_ids, summary_row_ids = (
            write.op_id,
            write.sheet_id,
            write.column_ids,
            write.row_ids,
        )
        run_id = int(authority_effect["run_id"])
        summary_column_id = column_ids[params.summary_column_name]
        claim_token = publication.claim_token
        generations = ResultGenerationStore(project)
        failed_groups = sum(bool(item.get("error")) for item in computations)
        run_status = (
            "failed"
            if computations and failed_groups == len(computations)
            else "completed"
        )
        generations.seal(
            run_id,
            [summary_column_id],
            claim_token=claim_token,
            terminal_disposition=run_status,
            commit=False,
        )
        run_store.point_column_at_run(op_id, summary_column_id, run_id, commit=False)
        action_status = receipt_status_from_failures(failed_groups, len(computations))
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
        return ctx.fail("project_write_failed", "project write failed")


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
            details = dict(RESUMABLE_PROVIDER_ERROR_DETAILS[first["error_code"]])
            if first["error_code"] != "provider_rate_limited":
                details["provider"] = params.model.split("/", 1)[0]
            errors.append(
                ActionError(
                    code=str(first["error_code"]),
                    message=str(
                        first.get("error")
                        or "provider capacity is temporarily unavailable"
                    ),
                    action_kind=action.kind,
                    details=details,
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
