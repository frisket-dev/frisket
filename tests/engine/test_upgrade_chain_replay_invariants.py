"""Full-chain project upgrade invariants: schema tip, payload hygiene, and map.python replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from frisket.actions.python_types import ReceiptRecordTarget
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_families.runs import (
    typed_run_has_matching_producer_receipt,
)
from frisket.engine.executor.map_rows_action import (
    _typed_replay_error,
    typed_program_from_runner_spec,
)
from frisket.engine.store import Project
from frisket.engine.store import evaluation as evaluation_store
from frisket.engine.store.action_submissions import record_submission
from frisket.engine.store.current_cells import register_current_cell_functions
from frisket.engine.store.generated_column_claims import GeneratedColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.schema import (
    RECEIPT_RECORDS_META_KEY,
    RECEIPT_RECORDS_VERSION,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
)
from frisket.server.services.action_history import editable_action_draft
from frisket.server.services.run_action_drafts import reconstruct_run_action_draft
from frisket.upgrades.project._tip import CURRENT_CHAIN_TIP_DIGEST
from frisket.upgrades.project.citations_migration import CITATIONS_TO_DIGEST

_PRE_ALERTS_WATCH_SCHEMA_SQL = """
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


def _map_python_request(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["title"],
            "code": (
                "result = {'length': len(str(row['title'])), "
                "'debug': {'len': len(str(row['title']))}}"
            ),
            "return_schema": {
                "type": "object",
                "required": ["length", "debug"],
                "properties": {
                    "length": {"type": "integer"},
                    "debug": {"type": "object"},
                },
            },
            "output_routes": [
                {
                    "name": "length",
                    "path": "$.length",
                    "target": {"kind": "column", "type": "integer"},
                },
                {
                    "name": "debug",
                    "path": "$.debug",
                    "target": {"kind": "receipt_record", "retention": "pinned"},
                },
            ],
        },
        "idempotency_key": key,
    }


def _downgrade_receipt_and_run_rows(
    db: sqlite3.Connection,
    *,
    receipt_id: str,
    run_id: int,
    op_id: int,
    hidden_col_id: int,
) -> None:
    db.execute(
        "UPDATE columns SET name='__evidence_debug' WHERE id=?",
        (hidden_col_id,),
    )
    db.execute(
        "UPDATE run_review_fields SET column_name='__evidence_debug' "
        "WHERE column_id=? AND column_name='__record_debug'",
        (hidden_col_id,),
    )
    db.execute(
        "UPDATE generated_column_claims SET output_name='__evidence_debug' "
        "WHERE column_id=? AND output_name='__record_debug'",
        (hidden_col_id,),
    )

    raw_body = db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()[0]
    body = json.loads(raw_body)
    records = body.pop("records")
    for item in records:
        ref = item.get("ref", {})
        if ref.get("kind") == "map_python_receipt_record":
            ref["kind"] = "map_python_receipt_evidence"
        elif (
            ref.get("kind") == "typed_hidden_output"
            and ref.get("column_id") == hidden_col_id
        ):
            ref["name"] = "__evidence_debug"
        elif ref.get("kind") == "typed_action_request":
            for route in ref.get("params", {}).get("output_routes", []):
                if route.get("target", {}).get("kind") == "receipt_record":
                    route["target"]["kind"] = "receipt_evidence"
    body["evidence"] = records
    db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), receipt_id),
    )

    for table, column, row_id in (("runs", "params", run_id), ("ops", "spec", op_id)):
        raw = db.execute(
            f"SELECT {column} FROM {table} WHERE id=?", (row_id,)
        ).fetchone()[0]
        rewritten = raw.replace('"receipt_record"', '"receipt_evidence"').replace(
            '"__record_debug"', '"__evidence_debug"'
        )
        db.execute(
            f"UPDATE {table} SET {column}=? WHERE id=?",
            (rewritten, row_id),
        )


