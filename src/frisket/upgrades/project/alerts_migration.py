"""Data-preserving upgrade from ``watches`` to ``alerts`` storage."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from frisket.engine.store.schema import BundleSchemaMismatch, SCHEMA_DIGEST
from frisket.upgrades.project._step import execute_ddl

ALERTS_FROM_DIGEST = "frisket.schema.v1:6a94206a03cff0c69e0188ff74317800"
ALERTS_TO_DIGEST = "frisket.schema.v1:f19c2358cf272be2717f5299a22ccea8"

_ALERT_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  view_id INTEGER NOT NULL REFERENCES views(id) ON DELETE CASCADE,
  detection_policy TEXT NOT NULL DEFAULT '{"kind":"new_matches"}',
  enabled INTEGER NOT NULL DEFAULT 1,
  schedule TEXT DEFAULT '@hourly',
  last_evaluated_op INTEGER NOT NULL DEFAULT 0,
  last_run_id INTEGER,
  baseline_run_id INTEGER,
  last_status TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS alert_checks (
  id INTEGER PRIMARY KEY,
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'ok',
  op_cursor_before INTEGER NOT NULL,
  op_cursor_after INTEGER NOT NULL,
  matched_rows INTEGER NOT NULL DEFAULT 0,
  new_rows INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  resolved_query_hash TEXT,
  resolved_query TEXT NOT NULL DEFAULT '{}',
  detection_policy TEXT,
  notification_pending INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_check_hits (
  run_id INTEGER NOT NULL REFERENCES alert_checks(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER REFERENCES columns(id) ON DELETE SET NULL,
  rank INTEGER NOT NULL,
  snippet TEXT,
  is_new INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, rank)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alert_check_events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES alert_checks(id) ON DELETE CASCADE,
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  event_kind TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_ref TEXT NOT NULL DEFAULT '{}',
  before_json TEXT,
  after_json TEXT,
  delta_json TEXT,
  severity TEXT NOT NULL DEFAULT 'info',
  rank INTEGER NOT NULL,
  snippet TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS alert_seen_rows (
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  first_seen_run_id INTEGER NOT NULL,
  last_seen_run_id INTEGER NOT NULL,
  PRIMARY KEY (alert_id, sheet_id, row_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alert_row_field_state (
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_hash TEXT NOT NULL,
  value_json TEXT,
  last_observed_run_id INTEGER NOT NULL REFERENCES alert_checks(id) ON DELETE CASCADE,
  PRIMARY KEY (alert_id, sheet_id, row_id, column_id)
) WITHOUT ROWID;
"""

