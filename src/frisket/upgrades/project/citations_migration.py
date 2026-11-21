"""One-time project cutover to citation tables and Ask citation locators.

Preserves every citation row, span association, index, foreign key, and
persisted Ask citation payload inside a single fixed-endpoint rebuild step.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from frisket.engine.store.document_text import DOCUMENT_TEXT_SELECTION_VIEW_SQL
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project._step import execute_ddl

CITATIONS_TO_DIGEST = "frisket.schema.v1:394bbd0504816e4447a809136ccc789e"

_CITATIONS_DDL = """
CREATE TABLE IF NOT EXISTS citations (
  id INTEGER PRIMARY KEY,
  stable_id TEXT NOT NULL UNIQUE,
  subject_kind TEXT NOT NULL,
  subject_ref_json TEXT NOT NULL DEFAULT '{}',
  sheet_id INTEGER,
  row_id INTEGER,
  column_id INTEGER,
  run_id INTEGER,
  op_id INTEGER,
  receipt_id TEXT,
  link_role TEXT NOT NULL DEFAULT 'support',
  status TEXT NOT NULL DEFAULT 'active',
  confidence REAL,
  pinned INTEGER NOT NULL DEFAULT 0,
  producer_json TEXT NOT NULL DEFAULT '{}',
  layer_family TEXT CHECK (
    layer_family IS NULL
    OR (length(layer_family) BETWEEN 1 AND 64
        AND substr(layer_family, 1, 1) GLOB '[a-z]'
        AND layer_family NOT GLOB '*[^a-z0-9_.-]*')
  ),
  stale_reason TEXT,
  stale_at TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS citation_spans (
  citation_id INTEGER NOT NULL REFERENCES citations(id) ON DELETE CASCADE,
  span_id INTEGER NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL DEFAULT 0,
  span_role TEXT NOT NULL DEFAULT 'support',
  required INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  PRIMARY KEY (citation_id, span_id)
) WITHOUT ROWID;
"""

_CITATIONS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_citations_cell_status
  ON citations(row_id, column_id, status);
CREATE INDEX IF NOT EXISTS idx_citations_subject
  ON citations(subject_kind, subject_ref_json);
CREATE INDEX IF NOT EXISTS idx_citations_run_cell
  ON citations(run_id, row_id, column_id);
CREATE INDEX IF NOT EXISTS idx_citations_receipt
  ON citations(receipt_id);
CREATE INDEX IF NOT EXISTS idx_citations_sheet_status
  ON citations(sheet_id, status, column_id);
CREATE INDEX IF NOT EXISTS idx_citation_spans_span
  ON citation_spans(span_id);
CREATE INDEX IF NOT EXISTS idx_citation_spans_annotation
  ON citation_spans(span_id, span_role, citation_id);
"""

_CITATION_COLUMNS = (
    "id,stable_id,subject_kind,subject_ref_json,sheet_id,row_id,column_id,"
    "run_id,op_id,receipt_id,link_role,status,confidence,pinned,producer_json,"
    "layer_family,stale_reason,stale_at,metadata,created_at"
)

_LEGACY_KIND = "evidence"
_LEGACY_LINK_KEY = "evidence_link_id"
_LEGACY_PREPARED_KEY = "prepared_evidence_link_id"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rename_key(mapping: dict[str, Any], old_key: str, new_key: str) -> bool:
    if old_key not in mapping:
        return False
    value = mapping.pop(old_key)
    mapping.setdefault(new_key, value)
    return True


def _rewrite_nested_ref(mapping: dict[str, Any], field: str, new_kind: str) -> bool:
    nested = mapping.get(field)
    if not isinstance(nested, dict):
        return False
    updated = dict(nested)
    changed = False
    if updated.get("kind") == _LEGACY_KIND:
        updated["kind"] = new_kind
        changed = True
    if _rename_key(updated, _LEGACY_LINK_KEY, "citation_id"):
        changed = True
    if changed:
        mapping[field] = updated
    return changed


def _rewrite_locator(locator: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated = dict(locator)
    changed = _rename_key(updated, _LEGACY_LINK_KEY, "citation_id")
    changed = (
        _rename_key(updated, _LEGACY_PREPARED_KEY, "prepared_citation_id") or changed
    )
    changed = _rewrite_nested_ref(updated, "navigation", "citation") or changed
    changed = _rewrite_nested_ref(updated, "target", "cell_citation") or changed
    return updated, changed


def _rewrite_event_dict(mapping: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    updated: dict[str, Any] = {}
    for key, item in mapping.items():
        rewritten, item_changed = _rewrite_event_value(item)
        updated[key] = rewritten
        changed = changed or item_changed
    if updated.get("source_kind") == _LEGACY_KIND:
        updated["source_kind"] = "citation"
        changed = True
    if updated.get("kind") == _LEGACY_KIND and (
        _LEGACY_LINK_KEY in updated or "citation_id" in updated
    ):
        updated["kind"] = (
            "cell_citation"
            if ("sheet_id" in updated or "artifact_id" in updated)
            else "citation"
        )
        changed = True
    changed = _rename_key(updated, _LEGACY_LINK_KEY, "citation_id") or changed
    changed = (
        _rename_key(updated, _LEGACY_PREPARED_KEY, "prepared_citation_id") or changed
    )
    return updated, changed


def _rewrite_event_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            rewritten, item_changed = _rewrite_event_value(item)
            items.append(rewritten)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        return _rewrite_event_dict(value)
    return value, False


def _migrate_ask_rows(db: sqlite3.Connection) -> None:
    db.execute(
        "UPDATE project_qa_citations SET source_kind='citation' WHERE source_kind=?",
        (_LEGACY_KIND,),
    )
    rows = db.execute(
        "SELECT id, locator_json FROM project_qa_citations "
        "WHERE locator_json LIKE '%evidence%'"
    ).fetchall()
    for row_id, raw_locator in rows:
        try:
            parsed = json.loads(raw_locator)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        updated, changed = _rewrite_locator(parsed)
        if changed:
            db.execute(
                "UPDATE project_qa_citations SET locator_json=? WHERE id=?",
                (_canonical_json(updated), row_id),
            )

    event_rows = db.execute(
        "SELECT thread_id, seq, payload_json FROM project_qa_events "
        "WHERE payload_json LIKE '%evidence%'"
    ).fetchall()
    for thread_id, seq, raw_payload in event_rows:
        try:
            parsed = json.loads(raw_payload)
        except (TypeError, ValueError):
            continue
        updated, changed = _rewrite_event_value(parsed)
        if changed:
            db.execute(
                "UPDATE project_qa_events SET payload_json=? "
                "WHERE thread_id=? AND seq=?",
                (_canonical_json(updated), thread_id, seq),
            )


def migrate_citations(db: sqlite3.Connection) -> None:
    """Move citation tables and Ask citation payloads onto the current schema."""
    execute_ddl(db, _CITATIONS_DDL)
    db.execute(
        f"INSERT INTO citations ({_CITATION_COLUMNS}) "
        f"SELECT {_CITATION_COLUMNS} FROM evidence_links"
    )
    db.execute(
        "INSERT INTO citation_spans (citation_id,span_id,rank,span_role,required,note) "
        "SELECT link_id,span_id,rank,span_role,required,note FROM evidence_link_spans"
    )
    db.execute("DROP TABLE evidence_link_spans")
    db.execute("DROP TABLE evidence_links")
    execute_ddl(db, _CITATIONS_INDEX_DDL)
    db.execute("DROP VIEW IF EXISTS current_document_text_selections")
    execute_ddl(db, DOCUMENT_TEXT_SELECTION_VIEW_SQL)
    _migrate_ask_rows(db)
    for table in ("citations", "citation_spans"):
        violation = db.execute(f"PRAGMA foreign_key_check({table})").fetchone()
        if violation is not None:
            raise BundleSchemaMismatch(
                f"Citation migration found an invalid reference in {table}: "
                f"{tuple(violation)}"
            )
