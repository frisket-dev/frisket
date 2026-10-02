"""Standalone maintenance must not take ownership of caller writes."""

import pytest

from frisket.engine.store import Project


@pytest.mark.parametrize("operation", ["compact", "export_database", "export"])
def test_maintenance_rejects_pending_writes_without_rolling_them_back(
    tmp_path, operation
):
    project = Project.create(tmp_path / "source.frisket")
    try:
        original = project.db.execute(
            "SELECT value FROM meta WHERE key='op_cursor'"
        ).fetchone()[0]
        project.db.execute("UPDATE meta SET value='12345' WHERE key='op_cursor'")
        target = tmp_path / "output" / "snapshot"
        with pytest.raises(RuntimeError, match="pending writes"):
            if operation == "compact":
                project.compact(vacuum=False)
            else:
                getattr(project, operation)(target)
        assert project.db.in_transaction
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key='op_cursor'"
            ).fetchone()[0]
            == "12345"
        )
        assert not target.exists()
        project.db.rollback()
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key='op_cursor'"
            ).fetchone()[0]
            == original
        )
    finally:
        project.close()


def test_retention_policy_read_does_not_commit_or_rewrite_project(tmp_path):
    project = Project.create(tmp_path / "source.frisket")
    try:
        before = project.db.total_changes
        manifest = project.path / "manifest.json"
        before_manifest = manifest.stat().st_mtime_ns
        project.db.execute("UPDATE meta SET value='12345' WHERE key='op_cursor'")
        assert project.retention_policy()["no_compact"] is False
        assert project.db.in_transaction
        assert project.db.total_changes == before + 1
        assert manifest.stat().st_mtime_ns == before_manifest
        project.db.rollback()
        assert (
            project.db.execute(
                "SELECT value FROM meta WHERE key='op_cursor'"
            ).fetchone()[0]
            != "12345"
        )
    finally:
        project.close()
