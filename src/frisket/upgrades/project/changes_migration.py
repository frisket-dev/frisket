"""Data-preserving project schema step cutting over ops -> changes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from frisket.engine.store.consumed_sources import capture_fingerprint
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project._changes_target_ddl import (
    TARGET_INDEX_DDLS,
    TARGET_TABLE_DDLS,
    TARGET_TRIGGER_DDLS,
    TARGET_VIEW_DDLS,
)
from frisket.upgrades.project.feeds_migration import FEEDS_TO_DIGEST

CHANGES_FROM_DIGEST = FEEDS_TO_DIGEST
CHANGES_TO_DIGEST = "frisket.schema.v1:5cb6590bae29da5c24fd43d0b3f4116f"

_SIMPLE_COLUMN_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("sheets", "parent_op_id", "parent_change_id"),
    ("sheets", "last_verified_op_cursor", "last_verified_change_cursor"),
    ("citations", "op_id", "change_id"),
    ("alerts", "last_evaluated_op", "last_evaluated_change"),
    ("alert_checks", "op_cursor_before", "change_cursor_before"),
    ("alert_checks", "op_cursor_after", "change_cursor_after"),
)

_TABLE_REBUILDS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("ops", "changes", {}),
    ("base_cell_producers", "base_cell_producers", {"change_id": "op_id"}),
    ("runs", "runs", {"change_id": "op_id"}),
    ("import_sessions", "import_sessions", {"change_id": "op_id"}),
    ("generated_column_claims", "generated_column_claims", {"change_id": "op_id"}),
    (
        "prepared_page_versions",
        "prepared_page_versions",
        {"producing_change_id": "producing_op_id"},
    ),
    (
        "prepared_content_sets",
        "prepared_content_sets",
        {"producing_change_id": "producing_op_id"},
    ),
    ("edits", "edits", {"change_id": "op_id"}),
    ("current_cells", "current_cells", {"origin_change_id": "origin_op_id"}),
    ("feed_checks", "feed_checks", {"change_id": "op_id"}),
    ("views", "views", {"change_id": "op_id"}),
    ("cell_baseline", "cell_baseline", {"origin_change_id": "origin_op_id"}),
    ("materialized_row_sources", "materialized_row_sources", {"change_id": "op_id"}),
    (
        "document_text_manual_pins",
        "document_text_manual_pins",
        {"edit_change_id": "edit_op_id"},
    ),
    ("copied_result_outcomes", "copied_result_outcomes", {"change_id": "op_id"}),
    (
        "consumed_source_bundles",
        "consumed_source_bundles",
        {"owner_change_id": "owner_op_id"},
    ),
    ("column_descriptor_changes", "column_descriptor_changes", {"change_id": "op_id"}),
    ("structural_changes", "structural_changes", {"change_id": "op_id"}),
    ("result_judgment_changes", "result_judgment_changes", {"change_id": "op_id"}),
)

_ACTION_KIND_MAP = {
    "operation.undo": "change.undo",
    "operation.redo": "change.redo",
    "operation.revert": "change.revert",
    "operation.restore": "change.restore",
}

_RECEIPT_REF_KIND_MAP = {
    "target_operation": "target_change",
    "operation_status_transition": "change_status_transition",
    "operation_cursor": "change_cursor",
    "operation_undo_info": "change_undo_info",
    "operation_snapshot": "change_snapshot",
}

_RECEIPT_ERROR_CODE_MAP = {
    "operation_unavailable": "change_unavailable",
    "operation_mismatch": "change_mismatch",
}

_RECEIPT_FIELD_MAP = {
    "params.expected_op_id": "params.expected_change_id",
    "params.op_id": "params.change_id",
    "params.copied_op_id": "params.copied_change_id",
}

_CHANGE_KEY_MAP = {
    "op_id": "change_id",
    "op_ids": "change_ids",
    "parent_op_id": "parent_change_id",
    "copied_op_id": "copied_change_id",
    "source_op_ids": "source_change_ids",
    "expected_op_id": "expected_change_id",
    "actual_op_id": "actual_change_id",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _rewrite_keys_recursive(value: Any, key_map: dict[str, str]) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            rewritten, item_changed = _rewrite_keys_recursive(item, key_map)
            items.append(rewritten)
            changed = changed or item_changed
        return items, changed
    if not isinstance(value, dict):
        return value, False
    changed = False
    out: dict[str, Any] = {}
    for key, item in value.items():
        key_str = str(key)
        new_key = key_map.get(key_str, key_str)
        if new_key != key_str:
            changed = True
        rewritten, item_changed = _rewrite_keys_recursive(item, key_map)
        changed = changed or item_changed
        out[new_key] = rewritten
    return out, changed


def _rewrite_receipt_ref(ref: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    rewritten, changed = _rewrite_keys_recursive(ref, _CHANGE_KEY_MAP)
    kind = rewritten.get("kind")
    if isinstance(kind, str) and kind in _RECEIPT_REF_KIND_MAP:
        rewritten["kind"] = _RECEIPT_REF_KIND_MAP[kind]
        changed = True
    if rewritten.get("kind") in set(_RECEIPT_REF_KIND_MAP.values()) and (
        "operation_kind" in rewritten
    ):
        rewritten["change_kind"] = rewritten.pop("operation_kind")
        changed = True
    return rewritten, changed


def _rewrite_receipt_io_list(items: Any) -> bool:
    if not isinstance(items, list):
        return False
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("name") == "target_operation":
            item["name"] = "target_change"
            changed = True
        if item.get("kind") == "operation":
            item["kind"] = "change"
            changed = True
        ref = item.get("ref")
        if isinstance(ref, dict):
            new_ref, ref_changed = _rewrite_receipt_ref(ref)
            if ref_changed:
                item["ref"] = new_ref
                changed = True
    return changed


def _rewrite_receipt_errors(errors: Any) -> bool:
    if not isinstance(errors, list):
        return False
    changed = False
    for err in errors:
        if not isinstance(err, dict):
            continue
        action_kind = err.get("action_kind")
        if isinstance(action_kind, str) and action_kind in _ACTION_KIND_MAP:
            err["action_kind"] = _ACTION_KIND_MAP[action_kind]
            changed = True
        code = err.get("code")
        if isinstance(code, str) and code in _RECEIPT_ERROR_CODE_MAP:
            err["code"] = _RECEIPT_ERROR_CODE_MAP[code]
            changed = True
        field = err.get("field")
        if isinstance(field, str) and field in _RECEIPT_FIELD_MAP:
            err["field"] = _RECEIPT_FIELD_MAP[field]
            changed = True
        details = err.get("details")
        if isinstance(details, dict):
            new_details, det_changed = _rewrite_keys_recursive(details, _CHANGE_KEY_MAP)
            if "operation_kind" in new_details and (
                err.get("code") == "irreversible_barrier"
            ):
                new_details["change_kind"] = new_details.pop("operation_kind")
                det_changed = True
            if det_changed:
                err["details"] = new_details
                changed = True
    return changed


def _rewrite_receipt_value(parsed: dict[str, Any]) -> bool:
    value = parsed.get("value")
    if not isinstance(value, dict):
        return False
    new_value, val_changed = _rewrite_keys_recursive(value, _CHANGE_KEY_MAP)
    if not val_changed:
        return False
    parsed["value"] = new_value
    return True


def _rewrite_receipt_body(raw_body: str) -> tuple[str, bool]:
    try:
        parsed = json.loads(raw_body)
    except (TypeError, ValueError):
        return raw_body, False
    if not isinstance(parsed, dict):
        return raw_body, False
    changed = False
    if "op_ids" in parsed:
        parsed["change_ids"] = parsed.pop("op_ids")
        changed = True
    action_kind = parsed.get("action_kind")
    if isinstance(action_kind, str) and action_kind in _ACTION_KIND_MAP:
        parsed["action_kind"] = _ACTION_KIND_MAP[action_kind]
        changed = True
    changed = (
        any(
            _rewrite_receipt_io_list(parsed.get(s))
            for s in ("inputs", "outputs", "records")
        )
        or changed
    )
    changed = _rewrite_receipt_errors(parsed.get("errors")) or changed
    changed = _rewrite_receipt_value(parsed) or changed
    if not changed:
        return raw_body, False
    return json.dumps(parsed), True


def _rebuild_table(
    db: sqlite3.Connection,
    old_name: str,
    new_name: str,
    new_to_old_columns: dict[str, str],
) -> None:
    target_ddl = TARGET_TABLE_DDLS[new_name]
    temp_name = f"__changes_mig_{new_name}"
    create_sql = re.sub(
        r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(new_name) + r"\b",
        f"CREATE TABLE {temp_name}",
        target_ddl,
        count=1,
        flags=re.IGNORECASE,
    )
    high_water = db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name=?", (old_name,)
    ).fetchone()
    db.execute(create_sql)
    target_cols = [
        str(row[1]) for row in db.execute(f'PRAGMA table_info("{temp_name}")')
    ]
    select_cols = [new_to_old_columns.get(col, col) for col in target_cols]
    target_list = ",".join(f'"{col}"' for col in target_cols)
    select_list = ",".join(f'"{col}"' for col in select_cols)
    db.execute(
        f'INSERT INTO "{temp_name}" ({target_list}) '
        f'SELECT {select_list} FROM "{old_name}"'
    )
    db.execute(f'DROP TABLE "{old_name}"')
    db.execute(f'ALTER TABLE "{temp_name}" RENAME TO "{new_name}"')
    for idx_sql in TARGET_INDEX_DDLS[new_name]:
        db.execute(idx_sql)
    if high_water is not None:
        if old_name != new_name:
            db.execute("DELETE FROM sqlite_sequence WHERE name=?", (old_name,))
        updated = db.execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?",
            (high_water[0], new_name),
        ).rowcount
        if not updated:
            db.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)",
                (new_name, high_water[0]),
            )


def _migrate_receipts(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT id, action_kind, body FROM receipts").fetchall()
    for receipt_id, action_kind, body in rows:
        new_kind = _ACTION_KIND_MAP.get(action_kind, action_kind)
        new_body, body_changed = (
            _rewrite_receipt_body(body) if isinstance(body, str) else (body, False)
        )
        if new_kind != action_kind or body_changed:
            db.execute(
                "UPDATE receipts SET action_kind=?, body=? WHERE id=?",
                (new_kind, new_body, receipt_id),
            )


def _encode_json(value: Any, encoding: str) -> str:
    if encoding == "canonical":
        return _canonical_json(value)
    if encoding == "canonical_utf8":
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    return json.dumps(value)


def _migrate_json_column(
    db: sqlite3.Connection,
    target: tuple[str, tuple[str, ...], str],
    key_map: dict[str, str],
    *,
    encoding: str = "default",
) -> None:
    table, pk_cols, json_col = target
    pk_select = ", ".join(pk_cols)
    where_pk = " AND ".join(f"{col}=?" for col in pk_cols)
    like_clauses = " OR ".join(f"{json_col} LIKE ?" for _ in key_map)
    params = [f'%"{old_key}"%' for old_key in key_map]
    rows = db.execute(
        f"SELECT {pk_select}, {json_col} FROM {table} "
        f"WHERE {json_col} IS NOT NULL AND ({like_clauses})",
        params,
    ).fetchall()
    for row in rows:
        pk_vals = row[:-1]
        raw = row[-1]
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        rewritten, changed = _rewrite_keys_recursive(parsed, key_map)
        if not changed:
            continue
        encoded = _encode_json(rewritten, encoding)
        db.execute(
            f"UPDATE {table} SET {json_col}=? WHERE {where_pk}",
            (encoded, *pk_vals),
        )


def _remap_shared_inputs_list(
    capture: dict[str, Any], shared_fp_map: dict[str, str]
) -> bool:
    if not shared_fp_map:
        return False
    shared_inputs = capture.get("shared_inputs")
    if not isinstance(shared_inputs, list):
        return False
    remapped = [
        shared_fp_map.get(fp, fp) if isinstance(fp, str) else fp for fp in shared_inputs
    ]
    if remapped == shared_inputs:
        return False
    capture["shared_inputs"] = remapped
    return True


def _migrate_aggregate_consumed_sources(db: sqlite3.Connection) -> dict[str, str]:
    shared_fp_map: dict[str, str] = {}
    rows = db.execute(
        "SELECT id, capture_key, input_fingerprint, capture_json "
        "FROM consumed_source_bundles "
        "WHERE kind='aggregate' AND capture_json LIKE '%\"op_id\"%'"
    ).fetchall()
    for bundle_id, capture_key, old_fp, raw in rows:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        rewritten, changed = _rewrite_keys_recursive(parsed, {"op_id": "change_id"})
        if not changed or not isinstance(rewritten, dict):
            continue
        new_fp = capture_fingerprint(rewritten)
        if isinstance(old_fp, str) and new_fp != old_fp:
            shared_fp_map[old_fp] = new_fp
        new_key = capture_key
        if isinstance(capture_key, str) and ":shared:" in capture_key:
            prefix = capture_key.split(":shared:", 1)[0]
            new_key = f"{prefix}:shared:{new_fp}"
        db.execute(
            "UPDATE consumed_source_bundles "
            "SET capture_key=?, input_fingerprint=?, capture_json=? WHERE id=?",
            (new_key, new_fp, _canonical_json(rewritten), bundle_id),
        )
    return shared_fp_map


def _migrate_consumed_sources(db: sqlite3.Connection) -> dict[str, str]:
    shared_fp_map = _migrate_aggregate_consumed_sources(db)
    where_clause = "kind='row' AND (capture_json LIKE '%\"op_id\"%'"
    if shared_fp_map:
        where_clause += " OR capture_json LIKE '%\"shared_inputs\"%'"
    where_clause += ")"
    rows = db.execute(
        f"SELECT id, capture_json FROM consumed_source_bundles WHERE {where_clause}"
    ).fetchall()
    for bundle_id, raw in rows:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        rewritten, changed = _rewrite_keys_recursive(parsed, {"op_id": "change_id"})
        if not isinstance(rewritten, dict):
            continue
        changed = _remap_shared_inputs_list(rewritten, shared_fp_map) or changed
        if not changed:
            continue
        new_fp = capture_fingerprint(rewritten)
        db.execute(
            "UPDATE consumed_source_bundles "
            "SET input_fingerprint=?, capture_json=? WHERE id=?",
            (new_fp, _canonical_json(rewritten), bundle_id),
        )
    return shared_fp_map


def _migrate_effect_checkpoints(
    db: sqlite3.Connection, shared_fp_map: dict[str, str]
) -> None:
    where_clause = "payload IS NOT NULL AND (payload LIKE '%\"op_id\"%'"
    if shared_fp_map:
        where_clause += " OR payload LIKE '%\"shared_inputs\"%'"
    where_clause += ")"
    rows = db.execute(
        f"SELECT id, payload FROM effect_checkpoints WHERE {where_clause}"
    ).fetchall()
    for checkpoint_id, raw in rows:
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        rewritten, changed = _rewrite_keys_recursive(parsed, {"op_id": "change_id"})
        if isinstance(rewritten, dict) and isinstance(
            rewritten.get("__consumed_sources"), dict
        ):
            changed = (
                _remap_shared_inputs_list(
                    rewritten["__consumed_sources"], shared_fp_map
                )
                or changed
            )
        if not changed:
            continue
        db.execute(
            "UPDATE effect_checkpoints SET payload=? WHERE id=?",
            (json.dumps(rewritten), checkpoint_id),
        )


def _rewrite_submission_draft(parsed: dict[str, Any]) -> bool:
    changed = False
    if parsed.get("action_id") in _ACTION_KIND_MAP:
        parsed["action_id"] = _ACTION_KIND_MAP[parsed["action_id"]]
        changed = True
    params = parsed.get("params")
    if isinstance(params, dict):
        new_params, params_changed = _rewrite_keys_recursive(
            params,
            {
                "copied_op_id": "copied_change_id",
                "expected_op_id": "expected_change_id",
                "op_id": "change_id",
            },
        )
        if params_changed:
            parsed["params"] = new_params
            changed = True
    return changed


def _migrate_action_submissions(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT id, request_key, action_id, draft_json FROM action_submissions "
        "WHERE action_id IN ('operation.undo','operation.redo','operation.revert','operation.restore') "
        "OR draft_json LIKE '%\"copied_op_id\"%' "
        "OR draft_json LIKE '%\"expected_op_id\"%' "
        "OR draft_json LIKE '%\"op_id\"%'"
    ).fetchall()
    for sub_id, request_key, action_id, raw in rows:
        new_action_id = _ACTION_KIND_MAP.get(action_id, action_id)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        changed = _rewrite_submission_draft(parsed) or (new_action_id != action_id)
        if not changed:
            continue
        encoded = _canonical_json(parsed)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        conflict = (
            db.execute(
                "SELECT 1 FROM action_submissions "
                "WHERE request_key=? AND draft_hash=? AND id!=?",
                (request_key, digest, sub_id),
            ).fetchone()
            if request_key is not None
            else None
        )
        if conflict is not None:
            db.execute(
                "UPDATE action_submissions SET action_id=?, draft_json=? WHERE id=?",
                (new_action_id, encoded, sub_id),
            )
        else:
            db.execute(
                "UPDATE action_submissions SET action_id=?, draft_hash=?, draft_json=? "
                "WHERE id=?",
                (new_action_id, digest, encoded, sub_id),
            )


def _migrate_params_and_specs(
    db: sqlite3.Connection, shared_fp_map: dict[str, str]
) -> None:
    key_map = {
        "copied_op_id": "copied_change_id",
        "expected_op_id": "expected_change_id",
        "import_op_id": "import_change_id",
        "op_id": "change_id",
        "op_ids": "change_ids",
        "parent_op_id": "parent_change_id",
        "source_op_id": "source_change_id",
        "source_op_ids": "source_change_ids",
    }
    _migrate_json_column(db, ("changes", ("id",), "spec"), key_map)
    _migrate_json_column(db, ("runs", ("id",), "params"), key_map)
    _migrate_json_column(
        db,
        ("citations", ("id",), "subject_ref_json"),
        {"op_id": "change_id"},
        encoding="canonical",
    )
    db.execute("DROP TRIGGER IF EXISTS trg_text_surfaces_no_update")
    try:
        _migrate_json_column(
            db,
            ("text_surfaces", ("id",), "value_ref_json"),
            {"op_id": "change_id"},
            encoding="canonical",
        )
    finally:
        db.execute(TARGET_TRIGGER_DDLS["trg_text_surfaces_no_update"])
    _migrate_effect_checkpoints(db, shared_fp_map)
    _migrate_json_column(
        db, ("feed_checks", ("id",), "summary_json"), {"op_id": "change_id"}
    )
    qa_key_map = {
        "source_op_cursor": "source_change_cursor",
        "op_id": "change_id",
        "op_ids": "change_ids",
    }
    _migrate_json_column(
        db,
        ("project_qa_citations", ("id",), "locator_json"),
        qa_key_map,
        encoding="canonical_utf8",
    )
    _migrate_json_column(
        db,
        ("project_qa_events", ("thread_id", "seq"), "payload_json"),
        qa_key_map,
        encoding="canonical_utf8",
    )
    for col in ("source_ref", "deep_link", "payload"):
        _migrate_json_column(
            db,
            ("notification_items", ("id",), col),
            {"op_id": "change_id", "op_ids": "change_ids"},
        )


def _rename_schema_objects(db: sqlite3.Connection) -> None:
    for view_name, _ in reversed(TARGET_VIEW_DDLS):
        db.execute(f"DROP VIEW IF EXISTS {view_name}")
    for trig_name in TARGET_TRIGGER_DDLS:
        if trig_name != "trg_text_surfaces_no_update":
            db.execute(f"DROP TRIGGER IF EXISTS {trig_name}")
    for table, old_col, new_col in _SIMPLE_COLUMN_RENAMES:
        cols = {str(r[1]) for r in db.execute(f'PRAGMA table_info("{table}")')}
        if old_col in cols and new_col not in cols:
            db.execute(
                f'ALTER TABLE "{table}" RENAME COLUMN "{old_col}" TO "{new_col}"'
            )
    for old_name, new_name, col_map in _TABLE_REBUILDS:
        _rebuild_table(db, old_name, new_name, col_map)
    for trig_name, trig_sql in TARGET_TRIGGER_DDLS.items():
        if trig_name != "trg_text_surfaces_no_update":
            db.execute(trig_sql)
    for _, view_sql in TARGET_VIEW_DDLS:
        db.execute(view_sql)


def _migrate_cursor_meta(db: sqlite3.Connection) -> None:
    legacy_cursor_row = db.execute(
        "SELECT value FROM meta WHERE key='op_cursor'"
    ).fetchone()
    if legacy_cursor_row is not None:
        db.execute(
            "INSERT INTO meta(key,value) VALUES ('change_cursor',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (legacy_cursor_row[0],),
        )
        db.execute("DELETE FROM meta WHERE key='op_cursor'")


def migrate_changes(db: sqlite3.Connection) -> None:
    """Rename ops -> changes across schema, triggers, views, meta, and JSON payloads."""
    tables = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "ops" in tables:
        _rename_schema_objects(db)

    _migrate_cursor_meta(db)
    _migrate_receipts(db)
    shared_fp_map = _migrate_consumed_sources(db)
    _migrate_action_submissions(db)
    _migrate_params_and_specs(db, shared_fp_map)

    violation = db.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise BundleSchemaMismatch(
            f"Changes upgrade found a broken reference: {tuple(violation)}"
        )
