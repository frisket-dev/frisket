"""Data-preserving upgrade from legacy evidence tables and Ask citation JSON."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from frisket.contracts.http.project_qa import AskCellCitationTarget, AskCitation
from frisket.engine.store import Project
from frisket.engine.store.citations import (
    list_cell_citations,
    record_citation,
    record_source_artifact,
    record_source_span,
    resolve_citation_viewer,
)
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.schema import SCHEMA_DIGEST
from frisket.engine.store.transactions import write_transaction
from frisket.server.services.project_qa_citations import resolve_citation
from frisket.upgrades.project.citations_migration import CITATIONS_TO_DIGEST
from frisket.upgrades.project.research_pause_migration import (
    RESEARCH_PAUSE_TO_DIGEST,
)

_LEGACY_TABLES_DDL = """
CREATE TABLE evidence_links (
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
CREATE INDEX idx_evidence_links_cell_status
  ON evidence_links(row_id, column_id, status);
CREATE INDEX idx_evidence_links_subject
  ON evidence_links(subject_kind, subject_ref_json);
CREATE INDEX idx_evidence_links_run_cell
  ON evidence_links(run_id, row_id, column_id);
CREATE INDEX idx_evidence_links_receipt
  ON evidence_links(receipt_id);
CREATE INDEX idx_evidence_links_sheet_status
  ON evidence_links(sheet_id, status, column_id);
CREATE TABLE evidence_link_spans (
  link_id INTEGER NOT NULL REFERENCES evidence_links(id) ON DELETE CASCADE,
  span_id INTEGER NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL DEFAULT 0,
  span_role TEXT NOT NULL DEFAULT 'support',
  required INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  PRIMARY KEY (link_id, span_id)
) WITHOUT ROWID;
CREATE INDEX idx_evidence_link_spans_span
  ON evidence_link_spans(span_id);
CREATE INDEX idx_evidence_link_spans_annotation
  ON evidence_link_spans(span_id, span_role, link_id);
"""


def _downgrade_to_pre_citations_schema(db: sqlite3.Connection) -> None:
    """Convert a fresh bundle's citation tables back to RESEARCH_PAUSE_TO_DIGEST."""
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN")
        view_sql = db.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='view' AND name='current_document_text_selections'"
        ).fetchone()[0]
        legacy_view_sql = (
            view_sql.replace(
                "JOIN citation_spans linked", "JOIN evidence_link_spans linked"
            )
            .replace(
                "JOIN citations source_link ON source_link.id=linked.citation_id",
                "JOIN evidence_links source_link ON source_link.id=linked.link_id",
            )
            .replace("ops citation_owner", "ops evidence_owner")
            .replace("citation_owner.", "evidence_owner.")
        )
        for statement in _LEGACY_TABLES_DDL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                db.execute(stmt)
        cols = (
            "id,stable_id,subject_kind,subject_ref_json,sheet_id,row_id,column_id,"
            "run_id,op_id,receipt_id,link_role,status,confidence,pinned,producer_json,"
            "layer_family,stale_reason,stale_at,metadata,created_at"
        )
        db.execute(f"INSERT INTO evidence_links ({cols}) SELECT {cols} FROM citations")
        db.execute(
            "INSERT INTO evidence_link_spans (link_id,span_id,rank,span_role,required,note) "
            "SELECT citation_id,span_id,rank,span_role,required,note FROM citation_spans"
        )
        db.execute("DROP TABLE citation_spans")
        db.execute("DROP TABLE citations")
        db.execute("DROP VIEW IF EXISTS current_document_text_selections")
        db.execute(legacy_view_sql)
        db.execute(
            "UPDATE meta SET value=? WHERE key='schema_digest'",
            (RESEARCH_PAUSE_TO_DIGEST,),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def test_citations_upgrade_preserves_links_spans_and_ask_threads(make_project) -> None:
    project = make_project("citation-upgrade")
    sheet_id = project.add_sheet("Findings")
    col_id = project.add_column(sheet_id, "Summary", type="text")
    [row_id] = project.add_rows(
        sheet_id, [{"Summary": "Bridge repair approved"}], {"Summary": col_id}
    )
    _values, refs = project.get_values_with_refs(sheet_id, col_id, row_ids=[row_id])
    value_ref = refs[row_id]

    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            title="Council Minutes",
            filename="minutes.pdf",
            page_count=3,
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=col_id,
        )
        span_primary = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page_range",
            page_start=2,
            page_end=2,
            quote="Bridge repair approved unanimously.",
            snippet="Bridge repair approved unanimously.",
        )
        span_extra = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page_range",
            page_start=3,
            page_end=3,
            quote="Budget allocation confirmed.",
            snippet="Budget allocation confirmed.",
        )
        active_link = record_citation(
            project,
            subject_kind="cell",
            subject_ref=value_ref,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=col_id,
            link_role="primary_support",
            status="active",
            confidence=0.92,
            producer={"action_kind": "map.extract", "model": "test/model"},
            spans=[
                {
                    "span_id": span_primary["id"],
                    "rank": 0,
                    "span_role": "support",
                    "required": True,
                    "note": "first",
                },
                {
                    "span_id": span_extra["id"],
                    "rank": 1,
                    "span_role": "support",
                    "required": False,
                    "note": "second",
                },
            ],
        )
        stale_link = record_citation(
            project,
            subject_kind="cell",
            subject_ref={"kind": "legacy"},
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=col_id,
            link_role="primary_support",
            status="stale",
            stale_reason="manual_edit",
            spans=[
                {
                    "span_id": span_primary["id"],
                    "rank": 0,
                    "span_role": "support",
                    "required": True,
                }
            ],
        )

    qa_store = ProjectQAStore(project)
    thread = qa_store.create_thread(title="Bridge inquiry", scope={"kind": "project"})
    turn = qa_store.submit_turn(
        thread["id"],
        request_id="req-1",
        question="Was the bridge repair approved?",
        scope={"kind": "project"},
    )
    legacy_locator = {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": col_id,
        "value_ref": value_ref,
        "evidence_link_id": active_link["stable_id"],
        "prepared_evidence_link_id": active_link["stable_id"],
        "artifact_id": artifact["stable_id"],
        "span_id": span_primary["stable_id"],
        "navigation": {
            "kind": "evidence",
            "evidence_link_id": active_link["stable_id"],
            "span_id": span_primary["stable_id"],
            "span_ids": [span_primary["stable_id"], span_extra["stable_id"]],
        },
    }
    qa_citation = qa_store.add_citation(
        turn["id"],
        label="Findings · row 1 · Summary",
        source_kind="evidence",
        locator=legacy_locator,
        excerpt="Bridge repair approved unanimously.",
    )
    qa_store.append_event(
        turn["id"],
        kind="answer",
        payload={
            "text": "Yes, the bridge repair was approved.",
            "citation_ids": [qa_citation["id"]],
            "saved_citation_snapshot": {
                "id": qa_citation["id"],
                "label": "Findings · row 1 · Summary",
                "source_kind": "evidence",
                "excerpt": "Bridge repair approved unanimously.",
                "status": "current",
                "message": None,
                "target": {
                    "kind": "evidence",
                    "sheet_id": sheet_id,
                    "row_id": row_id,
                    "column_id": col_id,
                    "evidence_link_id": active_link["stable_id"],
                    "artifact_id": artifact["stable_id"],
                    "span_id": span_primary["stable_id"],
                    "span_ids": [span_primary["stable_id"], span_extra["stable_id"]],
                },
            },
        },
    )

    _downgrade_to_pre_citations_schema(project.db)
    bundle_path = project.path
    project.close()

    with closing(Project(bundle_path)) as upgraded:
        assert (
            upgraded.db.execute(
                "SELECT value FROM meta WHERE key='schema_digest'"
            ).fetchone()[0]
            == SCHEMA_DIGEST
            == CITATIONS_TO_DIGEST
        )
        tables = {
            row[0]
            for row in upgraded.db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert "citations" in tables
        assert "citation_spans" in tables
        assert "evidence_links" not in tables
        assert "evidence_link_spans" not in tables

        indexes = {
            row[0]
            for row in upgraded.db.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'"
            )
        }
        assert {
            "idx_citations_cell_status",
            "idx_citations_subject",
            "idx_citations_run_cell",
            "idx_citations_receipt",
            "idx_citations_sheet_status",
            "idx_citation_spans_span",
            "idx_citation_spans_annotation",
        } <= indexes

        span_columns = {
            row[1] for row in upgraded.db.execute("PRAGMA table_info(citation_spans)")
        }
        assert "citation_id" in span_columns
        assert "link_id" not in span_columns
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchone() is None

        # Verify all citation and citation_span rows survived intact.
        citation_rows = upgraded.db.execute(
            "SELECT id, stable_id, status, stale_reason FROM citations ORDER BY id"
        ).fetchall()
        assert [
            (r["id"], r["stable_id"], r["status"], r["stale_reason"])
            for r in citation_rows
        ] == [
            (active_link["id"], active_link["stable_id"], "active", None),
            (stale_link["id"], stale_link["stable_id"], "stale", "manual_edit"),
        ]
        span_links = upgraded.db.execute(
            "SELECT citation_id, span_id, rank, required, note FROM citation_spans ORDER BY citation_id, rank"
        ).fetchall()
        assert [tuple(r) for r in span_links] == [
            (active_link["id"], span_primary["id"], 0, 1, "first"),
            (active_link["id"], span_extra["id"], 1, 0, "second"),
            (stale_link["id"], span_primary["id"], 0, 1, None),
        ]

        # Runtime citation readers and document text selection view work cleanly.
        cell_payload = list_cell_citations(
            upgraded,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=col_id,
            include_stale=True,
            project_id="proj-1",
        )
        assert cell_payload["schema_version"] == "frisket.cell_citations.v1"
        assert cell_payload["stale_count"] == 1
        assert [link["stable_id"] for link in cell_payload["links"]] == [
            active_link["stable_id"],
            stale_link["stable_id"],
        ]
        viewer = resolve_citation_viewer(
            upgraded, active_link["stable_id"], project_id="proj-1"
        )
        assert viewer["schema_version"] == "frisket.citation_viewer.v1"
        assert len(viewer["artifacts"][0]["spans"]) == 2
        upgraded.db.execute("SELECT * FROM current_document_text_selections").fetchall()

        # Persisted Ask citation row migrated cleanly and validates against AskCellCitationTarget.
        migrated_qa_row = upgraded.db.execute(
            "SELECT source_kind, locator_json FROM project_qa_citations WHERE id=?",
            (qa_citation["id"],),
        ).fetchone()
        assert migrated_qa_row["source_kind"] == "citation"
        migrated_locator = json.loads(migrated_qa_row["locator_json"])
        assert migrated_locator["citation_id"] == active_link["stable_id"]
        assert migrated_locator["prepared_citation_id"] == active_link["stable_id"]
        assert "evidence_link_id" not in migrated_locator
        assert "prepared_evidence_link_id" not in migrated_locator
        assert migrated_locator["navigation"] == {
            "kind": "citation",
            "citation_id": active_link["stable_id"],
            "span_id": span_primary["stable_id"],
            "span_ids": [span_primary["stable_id"], span_extra["stable_id"]],
        }

        resolved_ask = resolve_citation(upgraded, thread["id"], qa_citation["id"])
        validated = AskCitation.model_validate(resolved_ask)
        assert validated.source_kind == "citation"
        assert isinstance(validated.target, AskCellCitationTarget)
        assert validated.target.kind == "cell_citation"
        assert validated.target.citation_id == active_link["stable_id"]
        assert validated.target.span_ids == [
            span_primary["stable_id"],
            span_extra["stable_id"],
        ]

        # Persisted Ask event snapshot also migrated and validates cleanly.
        event_row = upgraded.db.execute(
            "SELECT payload_json FROM project_qa_events WHERE turn_id=? AND kind='answer'",
            (turn["id"],),
        ).fetchone()
        snapshot = json.loads(event_row["payload_json"])["saved_citation_snapshot"]
        validated_snapshot = AskCitation.model_validate(snapshot)
        assert validated_snapshot.source_kind == "citation"
        assert isinstance(validated_snapshot.target, AskCellCitationTarget)
        assert validated_snapshot.target.citation_id == active_link["stable_id"]

    # Reopening an already upgraded bundle is a clean no-op.
    with closing(Project(bundle_path)) as reopened:
        assert reopened.db.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 2
