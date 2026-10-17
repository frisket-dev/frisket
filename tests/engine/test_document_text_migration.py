from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.document_text_migration import (
    DOCUMENT_TEXT_FROM_DIGEST,
    DOCUMENT_TEXT_TO_DIGEST,
    migrate_document_text,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY


def _restore_prior_layout(project: Project) -> None:
    project.db.execute("DROP VIEW current_document_text_selections")
    project.db.execute("DROP TABLE document_text_manual_pins")
    project.db.execute("DROP TABLE document_text_column_roles")
    project.db.execute(
        "DROP TRIGGER trg_run_output_generations_immutable_declaration"
    )
    project.db.execute(
        "ALTER TABLE run_output_generations DROP COLUMN document_text_authority"
    )
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (DOCUMENT_TEXT_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    project.db.commit()


def test_prior_bundle_migrates_without_guessing_legacy_roles(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "prior.frisket")
    path = project.path
    _restore_prior_layout(project)
    project.close()

    reopened = Project(path)
    try:
        assert DOCUMENT_TEXT_TO_DIGEST == SCHEMA_DIGEST
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        assert {
            str(row[0])
            for row in reopened.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'document_text_%'"
            )
        } == {"document_text_column_roles", "document_text_manual_pins"}
        assert reopened.db.execute(
            "SELECT COUNT(*) FROM document_text_column_roles"
        ).fetchone()[0] == 0
        generation_columns = {
            str(row[1])
            for row in reopened.db.execute(
                "PRAGMA table_info(run_output_generations)"
            )
        }
        assert "document_text_authority" in generation_columns
        trigger_sql = str(
            reopened.db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                "name='trg_run_output_generations_immutable_declaration'"
            ).fetchone()[0]
        )
        assert "document_text_authority" in trigger_sql
    finally:
        reopened.close()


def test_migration_rolls_back_schema_and_stamp_together(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "rollback.frisket")
    try:
        _restore_prior_layout(project)
        project.db.execute(
            "CREATE TRIGGER interrupt_document_text_upgrade BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' AND NEW.value<>OLD.value "
            "BEGIN SELECT RAISE(ABORT,'migration interrupted'); END"
        )
        project.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
            migrate_document_text(project.db, bundle_path=project.db_path)
        assert project.get_meta(SCHEMA_DIGEST_META_KEY) == DOCUMENT_TEXT_FROM_DIGEST
        assert "document_text_authority" not in {
            str(row[1])
            for row in project.db.execute(
                "PRAGMA table_info(run_output_generations)"
            )
        }
        assert project.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='document_text_column_roles'"
        ).fetchone() is None
    finally:
        project.close()
