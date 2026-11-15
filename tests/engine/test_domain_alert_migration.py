import sqlite3

from frisket.engine.store.domain_alert_migration import migrate_alert_baselines


def test_cutover_keeps_definitions_and_history_and_silently_rebaselines():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE watches(id INTEGER PRIMARY KEY,query TEXT,last_run_id INTEGER);
        CREATE TABLE watch_runs(id INTEGER PRIMARY KEY,watch_id INTEGER,resolved_query_hash TEXT);
        CREATE TABLE watch_seen_rows(watch_id INTEGER,row_id INTEGER);
        CREATE TABLE watch_row_field_state(watch_id INTEGER,row_id INTEGER,value_json TEXT);
        INSERT INTO watches VALUES(1,'saved criteria',9);
        INSERT INTO watch_runs VALUES(9,1,'historic hash');
        INSERT INTO watch_seen_rows VALUES(1,99);
        INSERT INTO watch_row_field_state VALUES(1,99,'old');
    """)
    migrate_alert_baselines(db)
    assert db.execute(
        "SELECT query,last_run_id,baseline_run_id FROM watches"
    ).fetchone() == ("saved criteria", 9, None)
    assert db.execute(
        "SELECT resolved_query_hash,detection_policy,notification_pending FROM watch_runs"
    ).fetchone() == ("historic hash", None, 0)
    assert db.execute("SELECT count(*) FROM watch_seen_rows").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM watch_row_field_state").fetchone()[0] == 0
    assert db.execute("SELECT schedule FROM watches").fetchone()[0] == "@hourly"
    db.execute("UPDATE watches SET baseline_run_id=10, schedule=NULL")
    migrate_alert_baselines(db)
    assert db.execute("SELECT baseline_run_id FROM watches").fetchone()[0] == 10
    assert db.execute("SELECT schedule FROM watches").fetchone()[0] is None
    db.close()
