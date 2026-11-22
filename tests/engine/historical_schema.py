"""Build historical test bundles from frozen DDL, independently of current schema.

Fixtures may seed ordinary data through the current facade, but must then use
real old table definitions before testing an upgrade. This is a test-only
conversion for representable fixture data, not a downgrade supported by Frisket.
"""

from __future__ import annotations

from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from frisket.engine.store.consumed_sources import capture_fingerprint
from frisket.engine.store.current_cells import register_current_cell_functions
from frisket.upgrades.project._step import run_rebuild_step, run_step
from frisket.upgrades.project.alerts_migration import (
    ALERTS_FROM_DIGEST,
    ALERTS_TO_DIGEST,
    migrate_alerts,
)
from frisket.upgrades.project.citations_migration import (
    CITATIONS_TO_DIGEST,
    migrate_citations,
)
from frisket.upgrades.project.consent_estimate_migration import (
    CONSENT_ESTIMATE_TO_DIGEST,
)
from frisket.upgrades.project.domain_migration import (
    DOMAIN_TO_DIGEST,
    migrate_domain,
)
from frisket.upgrades.project.edit_precedence_migration import (
    COPY_PRECEDENCE_TO_DIGEST,
    EDIT_PRECEDENCE_TO_DIGEST,
    apply_edit_precedence,
)
from frisket.upgrades.project.feeds_migration import (
    FEEDS_FROM_DIGEST,
    FEEDS_TO_DIGEST,
    migrate_feeds,
)
from frisket.upgrades.project.generated_columns_migration import (
    GENERATED_COLUMNS_FROM_DIGEST,
    GENERATED_COLUMNS_TO_DIGEST,
    apply_generated_columns_migration,
)
from frisket.upgrades.project.receipt_records_migration import (
    RECEIPT_RECORDS_TO_DIGEST,
    apply_receipt_records,
)
from frisket.upgrades.project.research_pause_migration import (
    RESEARCH_PAUSE_TO_DIGEST,
    apply_research_pause,
)


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

_PRE_CHANGES_TABLE_RENAMES: dict[str, str] = {
    "ops": "changes",
}

_PRE_CHANGES_COLUMN_RENAMES: dict[str, dict[str, str]] = {
    "sheets": {
        "parent_op_id": "parent_change_id",
        "last_verified_op_cursor": "last_verified_change_cursor",
    },
    "base_cell_producers": {"op_id": "change_id"},
    "runs": {"op_id": "change_id"},
    "import_sessions": {"op_id": "change_id"},
    "generated_column_claims": {"op_id": "change_id"},
    "prepared_page_versions": {"producing_op_id": "producing_change_id"},
    "prepared_content_sets": {"producing_op_id": "producing_change_id"},
    "edits": {"op_id": "change_id"},
    "current_cells": {"origin_op_id": "origin_change_id"},
    "citations": {"op_id": "change_id"},
    "feed_checks": {"op_id": "change_id"},
    "views": {"op_id": "change_id"},
    "alerts": {"last_evaluated_op": "last_evaluated_change"},
    "alert_checks": {
        "op_cursor_before": "change_cursor_before",
        "op_cursor_after": "change_cursor_after",
    },
    "cell_baseline": {"origin_op_id": "origin_change_id"},
    "materialized_row_sources": {"op_id": "change_id"},
    "document_text_manual_pins": {"edit_op_id": "edit_change_id"},
    "copied_result_outcomes": {"op_id": "change_id"},
    "consumed_source_bundles": {"owner_op_id": "owner_change_id"},
    "column_descriptor_changes": {"op_id": "change_id"},
    "structural_changes": {"op_id": "change_id"},
    "result_judgment_changes": {"op_id": "change_id"},
}

_REVERSE_CHANGE_KEY_MAP: dict[str, str] = {
    "change_id": "op_id",
    "change_ids": "op_ids",
    "parent_change_id": "parent_op_id",
    "copied_change_id": "copied_op_id",
    "source_change_ids": "source_op_ids",
    "expected_change_id": "expected_op_id",
    "actual_change_id": "actual_op_id",
    "import_change_id": "import_op_id",
    "source_change_cursor": "source_op_cursor",
}

_REVERSE_ACTION_KIND_MAP: dict[str, str] = {
    "change.undo": "operation.undo",
    "change.redo": "operation.redo",
    "change.revert": "operation.revert",
    "change.restore": "operation.restore",
}


def _rewrite_keys_recursive(value: Any, key_map: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_rewrite_keys_recursive(item, key_map) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key_map.get(key, key): _rewrite_keys_recursive(item, key_map)
        for key, item in value.items()
    }