def _downgrade_generated_columns_tables(
    db: sqlite3.Connection,
    *,
    link_id: int,
) -> None:
    db.execute("ALTER TABLE columns RENAME COLUMN generated TO ai_generated")
    for idx in (
        "idx_generated_column_claims_active_name",
        "idx_generated_column_claims_active_column",
        "idx_generated_column_claims_claim_token",
        "idx_generated_column_claims_run",
        "idx_generated_column_claims_receipt",
        "idx_generated_column_claims_status_expiry",
    ):
        db.execute(f"DROP INDEX IF EXISTS {idx}")
    db.execute("ALTER TABLE generated_column_claims RENAME TO output_column_claims")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_output_claims_active_name "
        "ON output_column_claims(sheet_id, output_name) WHERE status='active'"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_output_claims_active_column "
        "ON output_column_claims(column_id) WHERE status='active' AND column_id IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_output_claims_claim_token "
        "ON output_column_claims(claim_token)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_output_claims_run "
        "ON output_column_claims(run_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_output_claims_receipt "
        "ON output_column_claims(receipt_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_output_claims_status_expiry "
        "ON output_column_claims(status, lease_expires_at)"
    )
    db.execute(
        "ALTER TABLE evaluation_judgments "
        "RENAME COLUMN generated_column_id TO output_column_id"
    )
    db.execute(
        "ALTER TABLE saved_compare_variants "
        "RENAME COLUMN generated_columns_json TO output_columns_json"
    )
    raw_fields = db.execute(
        "SELECT fields_json FROM expected_answer_links WHERE id=?", (link_id,)
    ).fetchone()[0]
    db.execute(
        "UPDATE expected_answer_links SET fields_json=? WHERE id=?",
        (raw_fields.replace('"generated_column_id"', '"output_column_id"'), link_id),
    )


