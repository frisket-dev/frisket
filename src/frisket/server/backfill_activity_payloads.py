"""Backfill activity payloads from durable v1 action receipts."""

from __future__ import annotations

from typing import Any

from frisket.server.paging import offset_page_meta
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


def _backfill_output_ref(body: dict[str, Any]) -> dict[str, Any]:
    outputs = body.get("outputs") or []
    if not outputs:
        return {}
    ref = outputs[0].get("ref") if isinstance(outputs[0], dict) else None
    if isinstance(ref, dict) and ref.get("kind") == "run_backfill":
        return ref
    return {}


def _activity_row(row: Any) -> dict[str, Any] | None:
    import json

    body = json.loads(row["body"])
    ref = _backfill_output_ref(body)
    try:
        sheet_id = int(ref["sheet_id"])
        column_id = int(ref["column_id"])
    except (KeyError, TypeError, ValueError):
        return None
    requested_row_ids = list(ref.get("requested_row_ids") or [])
    filled_row_ids = list(ref.get("filled_row_ids") or [])
    filled_value = ref.get("filled")
    filled_count = (
        int(filled_value) if filled_value is not None else len(filled_row_ids)
    )
    column_name = str(ref.get("column_name") or "")
    return {
        "schema_version": "frisket.backfill_activity.v1",
        "receipt_id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "run_id": row["run_id"] if row["run_id"] is not None else ref.get("run_id"),
        "sheet_id": sheet_id,
        "column_id": column_id,
        "column_name": column_name,
        "requested_count": len(requested_row_ids),
        "filled_count": filled_count,
        "label": f"Backfilled {filled_count} cell{'s' if filled_count != 1 else ''} in {column_name}",
        "restorable": False,
    }


def backfill_activity_payload(
    p: Project,
    *,
    offset: int = 0,
    limit: int = 25,
    column_id: int | None = None,
) -> dict[str, Any]:
    """Return newest-first backfill receipt activity without scanning all receipts."""
    receipt_store = ReceiptStore(p)
    total = receipt_store.backfill_activity_count(column_id=column_id)
    rows = receipt_store.backfill_activity_rows(
        column_id=column_id,
        limit=limit,
        offset=offset,
    )
    items = [item for row in rows if (item := _activity_row(row)) is not None]
    return {
        "schema_version": "frisket.backfill_activity_page.v1",
        "backfills": items,
        "page": offset_page_meta(
            schema_version="frisket.backfill_activity_page.v1",
            order="desc",
            offset=offset,
            limit=limit,
            total=total,
            item_count=len(items),
        ),
    }
