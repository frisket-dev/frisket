from __future__ import annotations

import sqlite3
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.prepared_content_migration import (
    PREPARED_CONTENT_FROM_DIGEST,
    PREPARED_CONTENT_TO_DIGEST,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY


def test_prepared_content_migration_preserves_existing_bundle(tmp_path: Path) -> None:
    path = tmp_path / "prior.frisket"
    project = Project.create(path, name="Prior")
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "Text")
    row_id = project.add_rows(sheet_id, [{"Text": "kept"}], {"Text": column_id})[0]
    project.close()

    with sqlite3.connect(path / "project.db") as db:
        db.execute("DROP VIEW current_cell_values")
        db.execute("DROP VIEW prepared_content_ref_values")
        for table in (
            "prepared_content_refs",
            "prepared_content_set_pages",
            "prepared_content_sets",
            "prepared_page_versions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute(
            "CREATE VIEW current_cell_values AS "
            "SELECT head.column_id,head.row_id,source.value_kind,source.value,"
            "head.origin_kind,head.origin_op_id,head.origin_run_id,"
            "head.base_producer_id,head.validity "
            "FROM current_cells AS head JOIN cells AS source "
            "ON source.row_id=head.row_id AND source.column_id=head.column_id "
            "AND source.producer_id IS head.base_producer_id"
        )
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (PREPARED_CONTENT_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
        )

    reopened = Project(path)
    try:
        assert PREPARED_CONTENT_TO_DIGEST == SCHEMA_DIGEST
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        assert reopened.get_values(sheet_id, column_id) == {row_id: "kept"}
        authority_sql = str(
            reopened.db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='cells'"
            ).fetchone()[0]
        )
        assert "prepared_content_ref" in authority_sql
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()
