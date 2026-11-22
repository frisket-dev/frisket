"""Copy a stopped execution's coherent saved outcomes into an ordinary Change."""

import json
from uuid import uuid4

from frisket.actions.core import OutputField
from frisket.actions.types import SheetRows
from frisket.contracts.action import ActionError, Receipt, ReceiptIO
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import ActionCtx
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    _require_expected_generated_columns,
    resolve_row_output_targets,
    typed_request_hash,
)
from frisket.engine.store.changes import ChangeStore
from frisket.engine.store.copied_results import (
    UnavailableReapplySelection,
    combined_execution_outcomes,
    copy_staged_outcomes,
)
from frisket.engine.store.op_log import set_undo_info
from frisket.engine.store.purge import require_restore_available
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore


def _copy_citations(project, op_id: int, sheet_id: int) -> None:
    """Copy link ownership, not source spans or per-cell receipt payloads."""
    from frisket.engine.store.citations import copy_citations

    links = project.db.execute(
        "SELECT link.id,copy.row_id AS target_row_id,copy.column_id AS target_column_id "
        "FROM copied_result_outcomes copy JOIN citations link "
        "ON link.run_id=copy.source_run_id AND link.row_id=copy.source_row_id "
        "AND link.column_id=copy.source_column_id WHERE copy.op_id=? "
        "AND (link.op_id IS NULL OR link.op_id=(SELECT op_id FROM runs WHERE id=copy.source_run_id)) ORDER BY link.id",
        (op_id,),
    )
    # Read only evidence owned by the producing run, never another copy.
    while batch := links.fetchmany(200):
        rows_by_column = {}
        for link in batch:
            rows_by_column.setdefault(link["target_column_id"], set()).add(
                link["target_row_id"]
            )
        refs_by_column = {
            column: project.get_values_with_refs(
                sheet_id, column, row_ids=sorted(rows)
            )[1]
            for column, rows in rows_by_column.items()
        }
        copy_citations(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            copies=[
                {
                    "source_link_id": link["id"],
                    "row_id": link["target_row_id"],
                    "column_id": link["target_column_id"],
                    "subject_ref": refs_by_column[link["target_column_id"]][
                        link["target_row_id"]
                    ],
                }
                for link in batch
            ],
        )


def _outcome_counts(project, op_id: int) -> dict[str, int]:
    counts = {"completed": 0, "failed": 0, "not_completed": 0}
    for state, count in project.db.execute(
        "SELECT outcome,count(*) FROM (SELECT row_id,"
        "CASE WHEN MAX(outcome='failed') THEN 'failed' "
        "WHEN MAX(outcome='not_completed') THEN 'not_completed' ELSE 'completed' END AS outcome "
        "FROM copied_result_values WHERE op_id=? GROUP BY row_id) GROUP BY outcome",
        (op_id,),
    ):
        counts[state] = int(count)
    return counts