_ALERT_INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_view ON alerts(view_id);
CREATE INDEX IF NOT EXISTS idx_alert_checks_notification_pending ON alert_checks(id) WHERE notification_pending=1;
CREATE INDEX IF NOT EXISTS idx_alert_checks_alert_started
  ON alert_checks(alert_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_alert_check_hits_run_rank
  ON alert_check_hits(run_id, rank);
CREATE INDEX IF NOT EXISTS idx_alert_check_events_run_rank
  ON alert_check_events(run_id, rank, id);
CREATE INDEX IF NOT EXISTS idx_alert_check_events_alert_id
  ON alert_check_events(alert_id, id);
CREATE INDEX IF NOT EXISTS idx_alert_check_events_kind
  ON alert_check_events(event_kind, run_id, rank);
CREATE INDEX IF NOT EXISTS idx_alert_seen_rows_alert
  ON alert_seen_rows(alert_id);
CREATE INDEX IF NOT EXISTS idx_alert_row_field_state_alert
  ON alert_row_field_state(alert_id, last_observed_run_id);
"""

_PURGE_TARGETS_NEW_SQL = """
CREATE TABLE project_purge_targets_alerts_new (
 kind TEXT NOT NULL CHECK(kind IN ('sheet','column','row','change','run','artifact','alert_check','text_surface','qa_thread')),
 object_id INTEGER NOT NULL,
 purge_id INTEGER NOT NULL REFERENCES project_purges(id),
 PRIMARY KEY(purge_id,kind,object_id)
) WITHOUT ROWID
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rebuild_alert_tables(db: sqlite3.Connection, tables: set[str]) -> None:
    if "watches" not in tables:
        return
    high_water = (
        db.execute("SELECT seq FROM sqlite_sequence WHERE name='watches'").fetchone()
        if "sqlite_sequence" in tables
        else None
    )
    execute_ddl(db, _ALERT_TABLES_SQL)
    db.execute(
        "INSERT INTO alerts "
        "(id,name,view_id,detection_policy,enabled,schedule,last_evaluated_op,"
        "last_run_id,baseline_run_id,last_status,created_at,updated_at) "
        "SELECT id,name,view_id,detection_policy,enabled,schedule,last_evaluated_op,"
        "last_run_id,baseline_run_id,last_status,created_at,updated_at FROM watches"
    )
    db.execute(
        "INSERT INTO alert_checks "
        "(id,alert_id,status,op_cursor_before,op_cursor_after,matched_rows,new_rows,"
        "error,resolved_query_hash,resolved_query,detection_policy,"
        "notification_pending,error_code,started_at,finished_at) "
        "SELECT id,watch_id,status,op_cursor_before,op_cursor_after,matched_rows,"
        "new_rows,error,resolved_query_hash,resolved_query,detection_policy,"
        "notification_pending,error_code,started_at,finished_at FROM watch_runs"
    )
    db.execute(
        "INSERT INTO alert_check_hits "
        "(run_id,sheet_id,row_id,column_id,rank,snippet,is_new) "
        "SELECT run_id,sheet_id,row_id,column_id,rank,snippet,is_new "
        "FROM watch_run_hits"
    )
    db.execute(
        "INSERT INTO alert_check_events "
        "(id,run_id,alert_id,event_kind,subject_kind,subject_ref,before_json,"
        "after_json,delta_json,severity,rank,snippet,created_at) "
        "SELECT id,run_id,watch_id,event_kind,subject_kind,subject_ref,before_json,"
        "after_json,delta_json,severity,rank,snippet,created_at "
        "FROM watch_run_events"
    )
    db.execute(
        "INSERT INTO alert_seen_rows "
        "(alert_id,sheet_id,row_id,first_seen_run_id,last_seen_run_id) "
        "SELECT watch_id,sheet_id,row_id,first_seen_run_id,last_seen_run_id "
        "FROM watch_seen_rows"
    )
    db.execute(
        "INSERT INTO alert_row_field_state "
        "(alert_id,sheet_id,row_id,column_id,value_hash,value_json,last_observed_run_id) "
        "SELECT watch_id,sheet_id,row_id,column_id,value_hash,value_json,"
        "last_observed_run_id FROM watch_row_field_state"
    )
    for old_table in (
        "watch_row_field_state",
        "watch_seen_rows",
        "watch_run_events",
        "watch_run_hits",
        "watch_runs",
        "watches",
    ):
        if old_table in tables:
            db.execute(f"DROP TABLE {old_table}")
    execute_ddl(db, _ALERT_INDEXES_SQL)
    if high_water is not None:
        cur = db.execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='alerts'",
            (int(high_water[0]),),
        )
        if cur.rowcount == 0:
            db.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES ('alerts',?)",
                (int(high_water[0]),),
            )


def _rebuild_purge_targets(db: sqlite3.Connection, tables: set[str]) -> None:
    if "project_purge_targets" not in tables:
        return
    db.execute(_PURGE_TARGETS_NEW_SQL)
    db.execute(
        "INSERT INTO project_purge_targets_alerts_new (kind,object_id,purge_id) "
        "SELECT CASE WHEN kind='watch_run' THEN 'alert_check' ELSE kind END,"
        "object_id,purge_id FROM project_purge_targets"
    )
    db.execute("DROP TABLE project_purge_targets")
    db.execute(
        "ALTER TABLE project_purge_targets_alerts_new RENAME TO project_purge_targets"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_purge_targets_object "
        "ON project_purge_targets(kind,object_id)"
    )


