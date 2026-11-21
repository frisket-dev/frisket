"""Lossless upgrade from COPY_PRECEDENCE_TO_DIGEST to generated-column schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store import evaluation as evaluation_store
from frisket.engine.store.current_cells import register_current_cell_functions
from frisket.engine.store.generated_column_claims import GeneratedColumnClaimStore
from frisket.engine.store.schema import (
    SCHEMA,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
    schema_digest,
)
from frisket.upgrades.project._step import run_step
from frisket.upgrades.project.edit_precedence_migration import (
    COPY_PRECEDENCE_TO_DIGEST,
)
from frisket.upgrades.project.generated_columns_migration import (
    GENERATED_COLUMNS_FROM_DIGEST,
    GENERATED_COLUMNS_TO_DIGEST,
    apply_generated_columns_migration,
)


def _pre_upgrade_schema() -> str:
    """Reconstruct the exact COPY_PRECEDENCE_TO_DIGEST DDL from current SCHEMA."""
    return (
        SCHEMA.replace(
            "  generated INTEGER NOT NULL DEFAULT 0,",
            "  ai_generated INTEGER NOT NULL DEFAULT 0,",
        )
        .replace(
            "CREATE TABLE IF NOT EXISTS generated_column_claims (",
            "CREATE TABLE IF NOT EXISTS output_column_claims (",
        )
        .replace(
            "idx_generated_column_claims_active_name",
            "idx_output_claims_active_name",
        )
        .replace(
            "idx_generated_column_claims_active_column",
            "idx_output_claims_active_column",
        )
        .replace(
            "idx_generated_column_claims_claim_token",
            "idx_output_claims_claim_token",
        )
        .replace(
            "idx_generated_column_claims_run",
            "idx_output_claims_run",
        )
        .replace(
            "idx_generated_column_claims_receipt",
            "idx_output_claims_receipt",
        )
        .replace(
            "idx_generated_column_claims_status_expiry",
            "idx_output_claims_status_expiry",
        )
        .replace(
            "ON generated_column_claims(",
            "ON output_column_claims(",
        )
        .replace(
            "NOT generated_column_claims:",
            "NOT output_column_claims:",
        )
        .replace(
            "generated_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,",
            "output_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,",
        )
        .replace(
            "PRIMARY KEY (sheet_id,row_id,expected_column_id,generated_column_id,",
            "PRIMARY KEY (sheet_id,row_id,expected_column_id,output_column_id,",
        )
        .replace(
            "generated_columns_json TEXT NOT NULL CHECK (json_valid(generated_columns_json)),",
            "output_columns_json TEXT NOT NULL CHECK (json_valid(output_columns_json)),",
        )
    )


def _build_pre_upgrade_bundle(path: Path) -> dict[str, int]:
    path.mkdir(parents=True, exist_ok=True)
    (path / "blobs").mkdir(exist_ok=True)
    pre_ddl = _pre_upgrade_schema()
    assert schema_digest(pre_ddl) == COPY_PRECEDENCE_TO_DIGEST

    db = sqlite3.connect(path / "project.db")
    try:
        register_current_cell_functions(db)
        db.executescript(pre_ddl)
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
            (SCHEMA_DIGEST_META_KEY, COPY_PRECEDENCE_TO_DIGEST),
        )
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version','1')"
        )
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('op_cursor','0')")
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('change_sequence','0')"
        )

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
        assert digest == SCHEMA_DIGEST == GENERATED_COLUMNS_TO_DIGEST
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
