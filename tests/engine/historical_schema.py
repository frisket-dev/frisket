"""Build historical test bundles from frozen DDL, independently of current schema.

Fixtures may seed ordinary data through the current facade, but must then use
real old table definitions before testing an upgrade. This is a test-only
conversion for representable fixture data, not a downgrade supported by Frisket.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import sqlite3


PRE_DOMAIN_SCHEMA = gzip.decompress(
    (Path(__file__).parents[1] / "fixtures/domain-v0-schema.sql.gz").read_bytes()
).decode()
PRE_DOMAIN_DIGEST = "frisket.schema.v1:7e339a8995763aa40346e7d36187837d"

_PRE_FEEDS_DDL = """
CREATE TABLE IF NOT EXISTS sources (
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
CREATE INDEX IF NOT EXISTS idx_sources_sheet ON sources(sheet_id);

CREATE TABLE IF NOT EXISTS source_runs (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS idx_source_runs_source_started
  ON source_runs(source_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_source_runs_receipt ON source_runs(receipt_id);
"""


def restore_pre_feeds_tables(db: sqlite3.Connection) -> None:
    """Replace current feeds/feed_checks tables with pre-cutover sources/source_runs."""
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if "sources" in tables and "source_runs" in tables:
        return
    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.executescript(_PRE_FEEDS_DDL)
        if "feeds" in tables:
            db.execute(
                "INSERT INTO sources ("
                "id, name, kind, url, config, sheet_id, schedule, enabled, cursor, "
                "last_checked_at, last_status, new_rows_total, created_at"
                ") SELECT "
                "id, name, kind, url, config, sheet_id, schedule, enabled, cursor, "
                "last_checked_at, last_status, new_rows_total, created_at "
                "FROM feeds"
            )
        if "feed_checks" in tables:
            db.execute(
                "INSERT INTO source_runs ("
                "id, source_id, op_id, receipt_id, status, new_rows, skipped_rows, "
                "changed_rows, revisions, error, cursor_before, cursor_after, "
                "duration_ms, warning_count, cost_micro, summary_json, started_at, "
                "finished_at"
                ") SELECT "
                "id, feed_id, op_id, receipt_id, status, new_rows, skipped_rows, "
                "changed_rows, revisions, error, cursor_before, cursor_after, "
                "duration_ms, warning_count, cost_micro, summary_json, started_at, "
                "finished_at "
                "FROM feed_checks"
            )
        db.execute("DROP TABLE IF EXISTS feed_checks")
        db.execute("DROP TABLE IF EXISTS feeds")
        db.commit()
    finally:
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def restore_pre_domain_schema(db: sqlite3.Connection) -> None:
    """Preserve fixture data while replacing every object with the frozen schema.

    New-only tables/columns cannot leak into tests of prior migrations. Old
    constraints still apply: a fixture using unrepresentable new features fails
    here rather than being silently coerced into fake historical data.
    """
    if "change_order" not in {row[1] for row in db.execute("PRAGMA table_info(ops)")}:
        return
    from frisket.engine.store.current_cells import register_current_cell_functions

    register_current_cell_functions(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    old = sqlite3.connect(":memory:")
    try:
        old.executescript(PRE_DOMAIN_SCHEMA.replace("PRAGMA foreign_keys=ON;", ""))
        tables = [
            row[0]
            for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        saved = {}
        current_table_renames = {
            "output_column_claims": "generated_column_claims",
            "sources": "feeds",
            "source_runs": "feed_checks",
        }
        current_column_renames = {
            "columns": {"ai_generated": "generated"},
            "evaluation_judgments": {"output_column_id": "generated_column_id"},
            "saved_compare_variants": {"output_columns_json": "generated_columns_json"},
            "source_runs": {"source_id": "feed_id"},
        }
        for table in tables:
            source_table = current_table_renames.get(table, table)
            columns = [
                row[1] for row in old.execute(f"PRAGMA table_info({_quoted(table)})")
            ]
            available = {
                row[1]
                for row in db.execute(f"PRAGMA table_info({_quoted(source_table)})")
            }
            col_map = current_column_renames.get(table, {})
            pairs = [
                (name, col_map.get(name, name))
                for name in columns
                if col_map.get(name, name) in available
            ]
            if not pairs:
                continue
            common = [old_name for old_name, _ in pairs]
            select_names = ",".join(_quoted(src_name) for _, src_name in pairs)
            records = [
                list(row)
                for row in db.execute(
                    f"SELECT {select_names} FROM {_quoted(source_table)}"
                )
            ]
            if table == "expected_answer_links" and "fields_json" in common:
                fields_idx = common.index("fields_json")
                for record in records:
                    parsed = json.loads(record[fields_idx])
                    if isinstance(parsed, list):
                        record[fields_idx] = json.dumps(
                            [
                                {
                                    (
                                        "output_column_id"
                                        if k == "generated_column_id"
                                        else k
                                    ): v
                                    for k, v in item.items()
                                }
                                if isinstance(item, dict)
                                else item
                                for item in parsed
                            ]
                        )
            if table == "current_cells":
                # The prior layer represented source ownership through producer_id,
                # not origin_op_id; values and producer identities stay untouched.
                for record in records:
                    if record[common.index("origin_kind")] == "source_cell":
                        record[common.index("origin_op_id")] = None
            saved[table] = (common, records)
        # Preserve logical run heads, including those underneath manual edits.
        names = ["column_id", "row_id", "run_id"]
        heads = {
            tuple(row)
            for row in db.execute(
                "SELECT column_id,row_id,run_id FROM generated_result_candidates"
            )
        }
        heads.update(
            tuple(row)
            for row in db.execute(
                "SELECT column_id,row_id,run_id FROM cell_result_heads"
            )
        )
        saved["cell_result_heads"] = (names, sorted(heads))
    finally:
        old.close()

    for kind in ("view", "trigger", "table"):
        objects = db.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        ).fetchall()
        for (name,) in objects:
            db.execute(f"DROP {kind.upper()} {_quoted(name)}")
    db.executescript(PRE_DOMAIN_SCHEMA.replace("PRAGMA foreign_keys=ON;", ""))
    triggers = db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    for name, _ in triggers:
        db.execute(f"DROP TRIGGER {_quoted(name)}")
    for table, (columns, records) in saved.items():
        db.execute(f"DELETE FROM {_quoted(table)}")
        if records:
            names = ",".join(map(_quoted, columns))
            db.executemany(
                f"INSERT INTO {_quoted(table)} ({names}) VALUES ({','.join('?' for _ in columns)})",
                records,
            )
    for _, statement in triggers:
        db.execute(statement)
    db.execute(
        "DELETE FROM meta WHERE key IN ('change_sequence', 'domain_history_floor')"
    )
    db.execute(
        "UPDATE meta SET value=? WHERE key='schema_digest'", (PRE_DOMAIN_DIGEST,)
    )
    db.commit()
