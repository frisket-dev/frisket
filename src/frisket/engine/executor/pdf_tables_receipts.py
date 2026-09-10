"""Project committed PDF read facts into cross-row feedable list schemas."""

from __future__ import annotations

import json

from frisket.actions.core import _value_annotation
from frisket.actions.pdf_table_types import PdfTableRows
from frisket.actions.types import _without_none
from frisket.contracts.action import ActionError, ReceiptEvidence
from frisket.engine.executor.recordsets import (
    DERIVE_TABLE_FROM_LIST,
    feedable_named_result_ref,
)
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import FAILURE_OUTCOMES
from frisket.ops.pdf_tables import _media_extract_pdf_tables_item_schema


def pdf_table_output(field):
    return PdfTableRows in _without_none(_value_annotation(field.annotation))


def pdf_table_receipt_projection(project, plan, facts, refs, receipt_id):
    stored = ReceiptStore(project).parsed_by_id(receipt_id)
    observed = (
        [
            item.ref
            for item in stored.evidence
            if item.ref.get("kind") == "pdf_table_read"
            and item.ref.get("run_id") == facts.run_id
        ]
        if stored is not None
        else []
    )
    named, evidence, errors = [], [], []
    for field in plan.program._resolved_output_fields:
        if not pdf_table_output(field):
            continue
        ref = next(
            item for item in refs if item["name"] == plan.output_names[field.key]
        )
        rows = project.db.execute(
            "SELECT row_id,value,error,error_code,outcome FROM results "
            "WHERE run_id=? AND column_id=? ORDER BY row_id",
            (facts.run_id, ref["column_id"]),
        ).fetchall()
        expected_columns, successful, count = None, [], 0
        for row in rows:
            if row["row_id"] not in facts.row_ids or row["outcome"] in FAILURE_OUTCOMES:
                continue
            value = json.loads(row["value"]) if row["value"] is not None else None
            read = next(
                (
                    item
                    for item in observed
                    if item["row_id"] == row["row_id"]
                    and item["column_id"] == ref["column_id"]
                    and item["output_key"] == field.key
                    and item["value_hash"] == canonical_json_hash(value)
                ),
                None,
            )
            if read is None or not isinstance(value, list):
                errors.append(
                    ActionError(
                        code="invalid_pdf_table_cells",
                        message="PDF output has no matching committed read.",
                        action_kind=plan.action.action_id,
                    )
                )
                break
            evidence.append(ReceiptEvidence(ref=read, retention="pinned"))
            if not value:
                successful.append(row["row_id"])
                continue
            if expected_columns is None:
                expected_columns = read["columns"]
            elif expected_columns != read["columns"]:
                errors.append(
                    ActionError(
                        code="pdf_table_shape_mismatch",
                        message="Extracted PDF table headers differ across source rows.",
                        action_kind=plan.action.action_id,
                    )
                )
                break
            count += len(value)
            successful.append(row["row_id"])
        if errors:
            continue
        if not count or expected_columns is None:
            failed = next((row for row in rows if row["error"] is not None), None)
            errors.append(
                ActionError(
                    code=str(failed["error_code"] or "pdf_table_extract_failed")
                    if failed
                    else "pdf_table_extract_failed",
                    message=str(failed["error"] or "PDF table extraction failed.")
                    if failed
                    else "No PDF table rows were extracted.",
                    action_kind=plan.action.action_id,
                )
            )
            continue
        named.append(
            feedable_named_result_ref(
                source_action_kind=plan.action.action_id,
                sheet_id=facts.sheet_id,
                column_id=ref["column_id"],
                run_id=facts.run_id,
                op_id=facts.op_id,
                route=ref["name"],
                schema_name="pdf_table_rows",
                schema_json={
                    "type": "array",
                    "items": _media_extract_pdf_tables_item_schema(expected_columns),
                },
                row_ids=successful,
                may_feed=[DERIVE_TABLE_FROM_LIST],
            )
        )
    return ([] if errors else named), evidence, errors
