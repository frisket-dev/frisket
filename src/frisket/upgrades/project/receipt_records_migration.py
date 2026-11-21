"""One-time project cutover of stored receipt record keys and route targets."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from frisket.engine.store.schema import (
    RECEIPT_RECORDS_META_KEY,
    RECEIPT_RECORDS_VERSION,
)

RECEIPT_RECORDS_TO_DIGEST = "frisket.schema.v1:133e8cf312131293db39f8e8f91caf6d"

_LEGACY_RECORDS_KEY = "evidence"
_LEGACY_PYTHON_TARGET_KIND = "receipt_evidence"
_LEGACY_PYTHON_REF_KIND = "map_python_receipt_evidence"
_LEGACY_COLUMN_PREFIX = "__evidence_"


def _rewrite_output_routes(mapping: dict[str, Any]) -> bool:
    routes = mapping.get("output_routes")
    if not isinstance(routes, list):
        return False
    changed = False
    for route in routes:
        if not isinstance(route, dict):
            continue
        target = route.get("target")
        if (
            isinstance(target, dict)
            and target.get("kind") == _LEGACY_PYTHON_TARGET_KIND
        ):
            target["kind"] = "receipt_record"
            changed = True
    return changed


def _rewrite_typed_action_request_ref(ref: dict[str, Any]) -> bool:
    changed = False
    params = ref.get("params")
    if isinstance(params, dict):
        changed = _rewrite_output_routes(params) or changed
    request = ref.get("request")
    if isinstance(request, dict):
        req_params = request.get("params")
        if isinstance(req_params, dict):
            changed = _rewrite_output_routes(req_params) or changed
    return changed


def _rewrite_exported_receipts_ref(ref: dict[str, Any]) -> bool:
    exported = ref.get("receipts")
    if not isinstance(exported, list):
        return False
    changed = False
    for entry in exported:
        if isinstance(entry, dict) and _LEGACY_RECORDS_KEY in entry:
            legacy = entry.pop(_LEGACY_RECORDS_KEY)
            entry.setdefault("records", legacy)
            changed = True
    return changed


def _rewrite_record_item(
    item: Any,
    *,
    renamed_columns: set[int],
) -> bool:
    if not isinstance(item, dict):
        return False
    ref = item.get("ref")
    if not isinstance(ref, dict):
        return False
    kind = ref.get("kind")
    if kind == _LEGACY_PYTHON_REF_KIND:
        ref["kind"] = "map_python_receipt_record"
        column_id = ref.get("column_id")
        if isinstance(column_id, int) and not isinstance(column_id, bool):
            renamed_columns.add(column_id)
        return True
    if kind == "typed_action_request":
        return _rewrite_typed_action_request_ref(ref)
    if kind == "exported_receipts":
        return _rewrite_exported_receipts_ref(ref)
    return False


def _rewrite_receipt_body(
    body: dict[str, Any],
    *,
    renamed_columns: set[int],
) -> bool:
    changed = False
    if _LEGACY_RECORDS_KEY in body:
        legacy = body.pop(_LEGACY_RECORDS_KEY)
        body.setdefault("records", legacy)
        changed = True
    records = body.get("records")
    if isinstance(records, list):
        for item in records:
            changed = (
                _rewrite_record_item(item, renamed_columns=renamed_columns) or changed
            )
    return changed


def _rewrite_receipts(db: sqlite3.Connection) -> set[int]:
    renamed_columns: set[int] = set()
    rows = db.execute("SELECT id, body FROM receipts").fetchall()
    for receipt_id, raw_body in rows:
        if not isinstance(raw_body, str) or not raw_body.strip():
            continue
        try:
            parsed = json.loads(raw_body)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        if _rewrite_receipt_body(parsed, renamed_columns=renamed_columns):
            db.execute(
                "UPDATE receipts SET body=? WHERE id=?",
                (json.dumps(parsed, sort_keys=True), receipt_id),
            )
    return renamed_columns


def _rewrite_python_route_specs(db: sqlite3.Connection) -> None:
    for table, column in (("runs", "params"), ("ops", "spec")):
        rows = db.execute(
            f"SELECT id, {column} FROM {table} WHERE {column} LIKE ?",
            (f"%{_LEGACY_PYTHON_TARGET_KIND}%",),
        ).fetchall()
        for row_id, raw_json in rows:
            try:
                parsed = json.loads(raw_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            changed = _rewrite_output_routes(parsed)
            nested = parsed.get("params")
            if isinstance(nested, dict):
                changed = _rewrite_output_routes(nested) or changed
            if changed:
                db.execute(
                    f"UPDATE {table} SET {column}=? WHERE id=?",
                    (json.dumps(parsed, sort_keys=True), row_id),
                )


def _rename_hidden_record_columns(
    db: sqlite3.Connection,
    column_ids: set[int],
) -> None:
    for column_id in sorted(column_ids):
        row = db.execute(
            "SELECT sheet_id, name, hidden FROM columns WHERE id=?",
            (column_id,),
        ).fetchone()
        if row is None:
            continue
        sheet_id, name, hidden = row[0], str(row[1]), int(row[2])
        if hidden != 1 or not name.startswith(_LEGACY_COLUMN_PREFIX):
            continue
        target_name = "__record_" + name[len(_LEGACY_COLUMN_PREFIX) :]
        conflict = db.execute(
            "SELECT 1 FROM columns WHERE sheet_id=? AND name=? AND active=1",
            (sheet_id, target_name),
        ).fetchone()
        if conflict is not None:
            continue
        db.execute(
            "UPDATE columns SET name=? WHERE id=?",
            (target_name, column_id),
        )


def apply_receipt_records(db: sqlite3.Connection) -> None:
    """Rewrite stored receipt bodies and Python route targets in one step."""
    renamed_columns = _rewrite_receipts(db)
    _rewrite_python_route_specs(db)
    _rename_hidden_record_columns(db, renamed_columns)
    db.execute(
        "INSERT INTO meta(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RECEIPT_RECORDS_META_KEY, RECEIPT_RECORDS_VERSION),
    )
