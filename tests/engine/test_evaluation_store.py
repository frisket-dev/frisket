"""The additive evaluation upgrade preserves ordinary data and stamps atomically."""

from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.evaluation_migration import (
    EVALUATION_FROM_DIGEST,
    EVALUATION_TO_DIGEST,
    migrate_evaluation,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY

TABLES = (
    "evaluation_judgments",
    "expected_answer_rules",
    "saved_compare_variants",
    "expected_answer_links",
)


def prior(project):
    for table in TABLES:
        project.db.execute(f"DROP TABLE {table}")
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (EVALUATION_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    project.db.commit()


def test_upgrade_preserves_cells_and_is_idempotent(tmp_path):
    path = tmp_path / "project"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "answer")
        row = project.add_rows(sheet, [{"answer": "original"}], {"answer": column})[0]
        prior(project)
    with closing(Project(path)) as project:
        assert project.get_values(sheet, column) == {row: "original"}
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == SCHEMA_DIGEST
            == EVALUATION_TO_DIGEST
        )
        migrate_evaluation(project.db)
        assert all(
            project.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            for table in TABLES
        )
        assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_stamp_rolls_back_tables_and_preserves_old_stamp(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        prior(project)
        project.db.execute(
            "CREATE TRIGGER refuse_evaluation_stamp BEFORE UPDATE ON meta WHEN NEW.key='schema_digest' BEGIN SELECT RAISE(ABORT,'stamp refused'); END"
        )
        project.db.commit()
        with pytest.raises(Exception, match="stamp refused"):
            migrate_evaluation(project.db)
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == EVALUATION_FROM_DIGEST
        )
        assert not project.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='expected_answer_links'"
        ).fetchone()


def test_undoable_row_preserves_caller_owned_transaction(tmp_path):
    from frisket.engine.store.evaluation import write_transaction

    with closing(Project.create(tmp_path / "project")) as project:
        sheet = project.add_sheet("Expected")
        column = project.add_column(sheet, "answer")
        first = project.add_row_with_undo(sheet, {"answer": "kept"}, {"answer": column})
        assert not project.db.in_transaction
        before = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        with pytest.raises(RuntimeError, match="abort"):
            with write_transaction(project):
                project.add_row_with_undo(
                    sheet, {"answer": "rolled back"}, {"answer": column}
                )
                assert project.db.in_transaction
                raise RuntimeError("abort")
        assert project.get_values(sheet, column) == {first: "kept"}
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == before