def _downgrade_alerts_and_seed_watch_rows(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    title_col_id: int,
    view_id: int,
) -> None:
    for table in (
        "alert_row_field_state",
        "alert_seen_rows",
        "alert_check_events",
        "alert_check_hits",
        "alert_checks",
        "alerts",
        "project_purge_targets",
    ):
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.executescript(_PRE_ALERTS_WATCH_SCHEMA_SQL)

    db.execute(
        "INSERT INTO watches "
        "(id,name,view_id,detection_policy,enabled,schedule,last_evaluated_op,"
        "last_run_id,baseline_run_id,last_status,created_at,updated_at) "
        "VALUES (5,'Document watch',?,'{\"kind\":\"new_matches\"}',1,'@hourly',3,12,12,'ok',"
        "'2026-01-01 00:00:00','2026-01-02 00:00:00')",
        (view_id,),
    )
    db.execute(
        "INSERT INTO watch_runs "
        "(id,watch_id,status,op_cursor_before,op_cursor_after,matched_rows,new_rows,"
        "error,resolved_query_hash,resolved_query,detection_policy,notification_pending,"
        "error_code,started_at,finished_at) "
        "VALUES (12,5,'ok',2,3,1,1,NULL,'hash12','{\"scope\":{\"kind\":\"sheet\"}}',"
        "'{\"kind\":\"new_matches\"}',0,NULL,'2026-01-02 00:00:00','2026-01-02 00:00:05')"
    )
    db.execute(
        "INSERT INTO watch_run_hits (run_id,sheet_id,row_id,column_id,rank,snippet,is_new) "
        "VALUES (12,?,?,?,1,'Alpha',1)",
        (sheet_id, row_id, title_col_id),
    )
    db.execute(
        "INSERT INTO watch_run_events "
        "(id,run_id,watch_id,event_kind,subject_kind,subject_ref,before_json,after_json,"
        "delta_json,severity,rank,snippet,created_at) "
        "VALUES (31,12,5,'row_entered','row',?,NULL,'{\"is_new\":true}',NULL,'info',1,"
        "'Alpha','2026-01-02 00:00:05')",
        (json.dumps({"sheet_id": sheet_id, "row_id": row_id}),),
    )
    db.execute(
        "INSERT INTO watch_seen_rows (watch_id,sheet_id,row_id,first_seen_run_id,last_seen_run_id) "
        "VALUES (5,?,?,12,12)",
        (sheet_id, row_id),
    )
    db.execute(
        "INSERT INTO watch_row_field_state "
        "(watch_id,sheet_id,row_id,column_id,value_hash,value_json,last_observed_run_id) "
        'VALUES (5,?,?,?,\'sha256:abc\',\'{"outcome":"completed","value":"Alpha"}\',12)',
        (sheet_id, row_id, title_col_id),
    )
    db.execute("INSERT INTO project_purges (id,phase) VALUES (1,'complete')")
    db.execute(
        "INSERT INTO project_purge_targets (kind,object_id,purge_id) VALUES ('watch_run',12,1)"
    )
    db.execute(
        "INSERT INTO notification_items "
        "(id,source_kind,source_ref,dedupe_key,source_event_ids,event_count,event_kinds,"
        "title,summary,severity,deep_link,payload) VALUES "
        "(101,'watch',?,'watch:5:run:12','[31]',1,'[\"row_entered\"]',"
        "'Document watch recorded 1 alert event','Check #12 matched 1 row.','info',?,?)",
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
                    "watch_name": "Document watch",
                    "matched_rows": 1,
                    "new_rows": 1,
                }
            ),
        ),
    )
    db.execute(
        "INSERT INTO notification_channels (id,kind,name,enabled,config_json,secret_ref) "
        "VALUES (201,'slack','Watch Slack channel',1,?,NULL)",
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
    db.execute(
        "INSERT INTO notification_routes "
        "(id,name,enabled,channel_id,owner_kind,owner_ref,source_kind,source_ref_match_json) "
        "VALUES (301,'Watch Slack channel',1,201,'project','chain-invariant','watch',?)",
        (json.dumps({"watch_id": 5}),),
    )


def _seed_pre_upgrade_bundle(bundle_path: Path) -> dict[str, Any]:
    with closing(Project.create(bundle_path, name="chain-invariant")) as seed:
        sheet_id = seed.add_sheet("Documents")
        title_col = seed.add_column(sheet_id, "title", type="text")
        row_ids = seed.add_rows(
            sheet_id,
            [{"title": "Alpha"}, {"title": "Beta"}],
            {"title": title_col},
        )
        expected_sheet = seed.add_sheet("Expected")
        exp_key_col = seed.add_column(expected_sheet, "title", type="text")
        exp_len_col = seed.add_column(expected_sheet, "expected_length", type="integer")

        request = _map_python_request(
            sheet_id, key="chain_invariant_map_python@sha256:v1"
        )
        result = run_action_spec(seed, request, project_id="proj-chain-invariant")
        assert result.status == "completed", result.errors
        assert result.receipt_id is not None and result.run_id is not None
        op_id = result.op_ids[0]

        length_col_id = int(
            seed.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='length'",
                (sheet_id,),
            ).fetchone()[0]
        )
        hidden_col_id = int(
            seed.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='__record_debug'",
                (sheet_id,),
            ).fetchone()[0]
        )

        # Record a pre-upgrade submission draft and evaluation links/variants.
        pre_upgrade_draft = {
            "action_id": "map.python",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "input_columns": ["title"],
                "code": "result = {'debug': row['title']}",
                "return_schema": {
                    "type": "object",
                    "required": ["debug"],
                    "properties": {"debug": {"type": "string"}},
                },
                "output_routes": [
                    {
                        "name": "debug",
                        "path": "$.debug",
                        "target": {"kind": "receipt_evidence", "retention": "pinned"},
                    }
                ],
            },
            "output_names": {},
        }
        submission_id = record_submission(
            seed.db, pre_upgrade_draft, request_key="req-chain-python"
        )
        link_id = int(
            seed.db.execute(
                "INSERT INTO expected_answer_links("
                "target_sheet_id,expected_sheet_id,action_id,keys_json,fields_json,hidden"
                ") VALUES (?,?,?,?,?,0)",
                (
                    sheet_id,
                    expected_sheet,
                    "map.python",
                    json.dumps(
                        [
                            {
                                "expected_column_id": exp_key_col,
                                "target_column_id": title_col,
                            }
                        ]
                    ),
                    json.dumps(
                        [
                            {
                                "expected_column_id": exp_len_col,
                                "generated_column_id": length_col_id,
                                "logical_key": "length",
                                "hidden": False,
                                "source": "import",
                            }
                        ]
                    ),
                ),
            ).lastrowid
            or 0
        )
        seed.db.execute(
            "INSERT INTO evaluation_judgments("
            "sheet_id,row_id,expected_column_id,generated_column_id,"
            "source_fingerprint,expected_hash,answer_hash,verdict,actor"
            ") VALUES (?,?,?,?,'fp-chain','exp-h','ans-h','match','alice')",
            (sheet_id, row_ids[0], exp_len_col, length_col_id),
        )
        saved_variant_id = int(
            seed.db.execute(
                "INSERT INTO saved_compare_variants("
                "sheet_id,action_id,label,generated_columns_json,settings_json,"
                "rows_json,fingerprints_json,kept"
                ") VALUES (?,'map.python','v1',?,'{}','{}','{}',1)",
                (
                    sheet_id,
                    json.dumps(
                        [
                            {
                                "logical_key": "length",
                                "column_id": length_col_id,
                                "current_name": "length",
                            }
                        ]
                    ),
                ),
            ).lastrowid
            or 0
        )
        seed.db.execute(
            "INSERT INTO generated_column_claims("
            "id,sheet_id,column_id,output_name,action_kind,claim_token,status,details"
            ") VALUES ('claim-chain-debug',?,?,'__record_debug','map.python',"
            "'tok-chain-debug','released','{}')",
            (sheet_id, hidden_col_id),
        )
        seed.db.commit()
        view_id = seed.add_view(
            "Documents view",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filters": [],
                }
            },
        )

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        register_current_cell_functions(raw)
        raw.execute("PRAGMA foreign_keys=OFF")
        _downgrade_receipt_and_run_rows(
            raw,
            receipt_id=result.receipt_id,
            run_id=result.run_id,
            op_id=op_id,
            hidden_col_id=hidden_col_id,
        )
        _downgrade_generated_columns_tables(raw, link_id=link_id)
        _downgrade_alerts_and_seed_watch_rows(
            raw,
            sheet_id=sheet_id,
            row_id=row_ids[0],
            title_col_id=title_col,
            view_id=view_id,
        )
        raw.execute("DELETE FROM meta WHERE key=?", (RECEIPT_RECORDS_META_KEY,))
        raw.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (CITATIONS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
        raw.commit()
        raw.execute("PRAGMA foreign_keys=ON")
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        raw.close()

    return {
        "request": request,
        "sheet_id": sheet_id,
        "row_id": row_ids[0],
        "title_col": title_col,
        "length_col_id": length_col_id,
        "hidden_col_id": hidden_col_id,
        "exp_len_col": exp_len_col,
        "receipt_id": result.receipt_id,
        "run_id": result.run_id,
        "op_id": op_id,
        "submission_id": submission_id,
        "pre_upgrade_draft": pre_upgrade_draft,
        "link_id": link_id,
        "saved_variant_id": saved_variant_id,
    }


def test_full_upgrade_chain_preserves_payload_hygiene_and_map_python_replay(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "full-chain-replay.frisket"
    fixture = _seed_pre_upgrade_bundle(bundle_path)

    with closing(Project(bundle_path)) as upgraded:
        # (a) Schema digest reaches tip and relational integrity holds.
        assert (
            upgraded.get_meta(SCHEMA_DIGEST_META_KEY)
            == CURRENT_CHAIN_TIP_DIGEST
            == SCHEMA_DIGEST
        )
        assert upgraded.get_meta(RECEIPT_RECORDS_META_KEY) == RECEIPT_RECORDS_VERSION
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchall() == []

        tables = {
            row[0]
            for row in upgraded.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "generated_column_claims",
            "alerts",
            "alert_checks",
            "alert_check_hits",
            "alert_check_events",
            "alert_seen_rows",
            "alert_row_field_state",
        } <= tables
        assert not (
            {
                "output_column_claims",
                "watches",
                "watch_runs",
                "watch_run_hits",
                "watch_run_events",
                "watch_seen_rows",
                "watch_row_field_state",
            }
            & tables
        )

        # (b) Upgraded payloads are valid and carry zero stale pre-cutover keys.
        raw_receipt = ReceiptStore(upgraded).body_by_id(fixture["receipt_id"])
        assert raw_receipt is not None
        receipt_dict = json.loads(raw_receipt)
        assert "evidence" not in receipt_dict
        assert "records" in receipt_dict
        for stale_token in (
            "map_python_receipt_evidence",
            "receipt_evidence",
            "__evidence_debug",
        ):
            assert stale_token not in raw_receipt
        parsed_receipt = ReceiptStore(upgraded).require_parsed(fixture["receipt_id"])
        ref_kinds = {item.ref.get("kind") for item in parsed_receipt.records}
        assert "map_python_receipt_record" in ref_kinds
        assert "typed_hidden_output" in ref_kinds
        assert "typed_action_request" in ref_kinds

        raw_run_params = upgraded.db.execute(
            "SELECT params FROM runs WHERE id=?", (fixture["run_id"],)
        ).fetchone()[0]
        assert "receipt_evidence" not in raw_run_params
        assert "__evidence_debug" not in raw_run_params
        run_params = json.loads(raw_run_params)
        assert run_params["output_names"]["debug"] == "__record_debug"
        assert (
            run_params["params"]["output_routes"][1]["target"]["kind"]
            == "receipt_record"
        )

        raw_op_spec = upgraded.db.execute(
            "SELECT spec FROM ops WHERE id=?", (fixture["op_id"],)
        ).fetchone()[0]
        assert "receipt_evidence" not in raw_op_spec
        assert "__evidence_debug" not in raw_op_spec
        op_spec = json.loads(raw_op_spec)
        assert op_spec["output_names"]["debug"] == "__record_debug"

        sub_row = upgraded.db.execute(
            "SELECT draft_hash, draft_json FROM action_submissions WHERE id=?",
            (fixture["submission_id"],),
        ).fetchone()
        assert sub_row is not None
        assert "receipt_evidence" not in sub_row[1]
        expected_draft = json.loads(
            json.dumps(fixture["pre_upgrade_draft"]).replace(
                '"receipt_evidence"', '"receipt_record"'
            )
        )
        expected_encoded = json.dumps(
            expected_draft, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        assert sub_row[0] == hashlib.sha256(expected_encoded.encode()).hexdigest()
        restored_draft = json.loads(sub_row[1])
        assert restored_draft == expected_draft
        assert editable_action_draft(restored_draft).action_id == "map.python"
        bound_draft = typed_action_for_request(
            {**restored_draft, "idempotency_key": "restored-chain@sha256:v1"}
        )
        assert isinstance(
            bound_draft.params.output_routes[0].target, ReceiptRecordTarget
        )

        cols_by_name = {
            col["name"]: bool(col["generated"])
            for col in upgraded.columns(fixture["sheet_id"], include_hidden=True)
        }
        assert cols_by_name["title"] is False
        assert cols_by_name["length"] is True
        assert "__record_debug" in cols_by_name
        assert "__evidence_debug" not in cols_by_name

        claim_names = [
            row[0]
            for row in upgraded.db.execute(
                "SELECT output_name FROM generated_column_claims WHERE column_id=?",
                (fixture["hidden_col_id"],),
            ).fetchall()
        ]
        assert claim_names and all(name == "__record_debug" for name in claim_names)
        assert (
            GeneratedColumnClaimStore(upgraded).active_for_columns(
                {fixture["length_col_id"]}
            )
            is None
        )

        link = evaluation_store.link(upgraded, fixture["link_id"])
        assert link is not None
        assert link["fields"][0]["generated_column_id"] == fixture["length_col_id"]
        assert "output_column_id" not in link["fields"][0]
        assert (
            evaluation_store.judgment(
                upgraded,
                {
                    "sheet_id": fixture["sheet_id"],
                    "row_id": fixture["row_id"],
                    "expected_column_id": fixture["exp_len_col"],
                    "generated_column_id": fixture["length_col_id"],
                    "source_fingerprint": "fp-chain",
                    "expected_hash": "exp-h",
                    "answer_hash": "ans-h",
                },
            )
            == "match"
        )
        saved_variant = evaluation_store.saved(upgraded, fixture["saved_variant_id"])
        assert saved_variant is not None
        assert (
            saved_variant["generated_columns"][0]["column_id"]
            == fixture["length_col_id"]
        )

        notif = upgraded.notification_item_by_dedupe_key("alert:5:run:12")
        assert notif is not None
        assert notif["source_kind"] == "alert"
        assert json.loads(notif["source_ref"]) == {"alert_id": 5, "run_id": 12}
        assert json.loads(notif["deep_link"]) == {
            "kind": "alert_check",
            "alert_id": 5,
            "run_id": 12,
            "event_ids": [31],
        }
        notif_payload = json.loads(notif["payload"])
        assert notif_payload["alert_name"] == "Document watch"
        assert "watch_name" not in notif_payload
        for field_name in ("source_ref", "dedupe_key", "deep_link", "payload"):
            assert "watch_" not in str(notif[field_name])
            assert "watch:" not in str(notif[field_name])

        routes = {int(r["id"]): r for r in upgraded.notification_routes()}
        assert routes[301]["source_kind"] == "alert"
        assert json.loads(routes[301]["source_ref_match_json"]) == {"alert_id": 5}
        channels = {int(c["id"]): c for c in upgraded.notification_channels()}
        assert json.loads(channels[201]["config_json"])["managed"] == "alert_slack"
        assert json.loads(channels[201]["config_json"])["alert_id"] == 5
        assert "watch_id" not in json.loads(channels[201]["config_json"])
        assert upgraded.get_alert(5) is not None
        assert upgraded.get_alert_check(12) is not None

        # (c) map.python replay succeeds cleanly on the upgraded bundle without stale_replay.
        bound_request = typed_action_for_request(fixture["request"])
        assert _typed_replay_error(upgraded, bound_request)(parsed_receipt) is None

        run_count_before = int(
            upgraded.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        )
        replayed = run_action_spec(
            upgraded, fixture["request"], project_id="proj-chain-invariant"
        )
        assert replayed.status == "completed", replayed.errors
        assert replayed.errors == []
        assert replayed.receipt_id == fixture["receipt_id"]
        assert replayed.run_id == fixture["run_id"]
        run_count_after = int(
            upgraded.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        )
        assert run_count_after == run_count_before

        run_row = upgraded.db.execute(
            "SELECT * FROM runs WHERE id=?", (fixture["run_id"],)
        ).fetchone()
        assert run_row is not None
        reconstructed = reconstruct_run_action_draft(upgraded, run_row)
        assert typed_run_has_matching_producer_receipt(
            upgraded, run_id=int(fixture["run_id"]), bound=reconstructed.bound
        )
        assert (
            typed_program_from_runner_spec(upgraded, reconstructed.runner_spec)
            is not None
        )

    with closing(Project(bundle_path)) as reopened:
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == CURRENT_CHAIN_TIP_DIGEST
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
