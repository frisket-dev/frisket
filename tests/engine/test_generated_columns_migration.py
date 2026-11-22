"""Lossless upgrade from RECEIPT_RECORDS_TO_DIGEST to generated-column schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from frisket.engine.executor.action_families.runs import (
    typed_run_has_matching_producer_receipt,
)
from frisket.engine.executor.map_rows_action import typed_program_from_runner_spec
from frisket.engine.store import Project
from frisket.engine.store import evaluation as evaluation_store
from frisket.engine.store.current_cells import register_current_cell_functions
from frisket.engine.store.generated_column_claims import GeneratedColumnClaimStore
from frisket.engine.store.schema import (
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
)
from frisket.server.services.run_action_drafts import reconstruct_run_action_draft
from frisket.upgrades.project._step import run_step
from frisket.upgrades.project._tip import CURRENT_CHAIN_TIP_DIGEST
from frisket.upgrades.project.generated_columns_migration import (
    GENERATED_COLUMNS_FROM_DIGEST,
    GENERATED_COLUMNS_TO_DIGEST,
    apply_generated_columns_migration,
)
from frisket.upgrades.project.receipt_records_migration import (
    RECEIPT_RECORDS_TO_DIGEST,
)
from tests.engine.historical_schema import (
    restore_pre_changes_schema,
    restore_pre_feeds_tables,
)
from tests.engine.test_alerts_migration import _PRE_UPGRADE_WATCH_TABLES_SQL


def _downgrade_generated_columns_schema(db: sqlite3.Connection) -> None:
    """Restore pre-generated-columns table/column names on a test fixture DB."""
    register_current_cell_functions(db)
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
        "CREATE INDEX IF NOT EXISTS idx_output_claims_run ON output_column_claims(run_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_output_claims_receipt ON output_column_claims(receipt_id)"
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


def _build_pre_upgrade_bundle(path: Path) -> dict[str, int]:
    with closing(Project.create(path, name="Generated Columns Migration")):
        pass

    db = sqlite3.connect(path / "project.db")
    try:
        register_current_cell_functions(db)
        restore_pre_changes_schema(db)
        restore_pre_feeds_tables(db)
        db.execute("PRAGMA foreign_keys=OFF")
        for tbl in (
            "alert_row_field_state",
            "alert_seen_rows",
            "alert_check_events",
            "alert_check_hits",
            "alert_checks",
            "alerts",
            "project_purge_targets",
        ):
            db.execute(f"DROP TABLE IF EXISTS {tbl}")
        db.executescript(_PRE_UPGRADE_WATCH_TABLES_SQL)
        db.execute("BEGIN")
        _downgrade_generated_columns_schema(db)
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
            (SCHEMA_DIGEST_META_KEY, RECEIPT_RECORDS_TO_DIGEST),
        )
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version','1')"
        )
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('op_cursor','0')")
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('change_sequence','0')"
        )
        db.commit()

        target_sheet = db.execute("INSERT INTO sheets(name) VALUES ('Docs')").lastrowid
        expected_sheet = db.execute(
            "INSERT INTO sheets(name) VALUES ('Expected')"
        ).lastrowid
        assert target_sheet is not None and expected_sheet is not None

        source_col = db.execute(
            "INSERT INTO columns(sheet_id,name,type,position,ai_generated) "
            "VALUES (?,'body','text',1,0)",
            (target_sheet,),
        ).lastrowid
        gen_col = db.execute(
            "INSERT INTO columns(sheet_id,name,type,position,ai_generated) "
            "VALUES (?,'summary','text',2,1)",
            (target_sheet,),
        ).lastrowid
        exp_key_col = db.execute(
            "INSERT INTO columns(sheet_id,name,type,position,ai_generated) "
            "VALUES (?,'doc_id','text',1,0)",
            (expected_sheet,),
        ).lastrowid
        exp_col = db.execute(
            "INSERT INTO columns(sheet_id,name,type,position,ai_generated) "
            "VALUES (?,'expected_summary','text',2,0)",
            (expected_sheet,),
        ).lastrowid
        assert (
            source_col is not None
            and gen_col is not None
            and exp_key_col is not None
            and exp_col is not None
        )

        row_id = db.execute(
            "INSERT INTO rows(sheet_id,position) VALUES (?,1)", (target_sheet,)
        ).lastrowid
        assert row_id is not None

        db.execute(
            "INSERT INTO output_column_claims("
            "id,sheet_id,column_id,output_name,action_kind,claim_token,status,details"
            ") VALUES ('claim-active',?,?,'summary','map.ask','tok-active','active','{}')",
            (target_sheet, gen_col),
        )
        db.execute(
            "INSERT INTO output_column_claims("
            "id,sheet_id,column_id,output_name,action_kind,claim_token,status,details"
            ") VALUES ('claim-done',?,?,'summary','map.ask','tok-done','released','{\"done\":true}')",
            (target_sheet, gen_col),
        )

        link_id = db.execute(
            "INSERT INTO expected_answer_links("
            "target_sheet_id,expected_sheet_id,action_id,keys_json,fields_json,hidden"
            ") VALUES (?,?,?,?,?,0)",
            (
                target_sheet,
                expected_sheet,
                "map.ask",
                json.dumps(
                    [
                        {
                            "expected_column_id": exp_key_col,
                            "target_column_id": source_col,
                        }
                    ]
                ),
                json.dumps(
                    [
                        {
                            "expected_column_id": exp_col,
                            "output_column_id": gen_col,
                            "logical_key": "summary",
                            "hidden": False,
                            "source": "import",
                        }
                    ]
                ),
            ),
        ).lastrowid
        assert link_id is not None

        db.execute(
            "INSERT INTO evaluation_judgments("
            "sheet_id,row_id,expected_column_id,output_column_id,"
            "source_fingerprint,expected_hash,answer_hash,verdict,actor"
            ") VALUES (?,?,?,?,'fp-1','exp-h','ans-h','match','alice')",
            (target_sheet, row_id, exp_col, gen_col),
        )

        saved_id = db.execute(
            "INSERT INTO saved_compare_variants("
            "sheet_id,action_id,label,output_columns_json,settings_json,"
            "rows_json,fingerprints_json,kept"
            ") VALUES (?,'map.ask','v1',?,'{}','{}','{}',1)",
            (
                target_sheet,
                json.dumps(
                    [
                        {
                            "logical_key": "summary",
                            "column_id": gen_col,
                            "current_name": "summary",
                        }
                    ]
                ),
            ),
        ).lastrowid
        assert saved_id is not None
        db.commit()
    finally:
        db.close()

    return {
        "target_sheet": int(target_sheet),
        "expected_sheet": int(expected_sheet),
        "source_col": int(source_col),
        "gen_col": int(gen_col),
        "exp_col": int(exp_col),
        "row_id": int(row_id),
        "link_id": int(link_id),
        "saved_id": int(saved_id),
    }


def test_generated_columns_migration_preserves_columns_claims_and_evaluation(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "legacy-bundle"
    ids = _build_pre_upgrade_bundle(bundle)

    with closing(Project(bundle)) as project:
        digest = project.db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()[0]
        assert digest == SCHEMA_DIGEST == CURRENT_CHAIN_TIP_DIGEST
        assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []

        tables = {
            row[0]
            for row in project.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "generated_column_claims" in tables
        assert "output_column_claims" not in tables

        indexes = {
            row[0]
            for row in project.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for old_idx in (
            "idx_output_claims_active_name",
            "idx_output_claims_active_column",
            "idx_output_claims_claim_token",
            "idx_output_claims_run",
            "idx_output_claims_receipt",
            "idx_output_claims_status_expiry",
        ):
            assert old_idx not in indexes
        for new_idx in (
            "idx_generated_column_claims_active_name",
            "idx_generated_column_claims_active_column",
            "idx_generated_column_claims_claim_token",
            "idx_generated_column_claims_run",
            "idx_generated_column_claims_receipt",
            "idx_generated_column_claims_status_expiry",
        ):
            assert new_idx in indexes

        cols = {
            col["name"]: bool(col["generated"])
            for col in project.columns(ids["target_sheet"])
        }
        assert cols == {"body": False, "summary": True}

        claims_store = GeneratedColumnClaimStore(project)
        active = claims_store.active_for_columns({ids["gen_col"]})
        assert active is not None
        assert active["id"] == "claim-active"
        assert active["claim_token"] == "tok-active"
        active_by_name = claims_store.active_for_output_name(
            sheet_id=ids["target_sheet"], output_name="summary"
        )
        assert active_by_name is not None
        assert active_by_name["id"] == "claim-active"
        all_claim_ids = {
            row["id"]
            for row in project.db.execute(
                "SELECT id FROM generated_column_claims"
            ).fetchall()
        }
        assert all_claim_ids == {"claim-active", "claim-done"}

        verdict = evaluation_store.judgment(
            project,
            {
                "sheet_id": ids["target_sheet"],
                "row_id": ids["row_id"],
                "expected_column_id": ids["exp_col"],
                "generated_column_id": ids["gen_col"],
                "source_fingerprint": "fp-1",
                "expected_hash": "exp-h",
                "answer_hash": "ans-h",
            },
        )
        assert verdict == "match"

        saved_variant = evaluation_store.saved(project, ids["saved_id"])
        assert saved_variant is not None
        assert saved_variant["generated_columns"] == [
            {
                "logical_key": "summary",
                "column_id": ids["gen_col"],
                "current_name": "summary",
            }
        ]

        link = evaluation_store.link(project, ids["link_id"])
        assert link is not None
        assert link["fields"] == [
            {
                "expected_column_id": ids["exp_col"],
                "generated_column_id": ids["gen_col"],
                "logical_key": "summary",
                "hidden": False,
                "source": "import",
                "rule": "exact",
            }
        ]

        # Re-running the step against an already-upgraded DB is a no-op.
        run_step(
            project.db,
            GENERATED_COLUMNS_FROM_DIGEST,
            GENERATED_COLUMNS_TO_DIGEST,
            apply_generated_columns_migration,
        )
        assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pre_upgrade_replacement_run_keeps_receipt_hash_and_draft_reconstruction(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "legacy-replacement-bundle"
    ids = _build_pre_upgrade_bundle(bundle)
    sheet_id = ids["target_sheet"]
    gen_col = ids["gen_col"]

    # Seed a pre-upgrade map.python replacement run and producer receipt whose
    # durable runner spec and params_hash use the pre-upgrade precondition key.
    action_params = {
        "input_columns": ["body"],
        "code": "result = row['body']",
        "return_schema": {"type": "string"},
        "output_routes": [
            {
                "name": "summary",
                "path": "$",
                "target": {"kind": "column", "type": "text"},
            }
        ],
    }
    pre_upgrade_runner_spec = {
        "action_kind": "map.python",
        "action_version": "1",
        "sheet_id": sheet_id,
        "input_columns": ["body"],
        "params": action_params,
        "output_names": {"summary": "summary"},
        "expected_output_columns": {"summary": gen_col},
        "replace_existing": True,
        "output_target_preconditions": {"summary": gen_col},
        "overwrite": True,
    }
    pre_upgrade_hash_identity = {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": None},
        "params": action_params,
        "output_names": {"summary": "summary"},
        "replace_existing": True,
        "expected_output_columns": {"summary": gen_col},
    }
    pre_upgrade_params_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                pre_upgrade_hash_identity,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )

    db = sqlite3.connect(bundle / "project.db")
    try:
        op_id = db.execute(
            "INSERT INTO ops(kind,label,status) VALUES ('map.python','summary','applied')"
        ).lastrowid
        assert op_id is not None
        run_id = db.execute(
            "INSERT INTO runs(op_id,sheet_id,action_kind,status,params,total_rows,completed_rows) "
            "VALUES (?,?,'map.python','completed',?,1,1)",
            (op_id, sheet_id, json.dumps(pre_upgrade_runner_spec)),
        ).lastrowid
        assert run_id is not None
        receipt_body = json.dumps(
            {
                "schema_version": "frisket.receipt.v1",
                "receipt_id": "rcpt-legacy-replace",
                "project_id": "legacy-proj",
                "action_id": "act-legacy-replace",
                "action_kind": "map.python",
                "status": "completed",
                "params_hash": pre_upgrade_params_hash,
                "inputs": [],
                "outputs": [],
                "evidence": [],
                "op_ids": [int(op_id)],
                "run_ids": [int(run_id)],
                "errors": [],
                "created_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            }
        )
        db.execute(
            "INSERT INTO run_output_generations("
            "run_id,column_id,output_role,output_descriptor_json,compatibility_key,"
            "write_mode,state,claim_token,terminal_disposition,sealed_at"
            ") VALUES (?,?,'summary','{\"column_type\":\"text\"}','text-v1',"
            "'replace_scope','sealed','tok-done','completed',datetime('now'))",
            (run_id, gen_col),
        )
        db.execute(
            "INSERT INTO receipts(id,run_id,action_kind,idempotency_key,params_hash,status,body) "
            "VALUES ('rcpt-legacy-replace',?,'map.python','idem-legacy',?,'completed',?)",
            (run_id, pre_upgrade_params_hash, receipt_body),
        )
        db.commit()
    finally:
        db.close()

    with closing(Project(bundle)) as project:
        run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        assert run is not None
        reconstructed = reconstruct_run_action_draft(project, run)
        assert reconstructed.bound.request.expected_generated_columns == {
            "summary": gen_col
        }
        assert typed_run_has_matching_producer_receipt(
            project, run_id=int(run_id), bound=reconstructed.bound
        )
        program = typed_program_from_runner_spec(project, reconstructed.runner_spec)
        assert program is not None
