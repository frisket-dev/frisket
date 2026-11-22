"""One-time project cutover of stored receipt record keys and route targets."""

from __future__ import annotations

import hashlib
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


def _parse_json_dict(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


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


def _collect_legacy_python_column_ids(db: sqlite3.Connection) -> set[int]:
    column_ids: set[int] = set()
    rows = db.execute(
        "SELECT body FROM receipts WHERE body LIKE ?",
        (f"%{_LEGACY_PYTHON_REF_KIND}%",),
    ).fetchall()
    for (raw_body,) in rows:
        parsed = _parse_json_dict(raw_body)
        if parsed is None:
            continue
        items = parsed.get(_LEGACY_RECORDS_KEY)
        if not isinstance(items, list):
            items = parsed.get("records")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            if not isinstance(ref, dict) or ref.get("kind") != _LEGACY_PYTHON_REF_KIND:
                continue
            column_id = ref.get("column_id")
            if isinstance(column_id, int) and not isinstance(column_id, bool):
                column_ids.add(column_id)
    return column_ids


def _rename_hidden_record_columns(
    db: sqlite3.Connection,
    column_ids: set[int],
) -> tuple[dict[int, tuple[str, str]], dict[tuple[int, str], str]]:
    renamed_columns: dict[int, tuple[str, str]] = {}
    renamed_by_sheet: dict[tuple[int, str], str] = {}
    for column_id in sorted(column_ids):
        row = db.execute(
            "SELECT sheet_id, name, hidden FROM columns WHERE id=?",
            (column_id,),
        ).fetchone()
        if row is None:
            continue
        sheet_id, old_name, hidden = int(row[0]), str(row[1]), int(row[2])
        if hidden != 1 or not old_name.startswith(_LEGACY_COLUMN_PREFIX):
            continue
        new_name = "__record_" + old_name[len(_LEGACY_COLUMN_PREFIX) :]
        if _target_column_name_conflicts(db, sheet_id, new_name, column_id):
            continue
        db.execute(
            "UPDATE columns SET name=? WHERE id=?",
            (new_name, column_id),
        )
        renamed_columns[column_id] = (old_name, new_name)
        renamed_by_sheet[(sheet_id, old_name)] = new_name
    return renamed_columns, renamed_by_sheet


def _target_column_name_conflicts(
    db: sqlite3.Connection,
    sheet_id: int,
    target_name: str,
    column_id: int,
) -> bool:
    column_conflict = db.execute(
        "SELECT 1 FROM columns WHERE sheet_id=? AND name=? AND active=1",
        (sheet_id, target_name),
    ).fetchone()
    if column_conflict is not None:
        return True
    claim_conflict = db.execute(
        "SELECT 1 FROM output_column_claims "
        "WHERE sheet_id=? AND output_name=? AND status='active' "
        "AND (column_id IS NULL OR column_id!=?)",
        (sheet_id, target_name, column_id),
    ).fetchone()
    return claim_conflict is not None


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


def _rewrite_hidden_output_ref(
    ref: dict[str, Any],
    renamed_columns: dict[int, tuple[str, str]],
) -> bool:
    column_id = ref.get("column_id")
    if not isinstance(column_id, int) or isinstance(column_id, bool):
        return False
    renamed = renamed_columns.get(column_id)
    if renamed is None:
        return False
    old_name, new_name = renamed
    if ref.get("name") != old_name:
        return False
    ref["name"] = new_name
    return True


def _rewrite_record_item(
    item: Any,
    *,
    renamed_columns: dict[int, tuple[str, str]],
) -> bool:
    if not isinstance(item, dict):
        return False
    ref = item.get("ref")
    if not isinstance(ref, dict):
        return False
    kind = ref.get("kind")
    if kind == _LEGACY_PYTHON_REF_KIND:
        ref["kind"] = "map_python_receipt_record"
        return True
    if kind == "typed_hidden_output":
        return _rewrite_hidden_output_ref(ref, renamed_columns)
    if kind == "typed_action_request":
        return _rewrite_typed_action_request_ref(ref)
    if kind == "exported_receipts":
        return _rewrite_exported_receipts_ref(ref)
    return False


def _rewrite_receipt_body(
    body: dict[str, Any],
    *,
    renamed_columns: dict[int, tuple[str, str]],
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


def _rewrite_receipts(
    db: sqlite3.Connection,
    renamed_columns: dict[int, tuple[str, str]],
) -> None:
    rows = db.execute("SELECT id, body FROM receipts").fetchall()
    for receipt_id, raw_body in rows:
        parsed = _parse_json_dict(raw_body)
        if parsed is None:
            continue
        if _rewrite_receipt_body(parsed, renamed_columns=renamed_columns):
            db.execute(
                "UPDATE receipts SET body=? WHERE id=?",
                (json.dumps(parsed, sort_keys=True), receipt_id),
            )


def _update_column_metadata_tables(
    db: sqlite3.Connection,
    renamed_columns: dict[int, tuple[str, str]],
) -> None:
    for column_id, (old_name, new_name) in renamed_columns.items():
        db.execute(
            "UPDATE run_review_fields SET column_name=? "
            "WHERE column_id=? AND column_name=?",
            (new_name, column_id, old_name),
        )
        db.execute(
            "UPDATE output_column_claims SET output_name=? "
            "WHERE column_id=? AND output_name=?",
            (new_name, column_id, old_name),
        )


def _rewrite_output_name_mappings(
    mapping: dict[str, Any],
    *,
    sheet_id: int | None,
    renamed_columns: dict[int, tuple[str, str]],
    renamed_by_sheet: dict[tuple[int, str], str],
) -> bool:
    changed = False
    output_names = mapping.get("output_names")
    if isinstance(output_names, dict) and sheet_id is not None:
        for key, value in list(output_names.items()):
            if isinstance(value, str):
                new_name = renamed_by_sheet.get((sheet_id, value))
                if new_name is not None:
                    output_names[key] = new_name
                    changed = True
    preconditions = mapping.get("output_target_preconditions")
    if isinstance(preconditions, dict):
        rebuilt: dict[str, Any] = {}
        preconditions_changed = False
        for key, value in preconditions.items():
            new_key = _renamed_precondition_key(
                key, value, sheet_id, renamed_columns, renamed_by_sheet
            )
            if new_key != key:
                preconditions_changed = True
            rebuilt[new_key] = value
        if preconditions_changed:
            mapping["output_target_preconditions"] = rebuilt
            changed = True
    return changed


def _renamed_precondition_key(
    key: str,
    value: Any,
    sheet_id: int | None,
    renamed_columns: dict[int, tuple[str, str]],
    renamed_by_sheet: dict[tuple[int, str], str],
) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        renamed = renamed_columns.get(value)
        if renamed is not None and renamed[0] == key:
            return renamed[1]
    if sheet_id is not None:
        new_name = renamed_by_sheet.get((sheet_id, key))
        if new_name is not None:
            return new_name
    return key


def _rewrite_spec_payload(
    parsed: dict[str, Any],
    *,
    sheet_id: int | None,
    renamed_columns: dict[int, tuple[str, str]],
    renamed_by_sheet: dict[tuple[int, str], str],
) -> bool:
    changed = _rewrite_output_routes(parsed)
    nested = parsed.get("params")
    if isinstance(nested, dict):
        changed = _rewrite_output_routes(nested) or changed
    changed = (
        _rewrite_output_name_mappings(
            parsed,
            sheet_id=sheet_id,
            renamed_columns=renamed_columns,
            renamed_by_sheet=renamed_by_sheet,
        )
        or changed
    )
    return changed


def _rewrite_runs_and_changes(
    db: sqlite3.Connection,
    *,
    renamed_columns: dict[int, tuple[str, str]],
    renamed_by_sheet: dict[tuple[int, str], str],
) -> None:
    like_args = (f"%{_LEGACY_PYTHON_TARGET_KIND}%", f"%{_LEGACY_COLUMN_PREFIX}%")
    run_rows = db.execute(
        "SELECT id, sheet_id, params FROM runs WHERE params LIKE ? OR params LIKE ?",
        like_args,
    ).fetchall()
    for row_id, raw_sheet_id, raw_json in run_rows:
        parsed = _parse_json_dict(raw_json)
        if parsed is None:
            continue
        sheet_id = int(raw_sheet_id) if raw_sheet_id is not None else None
        if _rewrite_spec_payload(
            parsed,
            sheet_id=sheet_id,
            renamed_columns=renamed_columns,
            renamed_by_sheet=renamed_by_sheet,
        ):
            db.execute(
                "UPDATE runs SET params=? WHERE id=?",
                (json.dumps(parsed, sort_keys=True), row_id),
            )

    change_rows = db.execute(
        "SELECT id, spec FROM ops WHERE spec LIKE ? OR spec LIKE ?",
        like_args,
    ).fetchall()
    for row_id, raw_json in change_rows:
        parsed = _parse_json_dict(raw_json)
        if parsed is None:
            continue
        raw_sheet_id = parsed.get("sheet_id")
        sheet_id = (
            int(raw_sheet_id)
            if isinstance(raw_sheet_id, int) and not isinstance(raw_sheet_id, bool)
            else None
        )
        if _rewrite_spec_payload(
            parsed,
            sheet_id=sheet_id,
            renamed_columns=renamed_columns,
            renamed_by_sheet=renamed_by_sheet,
        ):
            db.execute(
                "UPDATE ops SET spec=? WHERE id=?",
                (json.dumps(parsed, sort_keys=True), row_id),
            )


def _rewrite_action_submissions(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT id, request_key, draft_json FROM action_submissions "
        "WHERE draft_json LIKE ?",
        (f"%{_LEGACY_PYTHON_TARGET_KIND}%",),
    ).fetchall()
    for row_id, request_key, raw_draft in rows:
        parsed = _parse_json_dict(raw_draft)
        if parsed is None:
            continue
        changed = _rewrite_output_routes(parsed)
        nested = parsed.get("params")
        if isinstance(nested, dict):
            changed = _rewrite_output_routes(nested) or changed
        if not changed:
            continue
        encoded = json.dumps(
            parsed, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        new_hash = hashlib.sha256(encoded.encode()).hexdigest()
        conflict = None
        if request_key is not None:
            conflict = db.execute(
                "SELECT 1 FROM action_submissions "
                "WHERE request_key=? AND draft_hash=? AND id!=?",
                (request_key, new_hash, row_id),
            ).fetchone()
        if conflict is not None:
            db.execute(
                "UPDATE action_submissions SET draft_json=? WHERE id=?",
                (encoded, row_id),
            )
        else:
            db.execute(
                "UPDATE action_submissions SET draft_json=?, draft_hash=? WHERE id=?",
                (encoded, new_hash, row_id),
            )


def apply_receipt_records(db: sqlite3.Connection) -> None:
    """Rewrite stored receipt bodies, route specs, and aligned hidden columns."""
    candidate_columns = _collect_legacy_python_column_ids(db)
    renamed_columns, renamed_by_sheet = _rename_hidden_record_columns(
        db, candidate_columns
    )
    _rewrite_receipts(db, renamed_columns)
    _update_column_metadata_tables(db, renamed_columns)
    _rewrite_runs_and_changes(
        db,
        renamed_columns=renamed_columns,
        renamed_by_sheet=renamed_by_sheet,
    )
    _rewrite_action_submissions(db)
    db.execute(
        "INSERT INTO meta(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RECEIPT_RECORDS_META_KEY, RECEIPT_RECORDS_VERSION),
    )
