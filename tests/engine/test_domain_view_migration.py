import json

from frisket.upgrades.project.domain_view_migration import migrate_unified_views
from frisket.engine.views.evaluate import open_selection


def test_all_old_definitions_and_alert_identity_survive_one_time_cutover(make_project):
    project = make_project("old-views")
    sheet = project.add_sheet("records")
    col = project.add_column(sheet, "status")
    project.add_rows(sheet, [{"status": "open"}], {"status": col})
    db = project.db
    db.execute("DROP TABLE alert_row_field_state")
    db.execute("DROP TABLE alert_seen_rows")
    db.execute("DROP TABLE alert_check_events")
    db.execute("DROP TABLE alert_check_hits")
    db.execute("DROP TABLE alert_checks")
    db.execute("DROP TABLE alerts")
    db.execute("DROP TABLE views")
    db.executescript("""
      CREATE TABLE views(id INTEGER PRIMARY KEY,sheet_id INTEGER NOT NULL REFERENCES sheets(id),name TEXT,spec TEXT,op_id INTEGER,created_at TEXT DEFAULT 'old',updated_at TEXT DEFAULT 'old');
      CREATE INDEX idx_views_sheet ON views(sheet_id);
      CREATE TABLE lenses(id INTEGER PRIMARY KEY,sheet_id INTEGER REFERENCES sheets(id),name TEXT,spec TEXT,op_id INTEGER,created_at TEXT DEFAULT 'old',updated_at TEXT DEFAULT 'old');
      CREATE TABLE watches(id INTEGER PRIMARY KEY,name TEXT,scope TEXT,sheet_id INTEGER REFERENCES sheets(id),query TEXT NOT NULL,query_version TEXT,query_hash TEXT,detection_policy TEXT,enabled INTEGER DEFAULT 1,last_evaluated_op INTEGER DEFAULT 0,last_run_id INTEGER,baseline_run_id INTEGER,last_status TEXT,created_at TEXT DEFAULT 'old',updated_at TEXT DEFAULT 'old');
      CREATE INDEX idx_watches_sheet ON watches(sheet_id);
      CREATE TABLE watch_runs(id INTEGER PRIMARY KEY,watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'ok',op_cursor_before INTEGER NOT NULL,op_cursor_after INTEGER NOT NULL,matched_rows INTEGER NOT NULL DEFAULT 0,new_rows INTEGER NOT NULL DEFAULT 0);
    """)
    spec = {
        "filter": {"status": {"eq": "open"}},
        "sort": [{"column": "status", "dir": "desc"}],
        "columns": ["status", "deleted_layout_column"],
        "column_groups": [{"run_id": 44, "label": "Facts", "columns": ["status"]}],
    }
    db.execute(
        "INSERT INTO views(id,sheet_id,name,spec) VALUES(7,?,?,?)",
        (sheet, "Normal", json.dumps(spec)),
    )
    query = {
        "kind": "filter",
        "sheet_id": sheet,
        "filter": {"status": {"eq": "open"}},
    }
    db.execute(
        "INSERT INTO lenses(id,sheet_id,name,spec) VALUES(7,?,?,?)",
        (sheet, "Lens", json.dumps({"query": query})),
    )
    db.execute(
        "INSERT INTO lenses(id,sheet_id,name,spec) VALUES(8,?,?,?)",
        (sheet, "Broken", json.dumps({"query": {"kind": "missing_mode"}})),
    )
    db.execute(
        "INSERT INTO watches(id,name,scope,sheet_id,query,detection_policy,last_run_id,baseline_run_id) VALUES(9,'Alert','sheet',?,?,?,22,22)",
        (sheet, json.dumps(query), '{"kind":"new_matches"}'),
    )
    db.execute(
        "INSERT INTO watch_runs(id,watch_id,status,op_cursor_before,op_cursor_after,matched_rows,new_rows) VALUES(22,9,'ok',0,0,1,1)"
    )
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    migrate_unified_views(db)
    db.commit()
    assert project.get_view(7)["name"] == "Normal"
    assert len(project.views()) == 4
    watch = db.execute("SELECT * FROM watches WHERE id=9").fetchone()
    assert watch["last_run_id"] == 22
    assert watch["baseline_run_id"] is None
    assert db.execute("SELECT 1 FROM watch_runs WHERE id=22").fetchone() is not None
    assert not {"query", "scope", "sheet_id"} & {
        row[1] for row in db.execute("PRAGMA table_info(watches)")
    }
    assert (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='lenses'").fetchone() is None
    )
    broken = next(row for row in project.views() if row["name"] == "Broken")
    assert json.loads(broken["spec"])["unavailable_reason"]
    saved = json.loads(project.get_view(7)["spec"])
    assert saved["selection"]["filters"][0]["column_id"] == col
    assert saved["presentation"] == {
        "columns": [col],
        "column_groups": [{"run_id": 44, "label": "Facts", "columns": [col]}],
    }
    with open_selection(project, saved["selection"]) as selected:
        assert selected.total == 1
    migrate_unified_views(db)
    assert len(project.views()) == 4
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