def _migrate_notification_items(db: sqlite3.Connection, tables: set[str]) -> None:
    if "notification_items" not in tables:
        return
    rows = db.execute(
        "SELECT id,source_kind,source_ref,dedupe_key,deep_link,payload "
        "FROM notification_items "
        "WHERE source_kind='watch' OR dedupe_key LIKE 'watch:%'"
    ).fetchall()
    for (
        item_id,
        source_kind,
        raw_source_ref,
        dedupe_key,
        raw_deep_link,
        raw_payload,
    ) in rows:
        new_source_kind = "alert" if source_kind == "watch" else source_kind
        new_dedupe_key = (
            f"alert:{dedupe_key[len('watch:') :]}"
            if isinstance(dedupe_key, str) and dedupe_key.startswith("watch:")
            else dedupe_key
        )
        source_ref = _parse_json_object(raw_source_ref)
        if source_ref is not None and "watch_id" in source_ref:
            legacy_id = source_ref.pop("watch_id")
            source_ref.setdefault("alert_id", legacy_id)
            new_source_ref = _canonical_json(source_ref)
        else:
            new_source_ref = raw_source_ref
        deep_link = _parse_json_object(raw_deep_link)
        if deep_link is not None:
            changed = False
            if deep_link.get("kind") == "watch_run":
                deep_link["kind"] = "alert_check"
                changed = True
            if "watch_id" in deep_link:
                legacy_id = deep_link.pop("watch_id")
                deep_link.setdefault("alert_id", legacy_id)
                changed = True
            new_deep_link = _canonical_json(deep_link) if changed else raw_deep_link
        else:
            new_deep_link = raw_deep_link
        payload = _parse_json_object(raw_payload)
        if payload is not None and "watch_name" in payload:
            legacy_name = payload.pop("watch_name")
            payload.setdefault("alert_name", legacy_name)
            new_payload = _canonical_json(payload)
        else:
            new_payload = raw_payload
        db.execute(
            "UPDATE notification_items "
            "SET source_kind=?,source_ref=?,dedupe_key=?,deep_link=?,payload=? "
            "WHERE id=?",
            (
                new_source_kind,
                new_source_ref,
                new_dedupe_key,
                new_deep_link,
                new_payload,
                item_id,
            ),
        )


def _migrate_notification_routes(db: sqlite3.Connection, tables: set[str]) -> None:
    if "notification_routes" not in tables:
        return
    rows = db.execute(
        "SELECT id,source_kind,source_ref_match_json FROM notification_routes "
        "WHERE source_kind='watch' OR source_ref_match_json LIKE '%watch_id%'"
    ).fetchall()
    for route_id, source_kind, raw_match in rows:
        new_source_kind = "alert" if source_kind == "watch" else source_kind
        match = _parse_json_object(raw_match)
        if match is not None and "watch_id" in match:
            legacy_id = match.pop("watch_id")
            match.setdefault("alert_id", legacy_id)
            new_match = _canonical_json(match)
        else:
            new_match = raw_match
        db.execute(
            "UPDATE notification_routes SET source_kind=?,source_ref_match_json=? "
            "WHERE id=?",
            (new_source_kind, new_match, route_id),
        )


def _migrate_cloud_slack_channels(db: sqlite3.Connection, tables: set[str]) -> None:
    if "notification_channels" not in tables:
        return
    rows = db.execute(
        "SELECT id,config_json FROM notification_channels "
        "WHERE config_json LIKE '%watch_slack%'"
    ).fetchall()
    for channel_id, raw_config in rows:
        config = _parse_json_object(raw_config)
        if config is None or config.get("managed") != "watch_slack":
            continue
        config["managed"] = "alert_slack"
        if "watch_id" in config:
            legacy_id = config.pop("watch_id")
            config.setdefault("alert_id", legacy_id)
        db.execute(
            "UPDATE notification_channels SET config_json=? WHERE id=?",
            (_canonical_json(config), channel_id),
        )


def migrate_alerts(db: sqlite3.Connection) -> None:
    """Atomically cut over alert tables, purge targets, notifications, and Slack configs."""
    if SCHEMA_DIGEST != ALERTS_TO_DIGEST:
        raise BundleSchemaMismatch(
            "Alerts migration endpoint needs updating for this build"
        )
    tables = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    _rebuild_alert_tables(db, tables)
    _rebuild_purge_targets(db, tables)
    _migrate_notification_items(db, tables)
    _migrate_notification_routes(db, tables)
    _migrate_cloud_slack_channels(db, tables)
    violation = db.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise BundleSchemaMismatch(
            f"Alerts upgrade found a broken reference: {tuple(violation)}"
        )