def _downgrade_json_text(
    raw: Any, key_map: dict[str, str], *, canonical: bool = False
) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    rewritten = _rewrite_keys_recursive(parsed, key_map)
    if canonical:
        return json.dumps(
            rewritten, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    return json.dumps(rewritten)


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
            fc_cols = {row[1] for row in db.execute("PRAGMA table_info(feed_checks)")}
            op_col = "op_id" if "op_id" in fc_cols else "change_id"
            db.execute(
                "INSERT INTO source_runs ("
                "id, source_id, op_id, receipt_id, status, new_rows, skipped_rows, "
                "changed_rows, revisions, error, cursor_before, cursor_after, "
                "duration_ms, warning_count, cost_micro, summary_json, started_at, "
                "finished_at"
                ") SELECT "
                f"id, feed_id, {op_col}, receipt_id, status, new_rows, skipped_rows, "
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


@lru_cache(maxsize=1)
def _pre_changes_ddl_objects() -> tuple[tuple[str, str, str], ...]:
    """Build the exact FEEDS_TO_DIGEST DDL objects once per test session."""
    mem = sqlite3.connect(":memory:")
    try:
        register_current_cell_functions(mem)
        mem.executescript(PRE_DOMAIN_SCHEMA.replace("PRAGMA foreign_keys=ON;", ""))
        mem.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_digest',?)",
            (PRE_DOMAIN_DIGEST,),
        )
        mem.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('op_cursor','0')")
        mem.commit()
        migrate_domain(mem)
        run_step(
            mem, DOMAIN_TO_DIGEST, EDIT_PRECEDENCE_TO_DIGEST, apply_edit_precedence
        )
        run_step(
            mem,
            EDIT_PRECEDENCE_TO_DIGEST,
            COPY_PRECEDENCE_TO_DIGEST,
            apply_edit_precedence,
        )
        run_step(
            mem,
            COPY_PRECEDENCE_TO_DIGEST,
            CONSENT_ESTIMATE_TO_DIGEST,
            ("ALTER TABLE consents RENAME COLUMN quote_json TO estimate_json",),
        )
        run_step(
            mem,
            CONSENT_ESTIMATE_TO_DIGEST,
            RESEARCH_PAUSE_TO_DIGEST,
            apply_research_pause,
        )
        run_rebuild_step(
            mem, RESEARCH_PAUSE_TO_DIGEST, CITATIONS_TO_DIGEST, migrate_citations
        )
        run_step(
            mem, CITATIONS_TO_DIGEST, RECEIPT_RECORDS_TO_DIGEST, apply_receipt_records
        )
        run_step(
            mem,
            GENERATED_COLUMNS_FROM_DIGEST,
            GENERATED_COLUMNS_TO_DIGEST,
            apply_generated_columns_migration,
        )
        run_rebuild_step(mem, ALERTS_FROM_DIGEST, ALERTS_TO_DIGEST, migrate_alerts)
        run_rebuild_step(mem, FEEDS_FROM_DIGEST, FEEDS_TO_DIGEST, migrate_feeds)
        order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
        rows = [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in mem.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        ]
        rows.sort(key=lambda item: (order.get(item[0], 9), item[1]))
        return tuple(rows)
    finally:
        mem.close()


def restore_pre_changes_schema(db: sqlite3.Connection) -> None:
    """Replace current changes schema with the exact FEEDS_TO_DIGEST pre-changes schema."""
    tables_now = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if "ops" in tables_now and "changes" not in tables_now:
        return

    register_current_cell_functions(db)
    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        ddl_objects = _pre_changes_ddl_objects()
        old = sqlite3.connect(":memory:")
        try:
            for typ, _, sql in ddl_objects:
                if typ == "table":
                    old.execute(sql)
            old_tables = [name for typ, name, _ in ddl_objects if typ == "table"]
            saved: dict[str, tuple[list[str], list[list[Any]]]] = {}
            for table in old_tables:
                src_table = _PRE_CHANGES_TABLE_RENAMES.get(table, table)
                if src_table not in tables_now:
                    continue
                target_cols = [
                    str(r[1])
                    for r in old.execute(f"PRAGMA table_info({_quoted(table)})")
                ]
                avail_cols = {
                    str(r[1])
                    for r in db.execute(f"PRAGMA table_info({_quoted(src_table)})")
                }
                col_map = _PRE_CHANGES_COLUMN_RENAMES.get(table, {})
                pairs = [
                    (old_col, col_map.get(old_col, old_col))
                    for old_col in target_cols
                    if col_map.get(old_col, old_col) in avail_cols
                ]
                if not pairs:
                    continue
                common = [old_col for old_col, _ in pairs]
                select_list = ",".join(_quoted(src_col) for _, src_col in pairs)
                records = [
                    list(row)
                    for row in db.execute(
                        f"SELECT {select_list} FROM {_quoted(src_table)}"
                    )
                ]
                if table == "meta":
                    k_idx = common.index("key")
                    for rec in records:
                        if rec[k_idx] == "change_cursor":
                            rec[k_idx] = "op_cursor"
                elif table == "receipts":
                    ak_idx = common.index("action_kind")
                    b_idx = common.index("body")
                    for rec in records:
                        rec[ak_idx] = _REVERSE_ACTION_KIND_MAP.get(
                            rec[ak_idx], rec[ak_idx]
                        )
                        if isinstance(rec[b_idx], str):
                            try:
                                body = json.loads(rec[b_idx])
                            except ValueError:
                                continue
                            if isinstance(body, dict):
                                if "action_kind" in body:
                                    body["action_kind"] = _REVERSE_ACTION_KIND_MAP.get(
                                        body["action_kind"], body["action_kind"]
                                    )
                                body = _rewrite_keys_recursive(
                                    body, _REVERSE_CHANGE_KEY_MAP
                                )
                                rec[b_idx] = json.dumps(body)
                elif table == "citations":
                    idx = common.index("subject_ref_json")
                    for rec in records:
                        rec[idx] = _downgrade_json_text(
                            rec[idx], {"change_id": "op_id"}, canonical=True
                        )
                elif table == "text_surfaces":
                    idx = common.index("value_ref_json")
                    for rec in records:
                        rec[idx] = _downgrade_json_text(
                            rec[idx], {"change_id": "op_id"}, canonical=True
                        )
                elif table == "consumed_source_bundles":
                    ck_idx = common.index("capture_key")
                    fp_idx = common.index("input_fingerprint")
                    cj_idx = common.index("capture_json")
                    for rec in records:
                        if isinstance(rec[cj_idx], str):
                            parsed = json.loads(rec[cj_idx])
                            rewritten = _rewrite_keys_recursive(
                                parsed, {"change_id": "op_id"}
                            )
                            new_fp = capture_fingerprint(rewritten)
                            rec[cj_idx] = json.dumps(
                                rewritten,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            )
                            rec[fp_idx] = new_fp
                            if (
                                isinstance(rec[ck_idx], str)
                                and ":shared:" in rec[ck_idx]
                            ):
                                rec[ck_idx] = (
                                    rec[ck_idx].split(":shared:", 1)[0]
                                    + ":shared:"
                                    + new_fp
                                )
                elif table == "ops":
                    s_idx = common.index("spec")
                    for rec in records:
                        rec[s_idx] = _downgrade_json_text(
                            rec[s_idx], _REVERSE_CHANGE_KEY_MAP
                        )
                elif table == "runs":
                    p_idx = common.index("params")
                    for rec in records:
                        rec[p_idx] = _downgrade_json_text(
                            rec[p_idx], _REVERSE_CHANGE_KEY_MAP
                        )
                elif table == "action_submissions":
                    a_idx = common.index("action_id")
                    h_idx = common.index("draft_hash")
                    d_idx = common.index("draft_json")
                    for rec in records:
                        rec[a_idx] = _REVERSE_ACTION_KIND_MAP.get(
                            rec[a_idx], rec[a_idx]
                        )
                        if isinstance(rec[d_idx], str):
                            parsed = json.loads(rec[d_idx])
                            if isinstance(parsed, dict):
                                if parsed.get("action_id") in _REVERSE_ACTION_KIND_MAP:
                                    parsed["action_id"] = _REVERSE_ACTION_KIND_MAP[
                                        parsed["action_id"]
                                    ]
                                if isinstance(parsed.get("params"), dict):
                                    parsed["params"] = _rewrite_keys_recursive(
                                        parsed["params"], _REVERSE_CHANGE_KEY_MAP
                                    )
                                encoded = json.dumps(
                                    parsed,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                )
                                rec[d_idx] = encoded
                                rec[h_idx] = hashlib.sha256(
                                    encoded.encode()
                                ).hexdigest()
                elif table == "effect_checkpoints":
                    idx = common.index("payload")
                    for rec in records:
                        rec[idx] = _downgrade_json_text(
                            rec[idx], {"change_id": "op_id"}
                        )
                elif table == "feed_checks":
                    idx = common.index("summary_json")
                    for rec in records:
                        rec[idx] = _downgrade_json_text(
                            rec[idx], {"change_id": "op_id"}
                        )
                elif table == "project_qa_citations":
                    idx = common.index("locator_json")
                    for rec in records:
                        rec[idx] = _downgrade_json_text(
                            rec[idx],
                            {
                                "source_change_cursor": "source_op_cursor",
                                "change_id": "op_id",
                                "change_ids": "op_ids",
                            },
                            canonical=True,
                        )
                elif table == "project_qa_events":
                    idx = common.index("payload_json")
                    for rec in records:
                        rec[idx] = _downgrade_json_text(
                            rec[idx],
                            {
                                "source_change_cursor": "source_op_cursor",
                                "change_id": "op_id",
                                "change_ids": "op_ids",
                            },
                            canonical=True,
                        )
                saved[table] = (common, records)

            seq_rows = []
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
            ).fetchone():
                for name, seq in db.execute("SELECT name, seq FROM sqlite_sequence"):
                    mapped = "ops" if name == "changes" else name
                    seq_rows.append((mapped, seq))
        finally:
            old.close()

        for kind in ("view", "trigger", "table"):
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
                (kind,),
            ).fetchall():
                db.execute(f"DROP {kind.upper()} {_quoted(name)}")
        for typ, _, sql in ddl_objects:
            if typ in {"table", "index"}:
                db.execute(sql)
        for table, (columns, records) in saved.items():
            db.execute(f"DELETE FROM {_quoted(table)}")
            if records:
                names = ",".join(map(_quoted, columns))
                db.executemany(
                    f"INSERT INTO {_quoted(table)} ({names}) VALUES ({','.join('?' for _ in columns)})",
                    records,
                )
        for typ, _, sql in ddl_objects:
            if typ in {"view", "trigger"}:
                db.execute(sql)
        for name, seq in seq_rows:
            updated = db.execute(
                "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (seq, name)
            ).rowcount
            if not updated:
                db.execute(
                    "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (name, seq)
                )
        db.execute(
            "UPDATE meta SET value=? WHERE key='schema_digest'", (FEEDS_TO_DIGEST,)
        )
        db.commit()
    finally:
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")


