"""Stable cell-content identity, separate from current read diagnostics."""

import json
from typing import Any


def evidence_subject_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """Evidence names a saved value; validity/outcome describe its current read."""
    return {
        key: value for key, value in ref.items() if key not in {"outcome", "validity"}
    }


def evidence_subject_ref_json(ref: dict[str, Any]) -> str:
    return json.dumps(
        evidence_subject_ref(ref),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def current_value_ref(
    row: Any, *, row_id: int, column_id: int, include_validity: bool = False
) -> dict[str, Any]:
    """One identity shape for full, bounded, and metadata-only cell reads."""
    ref = {
        "kind": row["origin_kind"] or "missing",
        "op_id": row["origin_op_id"],
        "row_id": row_id,
        "column_id": column_id,
        "run_id": row["origin_run_id"],
        "outcome": row["outcome"],
    }
    for source, target in (
        ("base_producer_id", "producer_id"),
        ("prepared_ref_id", "prepared_ref_id"),
    ):
        if source in row.keys() and row[source] is not None:
            ref[target] = int(row[source])
    if "copied_run_id" in row.keys() and row["copied_run_id"] is not None:
        ref["retained_result"] = {
            "run_id": int(row["copied_run_id"]),
            "row_id": int(row["copied_row_id"]),
            "column_id": int(row["copied_column_id"]),
        }
    if include_validity:
        ref["validity"] = row["validity"]
    return ref
