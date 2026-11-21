"""Rename generated-column schema tables, columns, indexes, and link metadata.

Preserves every row in ``columns``, claims, judgments, saved comparison
variants, and expected-answer link mappings losslessly.
"""

from __future__ import annotations

import json
import sqlite3

from frisket.engine.store.current_cells import register_current_cell_functions
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project.receipt_records_migration import (
    RECEIPT_RECORDS_TO_DIGEST,
)


GENERATED_COLUMNS_FROM_DIGEST = RECEIPT_RECORDS_TO_DIGEST
GENERATED_COLUMNS_TO_DIGEST = "frisket.schema.v1:b72517a790628db308d8a10a4d225b98"

_OLD_CLAIM_INDEXES = (
    "idx_output_claims_active_name",
    "idx_output_claims_active_column",
    "idx_output_claims_claim_token",
    "idx_output_claims_run",
    "idx_output_claims_receipt",
    "idx_output_claims_status_expiry",
)

_NEW_CLAIM_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_column_claims_active_name "
    "ON generated_column_claims(sheet_id, output_name) "
    "WHERE status='active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_column_claims_active_column "
    "ON generated_column_claims(column_id) "
    "WHERE status='active' AND column_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_generated_column_claims_claim_token "
    "ON generated_column_claims(claim_token)",
    "CREATE INDEX IF NOT EXISTS idx_generated_column_claims_run "
    "ON generated_column_claims(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_generated_column_claims_receipt "
    "ON generated_column_claims(receipt_id)",
    "CREATE INDEX IF NOT EXISTS idx_generated_column_claims_status_expiry "
    "ON generated_column_claims(status, lease_expires_at)",
)


def _rewrite_expected_link_fields(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT id, fields_json FROM expected_answer_links").fetchall()
    for link_id, raw_fields in rows:
        fields = json.loads(raw_fields)
        if not isinstance(fields, list):
            continue
        changed = False
        rewritten = []
        for entry in fields:
            if isinstance(entry, dict) and "output_column_id" in entry:
                updated = {
                    ("generated_column_id" if key == "output_column_id" else key): value
                    for key, value in entry.items()
                }
                rewritten.append(updated)
                changed = True
            else:
                rewritten.append(entry)
        if changed:
            db.execute(
                "UPDATE expected_answer_links SET fields_json=? WHERE id=?",
                (
                    json.dumps(
                        rewritten,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    link_id,
                ),
            )


def apply_generated_columns_migration(db: sqlite3.Connection) -> None:
    register_current_cell_functions(db)
    db.execute("ALTER TABLE columns RENAME COLUMN ai_generated TO generated")
    for index_name in _OLD_CLAIM_INDEXES:
        db.execute(f"DROP INDEX IF EXISTS {index_name}")
    db.execute("ALTER TABLE output_column_claims RENAME TO generated_column_claims")
    for statement in _NEW_CLAIM_INDEXES:
        db.execute(statement)
    db.execute(
        "ALTER TABLE evaluation_judgments "
        "RENAME COLUMN output_column_id TO generated_column_id"
    )
    db.execute(
        "ALTER TABLE saved_compare_variants "
        "RENAME COLUMN output_columns_json TO generated_columns_json"
    )
    _rewrite_expected_link_fields(db)
    violation = db.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise BundleSchemaMismatch(
            f"Generated-columns upgrade found a broken reference: {tuple(violation)}"
        )