def restore_pre_domain_schema(db: sqlite3.Connection) -> None:
    """Preserve fixture data while replacing every object with the frozen schema.

    New-only tables/columns cannot leak into tests of prior migrations. Old
    constraints still apply: a fixture using unrepresentable new features fails
    here rather than being silently coerced into fake historical data.
    """
    change_table = (
        "changes"
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='changes'"
        ).fetchone()
        else "ops"
    )
    if "change_order" not in {
        row[1] for row in db.execute(f"PRAGMA table_info({change_table})")
    }:
        return

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
            "ops": "changes",
            "output_column_claims": "generated_column_claims",
            "sources": "feeds",
            "source_runs": "feed_checks",
            "watches": "alerts",
            "watch_runs": "alert_checks",
            "watch_run_hits": "alert_check_hits",
            "watch_run_events": "alert_check_events",
            "watch_seen_rows": "alert_seen_rows",
            "watch_row_field_state": "alert_row_field_state",
        }
        current_column_renames: dict[str, dict[str, str]] = {
            "columns": {"ai_generated": "generated"},
            "evaluation_judgments": {"output_column_id": "generated_column_id"},
            "saved_compare_variants": {"output_columns_json": "generated_columns_json"},
            "source_runs": {"source_id": "feed_id", "op_id": "change_id"},
            "sheets": {
                "parent_op_id": "parent_change_id",
                "last_verified_op_cursor": "last_verified_change_cursor",
            },
            "base_cell_producers": {"op_id": "change_id"},
            "runs": {"op_id": "change_id"},
            "import_sessions": {"op_id": "change_id"},
            "output_column_claims": {"op_id": "change_id"},
            "prepared_page_versions": {"producing_op_id": "producing_change_id"},
            "prepared_content_sets": {"producing_op_id": "producing_change_id"},
            "edits": {"op_id": "change_id"},
            "current_cells": {"origin_op_id": "origin_change_id"},
            "evidence_links": {"op_id": "change_id"},
            "views": {"op_id": "change_id"},
            "watches": {"last_evaluated_op": "last_evaluated_change"},
            "watch_runs": {
                "watch_id": "alert_id",
                "op_cursor_before": "change_cursor_before",
                "op_cursor_after": "change_cursor_after",
            },
            "watch_run_events": {"watch_id": "alert_id"},
            "watch_seen_rows": {"watch_id": "alert_id"},
            "watch_row_field_state": {"watch_id": "alert_id"},
            "materialized_row_sources": {"op_id": "change_id"},
            "document_text_manual_pins": {"edit_op_id": "edit_change_id"},
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
            if table == "meta":
                k_idx = common.index("key")
                for record in records:
                    if record[k_idx] == "change_cursor":
                        record[k_idx] = "op_cursor"
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
