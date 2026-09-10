"""Feedable media lists projected from accepted rows and durable image facts."""

from __future__ import annotations

import json
from typing import get_args, get_origin

from frisket.actions.core import _value_annotation
from frisket.actions.row_media_types import Face, Frame
from frisket.contracts.action import ReceiptIO
from frisket.engine.executor.recordsets import (
    DERIVE_TABLE_FROM_LIST,
    feedable_named_result_ref,
)
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import FAILURE_OUTCOMES


def row_media_output(field):
    annotation = _value_annotation(field.annotation)
    if get_origin(annotation) is list:
        (item,) = get_args(annotation)
        if item is Frame:
            return "frames", "image", "video_frame"
        if item is Face:
            return "faces", "face", "face_crop"
    return None


def project_row_media_receipt(project, plan, facts, receipt):
    stored = ReceiptStore(project).parsed_by_id(receipt.receipt_id)
    observations = (
        [
            item.ref
            for item in stored.evidence
            if item.ref.get("kind") == "row_file_output"
            and item.ref.get("run_id") == facts.run_id
        ]
        if stored is not None
        else []
    )
    for field in plan.program._resolved_output_fields:
        media = row_media_output(field)
        if media is None:
            continue
        schema_name, image_key, fact_kind = media
        column = next(
            output.ref
            for output in receipt.outputs
            if output.ref.get("kind") == "map_result_column"
            and output.name == plan.output_names[field.key]
        )
        rows = project.db.execute(
            "SELECT row_id,value,outcome FROM results WHERE run_id=? AND column_id=? ORDER BY row_id",
            (facts.run_id, column["column_id"]),
        ).fetchall()
        accepted = []
        for row in rows:
            if row["row_id"] not in facts.row_ids or row["outcome"] in FAILURE_OUTCOMES:
                continue
            values = json.loads(row["value"]) if row["value"] is not None else None
            if not isinstance(values, list):
                raise ValueError("Media list output is not a committed list")
            value_hash = canonical_json_hash(values)
            for index, value in enumerate(values):
                matching = [
                    observed
                    for observed in observations
                    if observed.get("row_id") == row["row_id"]
                    and observed.get("column_id") == column["column_id"]
                    and observed.get("output_key") == field.key
                    and observed.get("item_path") == [index, image_key]
                    and observed.get("value_hash") == value_hash
                    and observed.get("facts", {}).get("kind") == fact_kind
                    and observed.get("primary", {}).get("blob_hash")
                    == value[image_key]["blob"]
                ]
                if not matching:
                    raise ValueError(
                        "Media list image has no matching committed occurrence"
                    )
            accepted.append(row["row_id"])
        named = feedable_named_result_ref(
            source_action_kind=plan.action.action_id,
            sheet_id=facts.sheet_id,
            column_id=column["column_id"],
            run_id=facts.run_id,
            op_id=facts.op_id,
            route=column["name"],
            schema_name=schema_name,
            # Project the declared item model directly: copying list.items
            # alone would strand Pydantic's reference to the list's $defs.
            schema_json={
                "type": "array",
                "items": get_args(_value_annotation(field.annotation))[
                    0
                ].model_json_schema(),
            },
            row_ids=accepted,
            may_feed=[DERIVE_TABLE_FROM_LIST],
        )
        receipt.outputs.append(
            ReceiptIO(name=column["name"], kind="named_result", ref=named)
        )
    return receipt
