"""Data-preserving upgrade tests for the watches -> alerts schema cutover."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.schema import SCHEMA_DIGEST
from frisket.upgrades.project.alerts_migration import ALERTS_TO_DIGEST
from frisket.upgrades.project.generated_columns_migration import (
    GENERATED_COLUMNS_TO_DIGEST,
)

_PRE_UPGRADE_WATCH_TABLES_SQL = """
CREATE TABLE watches (
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
CREATE UNIQUE INDEX idx_watches_view ON watches(view_id);

CREATE TABLE watch_runs (
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
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
CREATE INDEX idx_watch_runs_notification_pending ON watch_runs(id) WHERE notification_pending=1;
CREATE INDEX idx_watch_runs_watch_started ON watch_runs(watch_id, started_at DESC, id DESC);

CREATE TABLE watch_run_hits (
  run_id INTEGER NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER REFERENCES columns(id) ON DELETE SET NULL,
  rank INTEGER NOT NULL,
  snippet TEXT,
  is_new INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, rank)
) WITHOUT ROWID;
CREATE INDEX idx_watch_run_hits_run_rank ON watch_run_hits(run_id, rank);

CREATE TABLE watch_run_events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
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
CREATE INDEX idx_watch_run_events_run_rank ON watch_run_events(run_id, rank, id);
CREATE INDEX idx_watch_run_events_watch_id ON watch_run_events(watch_id, id);
CREATE INDEX idx_watch_run_events_kind ON watch_run_events(event_kind, run_id, rank);

CREATE TABLE watch_seen_rows (
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  first_seen_run_id INTEGER NOT NULL,
  last_seen_run_id INTEGER NOT NULL,
  PRIMARY KEY (watch_id, sheet_id, row_id)
) WITHOUT ROWID;
CREATE INDEX idx_watch_seen_rows_watch ON watch_seen_rows(watch_id);

CREATE TABLE watch_row_field_state (
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_hash TEXT NOT NULL,
  value_json TEXT,
  last_observed_run_id INTEGER NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
  PRIMARY KEY (watch_id, sheet_id, row_id, column_id)
) WITHOUT ROWID;
CREATE INDEX idx_watch_row_field_state_watch ON watch_row_field_state(watch_id, last_observed_run_id);

CREATE TABLE project_purge_targets (
  kind TEXT NOT NULL CHECK(kind IN ('sheet','column','row','change','run','artifact','watch_run','text_surface','qa_thread')),
  object_id INTEGER NOT NULL,
  purge_id INTEGER NOT NULL REFERENCES project_purges(id),
  PRIMARY KEY(purge_id,kind,object_id)
) WITHOUT ROWID;
CREATE INDEX idx_project_purge_targets_object ON project_purge_targets(kind,object_id);
"""


def test_alerts_migration_preserves_all_tables_purge_targets_notifications_and_slack_config(
    tmp_path,
):
    bundle_path = tmp_path / "legacy-alerts.frisket"
    with closing(Project.create(bundle_path, name="legacy-alerts")) as seed:
        sheet_id = seed.add_sheet("contracts")
        col_id = seed.add_column(sheet_id, "vendor")
        row_ids = seed.add_rows(sheet_id, [{"vendor": "Acme Corp"}], {"vendor": col_id})
        row_id = row_ids[0]
        view_id = seed.add_view(
            "Vendor view",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filters": [],
                }
            },
        )
        second_view_id = seed.add_view(
            "Second view",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filters": [],
                }
            },
        )

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "alert_row_field_state",
            "alert_seen_rows",
            "alert_check_events",
            "alert_check_hits",
            "alert_checks",
            "alerts",
            "project_purge_targets",
        ):
            raw.execute(f"DROP TABLE IF EXISTS {table}")
        raw.executescript(_PRE_UPGRADE_WATCH_TABLES_SQL)

        raw.execute(
            "INSERT INTO watches "
            "(id,name,view_id,detection_policy,enabled,schedule,last_evaluated_op,"
            "last_run_id,baseline_run_id,last_status,created_at,updated_at) "
            "VALUES (5,'Contract watch',?,'{\"kind\":\"new_matches\"}',1,'@hourly',3,12,12,'ok','2026-01-01 00:00:00','2026-01-02 00:00:00')",
            (view_id,),
        )
        raw.execute("UPDATE sqlite_sequence SET seq=9 WHERE name='watches'")
        raw.execute(
            "INSERT INTO watch_runs "
            "(id,watch_id,status,op_cursor_before,op_cursor_after,matched_rows,new_rows,"
            "error,resolved_query_hash,resolved_query,detection_policy,notification_pending,"
            "error_code,started_at,finished_at) "
            "VALUES (12,5,'ok',2,3,1,1,NULL,'hash12','{\"scope\":{\"kind\":\"sheet\"}}',"
            "'{\"kind\":\"new_matches\"}',0,NULL,'2026-01-02 00:00:00','2026-01-02 00:00:05')"
        )
        raw.execute(
            "INSERT INTO watch_run_hits (run_id,sheet_id,row_id,column_id,rank,snippet,is_new) "
            "VALUES (12,?,?,?,1,'Acme Corp',1)",
            (sheet_id, row_id, col_id),
        )
        raw.execute(
            "INSERT INTO watch_run_events "
            "(id,run_id,watch_id,event_kind,subject_kind,subject_ref,before_json,after_json,"
            "delta_json,severity,rank,snippet,created_at) "
            "VALUES (31,12,5,'row_entered','row',?,NULL,'{\"is_new\":true}',NULL,'info',1,'Acme Corp','2026-01-02 00:00:05')",
            (json.dumps({"sheet_id": sheet_id, "row_id": row_id}),),
        )
        raw.execute(
            "INSERT INTO watch_seen_rows (watch_id,sheet_id,row_id,first_seen_run_id,last_seen_run_id) "
            "VALUES (5,?,?,12,12)",
            (sheet_id, row_id),
        )
        raw.execute(
            "INSERT INTO watch_row_field_state "
            "(watch_id,sheet_id,row_id,column_id,value_hash,value_json,last_observed_run_id) "
            'VALUES (5,?,?,?,\'sha256:abc\',\'{"outcome":"completed","value":"Acme Corp"}\',12)',
            (sheet_id, row_id, col_id),
        )

        raw.execute("INSERT INTO project_purges (id,phase) VALUES (1,'complete')")
        raw.execute(
            "INSERT INTO project_purge_targets (kind,object_id,purge_id) VALUES ('watch_run',12,1)"
        )
        raw.execute(
            "INSERT INTO project_purge_targets (kind,object_id,purge_id) VALUES ('sheet',99,1)"
        )

        raw.execute(
            "INSERT INTO notification_items "
            "(id,source_kind,source_ref,dedupe_key,source_event_ids,event_count,event_kinds,"
            "title,summary,severity,deep_link,payload) VALUES "
            "(101,'watch',?,'watch:5:run:12','[31]',1,'[\"row_entered\"]',"
            "'Contract watch recorded 1 alert event','Check #12 matched 1 rows.','info',?,?)",
            (
                json.dumps({"watch_id": 5, "run_id": 12}),
                json.dumps(
                    {
                        "kind": "watch_run",
                        "watch_id": 5,
                        "run_id": 12,
                        "event_ids": [31],
                    }
                ),
                json.dumps(
                    {
                        "watch_name": "Contract watch",
                        "matched_rows": 1,
                        "new_rows": 1,
                    }
                ),
            ),
        )
        raw.execute(
            "INSERT INTO notification_items "
            "(id,source_kind,source_ref,dedupe_key,source_event_ids,event_count,event_kinds,"
            "title,summary,severity,deep_link,payload) VALUES "
            "(102,'run','{\"run_id\":44}','run:44','[]',1,'[]','Run done','Done','info','{\"kind\":\"run\",\"run_id\":44}','{\"run_id\":44}')"
        )

        raw.execute(
            "INSERT INTO notification_channels (id,kind,name,enabled,config_json,secret_ref) VALUES "
            "(201,'slack','Watch Slack channel',1,?,NULL)",
            (
                json.dumps(
                    {
                        "managed": "watch_slack",
                        "watch_id": 5,
                        "workspace_id": "T999",
                        "channel_id": "C12345",
                    }
                ),
            ),
        )
        raw.execute(
            "INSERT INTO notification_channels (id,kind,name,enabled,config_json,secret_ref) VALUES "
            "(202,'slack','Ops Slack',1,?,'env:SLACK_WEBHOOK')",
            (json.dumps({"channel_label": "#ops", "webhook_host": "hooks.slack.com"}),),
        )

        raw.execute(
            "INSERT INTO notification_routes "
            "(id,name,enabled,channel_id,owner_kind,owner_ref,source_kind,source_ref_match_json) "
            "VALUES (301,'Watch Slack channel',1,201,'project','legacy-alerts','watch',?)",
            (json.dumps({"watch_id": 5}),),
        )
        raw.execute(
            "INSERT INTO notification_routes "
            "(id,name,enabled,channel_id,owner_kind,owner_ref,source_kind,source_ref_match_json) "
            "VALUES (302,'Run route',1,202,'project','legacy-alerts','run','{}')"
        )

        raw.execute(
            "UPDATE meta SET value=? WHERE key='schema_digest'",
            (GENERATED_COLUMNS_TO_DIGEST,),
        )
        raw.commit()
    finally:
        raw.close()

    with closing(Project(bundle_path)) as migrated:
        stamped = migrated.db.execute(
            "SELECT value FROM meta WHERE key='schema_digest'"
        ).fetchone()[0]
        assert stamped == SCHEMA_DIGEST == ALERTS_TO_DIGEST
        assert migrated.db.execute("PRAGMA foreign_key_check").fetchall() == []

        tables = {
            row[0]
            for row in migrated.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "alerts",
            "alert_checks",
            "alert_check_hits",
            "alert_check_events",
            "alert_seen_rows",
            "alert_row_field_state",
        } <= tables
        assert not (
            {
                "watches",
                "watch_runs",
                "watch_run_hits",
                "watch_run_events",
                "watch_seen_rows",
                "watch_row_field_state",
            }
            & tables
        )

        alert = migrated.get_alert(5)
        assert alert is not None
        assert alert["name"] == "Contract watch"
        assert alert["view_id"] == view_id
        assert alert["last_run_id"] == 12
        assert alert["baseline_run_id"] == 12

        check = migrated.get_alert_check(12)
        assert check is not None
        assert check["alert_id"] == 5
        assert check["matched_rows"] == 1

        hits = migrated.alert_check_matches(12)
        assert len(hits) == 1
        assert hits[0]["row_id"] == row_id
        assert hits[0]["snippet"] == "Acme Corp"

        events = migrated.alert_check_events(12)
        assert len(events) == 1
        assert events[0]["id"] == 31
        assert events[0]["alert_id"] == 5

        seen = migrated.db.execute(
            "SELECT alert_id,sheet_id,row_id FROM alert_seen_rows"
        ).fetchall()
        assert [(r["alert_id"], r["sheet_id"], r["row_id"]) for r in seen] == [
            (5, sheet_id, row_id)
        ]
        field_state = migrated.db.execute(
            "SELECT alert_id,column_id,value_hash,last_observed_run_id FROM alert_row_field_state"
        ).fetchall()
        assert [
            (r["alert_id"], r["column_id"], r["value_hash"], r["last_observed_run_id"])
            for r in field_state
        ] == [(5, col_id, "sha256:abc", 12)]

        purge_rows = migrated.db.execute(
            "SELECT kind,object_id FROM project_purge_targets ORDER BY kind,object_id"
        ).fetchall()
        assert [(r["kind"], r["object_id"]) for r in purge_rows] == [
            ("alert_check", 12),
            ("sheet", 99),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            migrated.db.execute(
                "INSERT INTO project_purge_targets (kind,object_id,purge_id) VALUES ('watch_run',99,1)"
            )

        notif = migrated.notification_item_by_dedupe_key("alert:5:run:12")
        assert notif is not None
        assert notif["source_kind"] == "alert"
        assert json.loads(notif["source_ref"]) == {"alert_id": 5, "run_id": 12}
        assert json.loads(notif["deep_link"]) == {
            "kind": "alert_check",
            "alert_id": 5,
            "run_id": 12,
            "event_ids": [31],
        }
        payload = json.loads(notif["payload"])
        assert payload["alert_name"] == "Contract watch"
        assert "watch_name" not in payload

        other_notif = migrated.notification_item_by_dedupe_key("run:44")
        assert other_notif is not None
        assert other_notif["source_kind"] == "run"

        summary = migrated.notification_summary(actor_id="local:project")
        assert summary["by_source_kind"]["alert"] == 1
        assert summary["by_source_ref"] == [
            {"source_kind": "alert", "source_ref": {"alert_id": 5}, "unseen": 1}
        ]

        routes = {int(r["id"]): r for r in migrated.notification_routes()}
        assert routes[301]["source_kind"] == "alert"
        assert json.loads(routes[301]["source_ref_match_json"]) == {"alert_id": 5}
        assert routes[302]["source_kind"] == "run"

        channels = {int(c["id"]): c for c in migrated.notification_channels()}
        assert json.loads(channels[201]["config_json"]) == {
            "managed": "alert_slack",
            "alert_id": 5,
            "workspace_id": "T999",
            "channel_id": "C12345",
        }
        assert json.loads(channels[202]["config_json"]) == {
            "channel_label": "#ops",
            "webhook_host": "hooks.slack.com",
        }

        new_alert_id = migrated.add_alert("New alert", view_id=second_view_id)
        assert new_alert_id > 9

    with closing(Project(bundle_path)) as reopened:
        assert reopened.get_alert(5) is not None
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
