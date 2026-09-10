"""Source materialization triggers for watch evaluation."""

from __future__ import annotations

import json
from typing import Any, TypedDict

from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.features.watchlists.service import run_watch_evaluation, watch_query


class SourcePollMaterialization(TypedDict):
    kind: str
    project_id: str
    source_id: int
    source_run_id: int
    sheet_id: int
    op_id: int
    row_ids: list[int]
    materialized_rows: int


def source_poll_materialization_from_receipt(
    project: Project,
    *,
    project_id: str,
    receipt_id: str,
) -> SourcePollMaterialization | None:
    body = ReceiptStore(project).body_by_id(receipt_id)
    if body is None:
        return None
    try:
        receipt = json.loads(body)
    except (TypeError, ValueError):
        return None
    if receipt.get("action_kind") != "source.poll":
        return None
    if receipt.get("status") != "completed":
        return None
    refs = _receipt_refs(receipt)
    source_run = next(
        (ref for ref in refs if ref.get("kind") == "source_poll_run"),
        {},
    )
    rows = next(
        (ref for ref in refs if ref.get("kind") == "source_poll_rows"),
        {},
    )
    if source_run.get("status") != "ok":
        return None
    materialized_rows = _positive_int_or_none(source_run.get("materialized_rows"))
    if materialized_rows is None or materialized_rows <= 0:
        return None
    row_ids = [_as_positive_int(row_id) for row_id in rows.get("row_ids") or []]
    row_ids = [row_id for row_id in row_ids if row_id is not None]
    if not row_ids:
        return None
    source_id = _positive_int_or_none(
        source_run.get("source_id") or rows.get("source_id")
    )
    source_run_id = _positive_int_or_none(
        source_run.get("source_run_id") or rows.get("source_run_id")
    )
    sheet_id = _positive_int_or_none(source_run.get("sheet_id") or rows.get("sheet_id"))
    op_id = _positive_int_or_none(source_run.get("op_id") or rows.get("op_id"))
    if None in {source_id, source_run_id, sheet_id, op_id}:
        return None
    return {
        "kind": "source_poll_materialized",
        "project_id": project_id,
        "source_id": int(source_id),
        "source_run_id": int(source_run_id),
        "sheet_id": int(sheet_id),
        "op_id": int(op_id),
        "row_ids": row_ids,
        "materialized_rows": materialized_rows,
    }


def trigger_source_materialized_watches(
    project: Project,
    materialization: SourcePollMaterialization,
) -> list[dict[str, Any]]:
    if (
        materialization.get("kind") != "source_poll_materialized"
        or materialization.get("op_id") is None
        or not materialization.get("row_ids")
    ):
        return []
    out: list[dict[str, Any]] = []
    for watch in affected_enabled_watches(project, materialization):
        evaluation = run_watch_evaluation(project, watch)
        evaluation.update(
            {
                "trigger_kind": "source_poll_materialized",
                "source_id": materialization["source_id"],
                "source_run_id": materialization["source_run_id"],
                "sheet_id": materialization["sheet_id"],
                "op_id": materialization["op_id"],
                "materialized_row_ids": list(materialization["row_ids"]),
            }
        )
        out.append(evaluation)
    return out


def trigger_completed_source_poll_watches(
    project: Project,
    *,
    project_id: str,
    receipt_id: str | None,
) -> list[dict[str, Any]]:
    if not receipt_id:
        return []
    materialization = source_poll_materialization_from_receipt(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
    )
    if materialization is None:
        return []
    return trigger_source_materialized_watches(project, materialization)


def affected_enabled_watches(
    project: Project,
    materialization: SourcePollMaterialization,
) -> list[Any]:
    sheet_id = int(materialization["sheet_id"])
    out: list[Any] = []
    for watch in project.watches():
        if not bool(watch["enabled"]):
            continue
        # embedding_similarity watches are evaluated AFTER their index refresh job
        # completes (jobs/watches.py), not inline on the source poll — evaluating
        # them here would block on embedding_index_incomplete (rows not yet
        # embedded) and never re-fire. Non-embedding watches (fts/filter) still run
        # inline.
        if _is_embedding_similarity_watch(watch):
            continue
        if _watch_may_be_affected(watch, sheet_id=sheet_id):
            out.append(watch)
    return out


def _is_embedding_similarity_watch(watch: Any) -> bool:
    query = watch_query(watch)
    return str(query.get("kind") or "").strip().lower() == "embedding_similarity"


def _watch_may_be_affected(watch: Any, *, sheet_id: int) -> bool:
    scope = str(watch["scope"] or "project").strip().lower()
    query = watch_query(watch)
    query_scope = query.get("scope") if isinstance(query.get("scope"), dict) else {}
    query_scope_kind = str(query_scope.get("kind") or "").strip().lower()
    query_kind = str(query.get("kind") or "").strip().lower()
    query_sheet_id = _as_positive_int(
        query.get("sheet_id") or query_scope.get("sheet_id")
    )
    if scope == "project":
        if query_scope_kind == "sheet" or query_kind == "sheet.filter":
            return query_sheet_id == sheet_id
        return True
    if scope == "sheet":
        watch_sheet_id = _as_positive_int(watch["sheet_id"])
        return watch_sheet_id == sheet_id
    return False


def _receipt_refs(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in [
        *receipt.get("inputs", []),
        *receipt.get("outputs", []),
        *receipt.get("evidence", []),
    ]:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        if isinstance(ref, dict):
            refs.append(ref)
    return refs


def _positive_int_or_none(value: Any) -> int | None:
    out = _as_positive_int(value)
    return out if out is not None and out > 0 else None


def _as_positive_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None
