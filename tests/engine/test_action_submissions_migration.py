from contextlib import closing
import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.action_submissions_migration import (
    ACTION_SUBMISSIONS_FROM_DIGEST,
    ACTION_SUBMISSIONS_TO_DIGEST,
    migrate_action_submissions,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY


def prior(project):
    project.db.execute("DROP TABLE action_submissions")
    project.db.execute("DROP INDEX idx_runs_action_history")
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (ACTION_SUBMISSIONS_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    project.db.commit()


def test_upgrade_preserves_data_and_undo_history(tmp_path):
    path = tmp_path / "project"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "note")
        rows = project.add_rows(sheet, [{"note": "keep"}], {"note": column})
        ops = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        prior(project)
    with closing(Project(path)) as project:
        assert project.get_values(sheet, column) == {rows[0]: "keep"}
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == ops
        assert (
            project.db.execute("SELECT COUNT(*) FROM action_submissions").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == SCHEMA_DIGEST
            == ACTION_SUBMISSIONS_TO_DIGEST
        )
        migrate_action_submissions(project.db)
        assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_upgrade_rolls_back_atomically(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        prior(project)
        project.db.execute(
            "CREATE TRIGGER refuse_stamp BEFORE UPDATE ON meta WHEN NEW.key='schema_digest' BEGIN SELECT RAISE(ABORT,'stamp refused'); END"
        )
        project.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="stamp refused"):
            migrate_action_submissions(project.db)
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == ACTION_SUBMISSIONS_FROM_DIGEST
        )
        assert not project.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='action_submissions'"
        ).fetchone()