def reapply_results(project, project_id, bound):
    scope = bound.request.scope
    fingerprint = typed_request_hash(bound)
    try:
        if not isinstance(scope, SheetRows):
            raise ValueError("Reapply requires a sheet selection")
        with project.transaction_scope():
            receipts = ReceiptStore(project)
            existing = receipts.find_by_idempotency_key(bound.request.idempotency_key)
            if existing is not None:
                if existing.params_hash != fingerprint:
                    raise ValueError("Idempotency key belongs to different work")
                return _result_from_receipt(existing.parsed())
            with combined_execution_outcomes(
                project,
                bound.params.source_run_id,
                sheet_id=scope.sheet_id,
                row_ids=None if scope.row_ids is None else list(scope.row_ids),
            ) as (source, unavailable):
                bindings = ResultGenerationStore(project).bindings_for_run(source)
                require_restore_available(project.db, sheet_ids=[scope.sheet_id])
                require_restore_available(
                    project.db, column_ids=[binding.column_id for binding in bindings]
                )
                if not bindings:
                    raise ValueError("Saved execution has no output contract")
                fields = []
                for binding in bindings:
                    descriptor = binding.output_descriptor
                    if not descriptor.get("column_type"):
                        raise TypedMapRowsPlanError(
                            "saved_contract_unavailable",
                            "This execution has no retained output descriptor; its results remain inspectable",
                        )
                    fields.append(OutputField(key=binding.output_role, **descriptor))
                fields = tuple(fields)
                columns = {
                    column["id"]: column
                    for column in project.db.execute(
                        "SELECT * FROM columns WHERE sheet_id=?", (scope.sheet_id,)
                    )
                }
                output_names = {
                    binding.output_role: columns[binding.column_id]["name"]
                    for binding in bindings
                }
                output_names.update(bound.request.output_names)
                bound.action.validate_output_names(fields, output_names)
                # Reapply always targets the original cohort, even if later rows
                # exist. Descriptor replacement is only wholesale when that
                # cohort still includes every visible row.
                covers_sheet = not project.db.execute(
                    "SELECT 1 FROM rows WHERE sheet_id=? AND hidden=0 AND id NOT IN "
                    "(SELECT row_id FROM reapply_selected_rows) LIMIT 1",
                    (scope.sheet_id,),
                ).fetchone()
                target_request = bound.request.model_copy(
                    update={
                        "output_names": output_names,
                        "scope": scope.model_copy(update={"row_ids": None})
                        if covers_sheet
                        else scope,
                    }
                )
                hidden = {
                    column["name"]
                    for column in columns.values()
                    if column["active"] and column["hidden"]
                }
                if hidden.intersection(output_names.values()):
                    raise TypedMapRowsPlanError(
                        "generated_column_exists",
                        "Reapply cannot replace hidden active output columns; choose a new name",
                    )
                names, preconditions = resolve_row_output_targets(
                    project, target_request, fields
                )
                _require_expected_generated_columns(target_request, preconditions)
                for field in fields:
                    target = preconditions[names[field.key]]
                    if target is not None and not covers_sheet:
                        column = columns[target]
                        if (
                            column["type"],
                            column["format"],
                            column["semantic_type"],
                        ) != (field.column_type, field.format, field.semantic_type):
                            raise TypedMapRowsPlanError(
                                "generated_column_exists",
                                "A partial copy requires a compatible output column; choose a new column",
                            )
                op_id = project.append_change_in_transaction(
                    "run.reapply",
                    {"action_kind": "run.reapply", "source_run_id": source},
                    label="Reapply saved execution results",
                )
                changes = ChangeStore(project)
                changes.start(op_id)
                targets, created = {}, []
                for field in fields:
                    target = preconditions[names[field.key]]
                    if target is None:
                        target = project.add_column_in_transaction(
                            scope.sheet_id,
                            names[field.key],
                            type=field.column_type,
                            generated=True,
                            format=field.format,
                            semantic_type=field.semantic_type,
                        )
                        created.append(target)
                    else:
                        from frisket.engine.store.column_descriptors import (
                            set_descriptor,
                        )

                        for key, value in (
                            ("type", field.column_type),
                            ("format", field.format),
                            ("semantic_type", field.semantic_type),
                        ):
                            if columns[target][key] != value:
                                set_descriptor(
                                    project,
                                    target,
                                    key,
                                    value,
                                    op_id=op_id,
                                )
                    targets[field.key] = target
                info = json.loads(
                    project.db.execute(
                        "SELECT undo_info FROM ops WHERE id=?", (op_id,)
                    ).fetchone()[0]
                    or "{}"
                )
                info["created_columns"] = created
                set_undo_info(project, op_id, info)
                from frisket.engine.store.review_stats import ensure_run_review_stats

                ensure_run_review_stats(project.db, source)
                review_roles = {
                    row["column_id"]: "field" if row["is_primary"] else "evidence"
                    for row in project.db.execute(
                        "SELECT column_id,is_primary FROM run_review_fields WHERE run_id=?",
                        (source,),
                    )
                }
                output_bindings = {
                    binding.output_role: {
                        "column_id": targets[binding.output_role],
                        "source_column_id": binding.column_id,
                        "review_role": review_roles[binding.column_id],
                    }
                    for binding in bindings
                }
                project.db.execute(
                    "UPDATE ops SET spec=json_set(spec,'$.output_bindings',json(?)) WHERE id=?",
                    (json.dumps(output_bindings), op_id),
                )
                copy_staged_outcomes(project.db, op_id=op_id, targets=targets)
                _copy_citations(project, op_id, scope.sheet_id)
                changes.close(op_id)
                summary = {
                    **_outcome_counts(project, op_id),
                    "unavailable_rows": unavailable,
                }
                receipt = Receipt(
                    receipt_id=f"receipt_{uuid4().hex}",
                    project_id=project_id,
                    action_id=f"act_{uuid4().hex}",
                    action_kind="run.reapply",
                    status="completed",
                    op_ids=[op_id],
                    idempotency_key=bound.request.idempotency_key,
                    params_hash=fingerprint,
                    inputs=[
                        ReceiptIO(
                            name="saved_results",
                            ref={
                                "kind": "retained_execution_results",
                                "run_id": source,
                                **summary,
                            },
                        )
                    ],
                    outputs=[
                        ReceiptIO(
                            name=role,
                            ref={
                                "kind": "added_column"
                                if target in created
                                else "column_patch",
                                "sheet_id": scope.sheet_id,
                                "column_id": target,
                                "source_run_id": source,
                                **summary,
                            },
                        )
                        for role, target in targets.items()
                    ],
                )
                receipts.insert_completed(receipt)
        return _result_from_receipt(receipt)
    except (ValueError, KeyError, TypeError) as error:
        return ActionCtx(project_id, "run.reapply").failed(
            ActionError(
                code=error.code
                if isinstance(error, TypedMapRowsPlanError)
                else "invalid_row_ref"
                if isinstance(error, UnavailableReapplySelection)
                else "invalid_params",
                message=str(error),
                field=error.field if isinstance(error, TypedMapRowsPlanError) else None,
                details=dict(error.details)
                if isinstance(error, TypedMapRowsPlanError)
                else {},
            )
        )
