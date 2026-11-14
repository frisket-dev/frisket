"""Reapply retained results through ordinary undoable project writes."""

from uuid import uuid4
import json

from frisket.actions.types import SheetRows
from frisket.contracts.action import Receipt, ReceiptIO, ActionError
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


def reapply_results(project, project_id, bound):
    source = bound.params.source_run_id
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
            from frisket.engine.store.execution_groups import execution_runs

            source, group = execution_runs(project, source)
            run = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (source,)
            ).fetchone()
            if run is None or run["sheet_id"] != scope.sheet_id:
                raise ValueError("Saved run does not belong to the selected sheet")
            original_rows = set(RunResultStore(project).run_row_scope(source))
            requested = original_rows if scope.row_ids is None else set(scope.row_ids)
            if not requested.issubset(original_rows):
                raise ValueError(
                    "Reapply cannot include rows outside the original execution"
                )
            ph = ",".join("?" for _ in group)
            stored = project.db.execute(
                f"SELECT run_id,row_id,column_id FROM results WHERE run_id IN ({ph}) AND outcome IN ('ok','empty') "
                "AND (review_decision IS NULL OR review_decision NOT IN ('reject','reject_clear')) ORDER BY run_id",
                group,
            ).fetchall()
            retained = {
                (r["row_id"], r["column_id"]): (
                    r["run_id"],
                    r["row_id"],
                    r["column_id"],
                )
                for r in stored
                if r["row_id"] in requested
            }
            coordinates = list(retained.values())
            if not coordinates:
                raise ValueError("No retained accepted results are available")
            values = RunResultStore(project).decoded_result_rows(coordinates)
            params = json.loads(run["params"])
            names = params.get("output_names") or {}
            inverse = {value: key for key, value in names.items()}
            columns = {
                c["id"]: c
                for c in project.db.execute(
                    "SELECT * FROM columns WHERE sheet_id=?", (scope.sheet_id,)
                )
            }
            live = {c["name"]: c for c in project.columns(scope.sheet_id)}
            targets = {}
            created = []
            for _, _, column_id in coordinates:
                if column_id in targets:
                    continue
                original = columns[column_id]
                logical = inverse.get(original["name"], original["name"])
                name = (bound.request.output_names or {}).get(logical, original["name"])
                existing_column = live.get(name)
                if existing_column is not None and not bound.request.replace_existing:
                    raise ValueError(
                        f"Column {name!r} exists; choose another name or replace_existing"
                    )
                targets[column_id] = (
                    existing_column["id"]
                    if existing_column
                    else project.add_column(
                        scope.sheet_id, name, type=original["type"], commit=False
                    )
                )
                if existing_column is None:
                    created.append(targets[column_id])
            references = [
                {
                    "run_id": result_run_id,
                    "row_id": row_id,
                    "column_id": column_id,
                    "target_column_id": targets[column_id],
                }
                for result_run_id, row_id, column_id in coordinates
            ]
            op_id = project.apply_edits(
                [
                    {
                        "row_id": row_id,
                        "column_id": targets[column_id],
                        "value": values[(result_run_id, row_id, column_id)]["value"],
                    }
                    for result_run_id, row_id, column_id in coordinates
                ],
                label="Reapply saved execution results",
                spec={
                    "action_kind": "run.reapply",
                    "source_run_id": source,
                    "result_refs": references,
                },
            )
            from frisket.engine.store.op_log import set_undo_info

            info = json.loads(
                project.db.execute(
                    "SELECT undo_info FROM ops WHERE id=?", (op_id,)
                ).fetchone()[0]
                or "{}"
            )
            info["created_columns"] = created
            set_undo_info(project, op_id, info, commit=False)
            from frisket.engine.store.evidence import record_evidence_link

            for reference in references:
                target = reference["target_column_id"]
                _, current = project.get_values_with_refs(
                    scope.sheet_id, target, row_ids=[reference["row_id"]]
                )
                links = project.db.execute(
                    "SELECT * FROM evidence_links WHERE run_id=? AND row_id=? AND column_id=?",
                    (reference["run_id"], reference["row_id"], reference["column_id"]),
                ).fetchall()
                for link in links:
                    spans = [
                        dict(span)
                        for span in project.db.execute(
                            "SELECT span_id,rank,span_role,required,note FROM evidence_link_spans WHERE link_id=?",
                            (link["id"],),
                        )
                    ]
                    record_evidence_link(
                        project,
                        subject_kind=link["subject_kind"],
                        subject_ref=current[reference["row_id"]],
                        spans=spans,
                        sheet_id=scope.sheet_id,
                        row_id=reference["row_id"],
                        column_id=target,
                        run_id=reference["run_id"],
                        op_id=op_id,
                        link_role=link["link_role"],
                        confidence=link["confidence"],
                        pinned=True,
                        producer=json.loads(link["producer_json"]),
                        layer_family=link["layer_family"],
                        metadata={
                            **json.loads(link["metadata"]),
                            "reapplied_from": link["stable_id"],
                        },
                    )
            receipt_id, action_id = f"receipt_{uuid4().hex}", f"act_{uuid4().hex}"
            receipt = Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id=action_id,
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
                            "results": references,
                        },
                    )
                ],
                outputs=[
                    ReceiptIO(
                        name=str(column_id),
                        ref={
                            "kind": "added_column",
                            "sheet_id": scope.sheet_id,
                            "column_id": target,
                            "source_run_id": source,
                            "source_column_id": column_id,
                        },
                    )
                    for column_id, target in targets.items()
                ],
            )
            receipts.insert_completed(receipt, commit=False)
        return _result_from_receipt(receipt)
    except (ValueError, KeyError, TypeError) as error:
        return _failed_result(
            project_id=project_id,
            action_kind="run.reapply",
            error=ActionError(code="invalid_params", message=str(error)),
        )
