"""Data-preserving project schema step for feed tables, receipts, and notifications."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from frisket.engine.store.schema import BundleSchemaMismatch, SCHEMA_DIGEST
from frisket.upgrades.project._step import execute_ddl

FEEDS_TO_DIGEST = "frisket.schema.v1:c85debf6ee343fcbde2cb862d18a0544"

_FEEDS_DDL = """
CREATE TABLE IF NOT EXISTS feeds (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'url',
  url TEXT,
  config TEXT NOT NULL DEFAULT '{}',
  sheet_id INTEGER REFERENCES sheets(id) ON DELETE SET NULL,
  schedule TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  cursor TEXT,
  last_checked_at TEXT,
  last_status TEXT,
  new_rows_total INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feeds_sheet ON feeds(sheet_id);

CREATE TABLE IF NOT EXISTS feed_checks (
  id INTEGER PRIMARY KEY,
  feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  op_id INTEGER REFERENCES ops(id) ON DELETE SET NULL,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  new_rows INTEGER NOT NULL DEFAULT 0,
  skipped_rows INTEGER NOT NULL DEFAULT 0,
  changed_rows INTEGER NOT NULL DEFAULT 0,
  revisions INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  cursor_before TEXT,
  cursor_after TEXT,
  duration_ms INTEGER,
  warning_count INTEGER NOT NULL DEFAULT 0,
  cost_micro INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_checks_feed_started
  ON feed_checks(feed_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_feed_checks_receipt ON feed_checks(receipt_id);
"""

_ACTION_KIND_MAP = {
    "source.create": "feed.create",
    "source.poll": "feed.poll",
    "source.update": "feed.update",
    "source.delete": "feed.delete",
    "source.check": "feed.check",
}

_IO_NAME_MAP = {
    "source": "feed",
    "source_before": "feed_before",
    "source_run": "feed_check",
}

_REF_KIND_MAP = {
    "source_create_source": "feed_create_feed",
    "source_create_request": "feed_create_request",
    "source_update_source": "feed_update_feed",
    "source_update_request": "feed_update_request",
    "source_update_before": "feed_update_before",
    "source_delete_source": "feed_delete_feed",
    "source_delete_request": "feed_delete_request",
    "source_check_source": "feed_check_feed",
    "source_check_request": "feed_check_request",
    "source_check_run": "feed_check_record",
    "source_check_cursor": "feed_check_cursor",
    "source_poll_source": "feed_poll_feed",
    "source_poll_run": "feed_poll_check",
    "source_poll_request": "feed_poll_request",
    "source_sheet": "feed_sheet",
    "source_poll_rows": "feed_poll_rows",
    "source_poll_cursor": "feed_poll_cursor",
    "source_poll_artifacts": "feed_poll_artifacts",
    "source_poll_enclosure_pointers": "feed_poll_enclosure_pointers",
}

_REF_KEY_MAP = {
    "source_id": "feed_id",
    "source_kind": "feed_kind",
    "source_run_id": "feed_check_id",
    "source_run_count": "feed_check_count",
}

_ERROR_CODE_MAP = {
    "unsupported_source_kind": "unsupported_feed_kind",
    "unsupported_source_config": "unsupported_feed_config",
    "source_poll_failed": "feed_poll_failed",
    "source_poll_invalid": "feed_poll_invalid",
}


def _rewrite_dict_keys(
    mapping: dict[str, Any], key_map: dict[str, str]
) -> dict[str, Any]:
    return {key_map.get(key, key): value for key, value in mapping.items()}


def _rewrite_receipt_io_item(item: Any) -> None:
    if not isinstance(item, dict):
        return
    name = item.get("name")
    if isinstance(name, str) and name in _IO_NAME_MAP:
        item["name"] = _IO_NAME_MAP[name]
    ref = item.get("ref")
    if not isinstance(ref, dict):
        return
    rewritten_ref = _rewrite_dict_keys(ref, _REF_KEY_MAP)
    kind = rewritten_ref.get("kind")
    if isinstance(kind, str) and kind in _REF_KIND_MAP:
        rewritten_ref["kind"] = _REF_KIND_MAP[kind]
    item["ref"] = rewritten_ref


def _rewrite_receipt_error_item(err: Any) -> None:
    if not isinstance(err, dict):
        return
    err_action = err.get("action_kind")
    if isinstance(err_action, str) and err_action in _ACTION_KIND_MAP:
        err["action_kind"] = _ACTION_KIND_MAP[err_action]
    code = err.get("code")
    if isinstance(code, str) and code in _ERROR_CODE_MAP:
        err["code"] = _ERROR_CODE_MAP[code]
    details = err.get("details")
    if isinstance(details, dict):
        err["details"] = _rewrite_dict_keys(details, _REF_KEY_MAP)


def _rewrite_receipt_body(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return body
    if not isinstance(parsed, dict):
        return body
    action_kind = parsed.get("action_kind")
    if isinstance(action_kind, str) and action_kind in _ACTION_KIND_MAP:
        parsed["action_kind"] = _ACTION_KIND_MAP[action_kind]
    for section in ("inputs", "outputs", "evidence"):
        items = parsed.get(section)
        if isinstance(items, list):
            for item in items:
                _rewrite_receipt_io_item(item)
    errors = parsed.get("errors")
    if isinstance(errors, list):
        for err in errors:
            _rewrite_receipt_error_item(err)
    return json.dumps(parsed)


def _rewrite_json_object(
    raw: str, key_map: dict[str, str], *, rewrite_kind: bool = False
) -> str:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(parsed, dict):
        return raw
    rewritten = _rewrite_dict_keys(parsed, key_map)
    if rewrite_kind and rewritten.get("kind") == "source":
        rewritten["kind"] = "feed"
    return json.dumps(rewritten)


def _migrate_receipts(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT id, action_kind, body FROM receipts "
        "WHERE action_kind IN ('source.create','source.poll','source.update','source.delete','source.check')"
    ).fetchall()
    for receipt_id, action_kind, body in rows:
        new_kind = _ACTION_KIND_MAP[action_kind]
        new_body = _rewrite_receipt_body(body) if isinstance(body, str) else body
        db.execute(
            "UPDATE receipts SET action_kind=?, body=? WHERE id=?",
            (new_kind, new_body, receipt_id),
        )


def _migrate_notifications(db: sqlite3.Connection) -> None:
    items = db.execute(
        "SELECT id, source_ref, dedupe_key, deep_link FROM notification_items "
        "WHERE source_kind='source'"
    ).fetchall()
    for item_id, source_ref, dedupe_key, deep_link in items:
        new_dedupe = (
            "feed:" + dedupe_key[len("source:") :]
            if isinstance(dedupe_key, str) and dedupe_key.startswith("source:")
            else dedupe_key
        )
        new_ref = (
            _rewrite_json_object(source_ref, _REF_KEY_MAP)
            if isinstance(source_ref, str)
            else source_ref
        )
        new_link = (
            _rewrite_json_object(deep_link, _REF_KEY_MAP, rewrite_kind=True)
            if isinstance(deep_link, str)
            else deep_link
        )
        db.execute(
            "UPDATE notification_items "
            "SET source_kind='feed', source_ref=?, dedupe_key=?, deep_link=? "
            "WHERE id=?",
            (new_ref, new_dedupe, new_link, item_id),
        )
    routes = db.execute(
        "SELECT id, source_ref_match_json FROM notification_routes "
        "WHERE source_kind='source'"
    ).fetchall()
    for route_id, ref_match in routes:
        new_match = (
            _rewrite_json_object(ref_match, _REF_KEY_MAP)
            if isinstance(ref_match, str)
            else ref_match
        )
        db.execute(
            "UPDATE notification_routes "
            "SET source_kind='feed', source_ref_match_json=? WHERE id=?",
            (new_match, route_id),
        )


def migrate_feeds(db: sqlite3.Connection) -> None:
    if SCHEMA_DIGEST != FEEDS_TO_DIGEST:
        raise BundleSchemaMismatch(
            "Feeds migration endpoint needs updating for this build"
        )
    execute_ddl(db, _FEEDS_DDL)
    db.execute(
        "INSERT INTO feeds ("
        "id, name, kind, url, config, sheet_id, schedule, enabled, cursor, "
        "last_checked_at, last_status, new_rows_total, created_at"
        ") SELECT "
        "id, name, kind, url, config, sheet_id, schedule, enabled, cursor, "
        "last_checked_at, last_status, new_rows_total, created_at "
        "FROM sources"
    )
    db.execute(
        "INSERT INTO feed_checks ("
        "id, feed_id, op_id, receipt_id, status, new_rows, skipped_rows, "
        "changed_rows, revisions, error, cursor_before, cursor_after, "
        "duration_ms, warning_count, cost_micro, summary_json, started_at, "
        "finished_at"
        ") SELECT "
        "id, source_id, op_id, receipt_id, status, new_rows, skipped_rows, "
        "changed_rows, revisions, error, cursor_before, cursor_after, "
        "duration_ms, warning_count, cost_micro, summary_json, started_at, "
        "finished_at "
        "FROM source_runs"
    )
    db.execute("DROP TABLE source_runs")
    db.execute("DROP TABLE sources")
    _migrate_receipts(db)
    _migrate_notifications(db)
    violation = db.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise BundleSchemaMismatch(
            f"Feeds upgrade found a broken reference: {tuple(violation)}"
        )
